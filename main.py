"""
Digging Market — 셀러/소비자용 경량 웹 서비스
FastAPI + SQLite, 결제 기능 없음 (외부 채널 연결 방식)
경매(입찰) / 즉시구매 지원 — 낙찰·구매 확정 후 거래는 셀러 연락처로 앱 밖에서 진행
"""
import os
import re
import sqlite3
import uuid
import base64
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
# DATA_DIR: Render에서 Persistent Disk를 마운트할 경로 (예: /var/data).
# 환경변수가 없으면 로컬 개발 시 프로젝트 폴더를 그대로 사용.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "digging_market.db"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Digging Market API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def write_tx():
    """
    상태를 바꾸는 쓰기 트랜잭션 (입찰/즉시구매/마감/상태변경).

    BEGIN IMMEDIATE로 시작해 트랜잭션 시작 시점에 쓰기 락을 먼저 잡는다.
    SQLite는 쓰기가 DB 단위로 직렬화되므로 '읽기 → 검증 → 쓰기' 사이에 다른 쓰기가 끼어들 수 없다.
    추가로 UPDATE마다 version 조건을 걸어 두었기 때문에, 행 단위 락을 쓰는 DB(Postgres 등)로
    옮겨도 같은 코드가 낙관적 락으로 동작해 '두 명이 동시에 낙찰/구매'되는 상황을 막는다.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# items 테이블에 경매/즉시구매용으로 추가된 컬럼.
# 이미 운영 중인 DB(Render Persistent Disk)에도 적용되도록 CREATE가 아니라 ALTER로 붙인다.
ITEM_SALE_COLUMNS = {
    "sale_type": "TEXT NOT NULL DEFAULT 'fixed'",  # fixed(고정가) | auction(경매)
    "buy_now_price": "INTEGER",  # 경매 즉시구매가 (선택). 고정가 상품은 price가 곧 즉시구매가
    "auction_end_at": "TEXT",  # UTC 'YYYY-MM-DD HH:MM:SS'
    "current_price": "INTEGER",  # 현재 최고 입찰가 (입찰 없으면 NULL, 시작가는 price)
    "bid_count": "INTEGER NOT NULL DEFAULT 0",
    "highest_bid_id": "TEXT",
    "version": "INTEGER NOT NULL DEFAULT 0",  # 낙관적 락용
}


def init_db():
    conn = get_db()
    # WAL: 쓰기 중에도 읽기(피드 조회)가 막히지 않게
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            seller_token TEXT NOT NULL,
            seller_name TEXT NOT NULL,
            booth_number TEXT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            price INTEGER NOT NULL,
            image_path TEXT,
            status TEXT DEFAULT 'available',
            likes INTEGER DEFAULT 0,
            views INTEGER DEFAULT 0,
            contact_link TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            visited_time TEXT,
            interest_category TEXT,
            revisit_intent INTEGER,
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sellers (
            token TEXT PRIMARY KEY,
            seller_name TEXT NOT NULL,
            booth_number TEXT,
            contact_link TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            icon TEXT NOT NULL DEFAULT '📦',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # 기존에 하드코딩되어 있던 카테고리를 초기값으로 시드 (최초 1회만)
    existing = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
    if existing == 0:
        default_categories = [
            ("LP", "💿"),
            ("카세트", "📼"),
            ("의류", "👕"),
            ("기타", "📦"),
        ]
        for name, icon in default_categories:
            conn.execute(
                "INSERT INTO categories (id, name, icon) VALUES (?, ?, ?)",
                (str(uuid.uuid4())[:8], name, icon),
            )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    # ---- 경매/즉시구매 ----
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    for col, ddl in ITEM_SALE_COLUMNS.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")

    # 입찰 기록은 insert-only (수정/취소 없음)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bids (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            buyer_token TEXT NOT NULL,
            buyer_name TEXT NOT NULL,
            buyer_contact TEXT NOT NULL,
            amount INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_item ON bids(item_id, amount DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_buyer ON bids(buyer_token)")

    # 낙찰/즉시구매로 생긴 거래. 결제는 앱 밖에서 하므로 '누가 얼마에 사기로 했는지'만 기록
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            buyer_token TEXT NOT NULL,
            buyer_name TEXT NOT NULL,
            buyer_contact TEXT NOT NULL,
            price INTEGER NOT NULL,
            order_type TEXT NOT NULL,              -- buy_now | auction_win
            status TEXT NOT NULL DEFAULT 'pending', -- pending | completed | canceled
            created_at TEXT NOT NULL
        )
        """
    )
    # 최후 방어선: 한 상품에 유효한 거래는 DB 차원에서 1건만 존재할 수 있다
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_active_item "
        "ON orders(item_id) WHERE status != 'canceled'"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_token)")

    create_item_view(conn)
    conn.commit()
    conn.close()


# 상품에는 등록 시점의 셀러명/부스/인스타가 복사돼 있지만(과거 구조), 화면에 보여줄 땐 항상
# 셀러 테이블의 '현재 값'을 쓴다. 셀러가 이름을 바꾸거나 관리자가 부스를 바꾸면 기존 상품에도 바로 반영.
# 셀러 행이 없을 때(이론상)만 상품에 복사된 값으로 대체한다.
SELLER_JOINED_COLUMNS = ("seller_name", "booth_number", "contact_link")


def create_item_view(conn):
    """items 컬럼이 마이그레이션으로 늘어날 수 있어서 서버 시작 때마다 view를 다시 만든다"""
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()]
    select_list = ",\n    ".join(
        f"COALESCE(s.{c}, i.{c}) AS {c}" if c in SELLER_JOINED_COLUMNS else f"i.{c} AS {c}"
        for c in columns
    )
    conn.execute("DROP VIEW IF EXISTS item_view")
    conn.execute(
        f"CREATE VIEW item_view AS SELECT\n    {select_list}\n"
        "FROM items i LEFT JOIN sellers s ON s.token = i.seller_token"
    )


init_db()


# ---------------------------------------------------------------------------
# 경매/즉시구매 규칙 — 판정 로직은 전부 서버에서 (클라이언트 값은 신뢰하지 않음)
# ---------------------------------------------------------------------------
TIME_FMT = "%Y-%m-%d %H:%M:%S"  # SQLite CURRENT_TIMESTAMP와 같은 UTC 포맷
AUCTION_DURATIONS_MIN = (10, 30, 60, 120, 240, 1440, 4320)  # 10분 ~ 3일
ANTI_SNIPING_WINDOW = timedelta(minutes=3)  # 마감 3분 안에 입찰이 들어오면 마감을 3분 뒤로 연장
MAX_PRICE = 100_000_000


def now_utc() -> datetime:
    """테스트에서 시간을 바꿔 끼울 수 있도록 한 곳에서만 현재 시각을 만든다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fmt_time(dt: datetime) -> str:
    return dt.strftime(TIME_FMT)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FMT)


def min_increment(price: int) -> int:
    """현재가 구간별 최소 입찰 단위"""
    if price < 10_000:
        return 500
    if price < 50_000:
        return 1_000
    if price < 100_000:
        return 2_000
    return 5_000


def next_min_bid(item: dict) -> int:
    if item["bid_count"] == 0:
        return item["price"]  # 첫 입찰은 시작가부터
    return item["current_price"] + min_increment(item["current_price"])


def is_auction_open(item: dict, now: datetime) -> bool:
    return (
        item["sale_type"] == "auction"
        and item["status"] == "available"
        and parse_time(item["auction_end_at"]) > now
    )


def can_buy_now(item: dict, now: datetime) -> bool:
    if item["status"] != "available":
        return False
    if item["sale_type"] == "fixed":
        return True
    # 경매: 즉시구매가가 있고, 아직 아무도 입찰하지 않았을 때만 (첫 입찰이 들어오면 즉시구매 마감)
    return bool(item["buy_now_price"]) and item["bid_count"] == 0 and is_auction_open(item, now)


PRIVATE_ITEM_FIELDS = ("seller_token", "highest_bid_id", "version")


def public_item(row, now: Optional[datetime] = None) -> dict:
    """
    외부에 내보내는 상품 응답.
    seller_token은 상품 수정/삭제 권한 그 자체라서 절대 공개 응답에 포함하면 안 된다.
    """
    now = now or now_utc()
    item = dict(row)
    item["buy_now_available"] = can_buy_now(item, now)
    if item["sale_type"] == "auction":
        item["display_price"] = item["current_price"] if item["bid_count"] else item["price"]
        item["min_next_bid"] = next_min_bid(item)
        item["auction_open"] = is_auction_open(item, now)
    else:
        item["display_price"] = item["price"]
    for field in PRIVATE_ITEM_FIELDS:
        item.pop(field, None)
    return item


def mask_name(name: str) -> str:
    name = (name or "").strip()
    if len(name) <= 1:
        return "*"
    return name[0] + "*" * (len(name) - 1)


def _close_expired(conn, now: datetime) -> int:
    """
    마감 시각이 지난 경매를 확정한다 (write_tx 안에서만 호출).
    입찰이 있으면 최고 입찰자에게 낙찰(reserved + 주문 생성), 없으면 유찰(unsold).
    """
    rows = conn.execute(
        "SELECT * FROM items WHERE sale_type = 'auction' AND status = 'available' AND auction_end_at <= ?",
        (fmt_time(now),),
    ).fetchall()
    closed = 0
    for item in rows:
        if item["bid_count"] > 0:
            bid = conn.execute("SELECT * FROM bids WHERE id = ?", (item["highest_bid_id"],)).fetchone()
            cur = conn.execute(
                "UPDATE items SET status = 'reserved', version = version + 1 "
                "WHERE id = ? AND version = ? AND status = 'available'",
                (item["id"], item["version"]),
            )
            if cur.rowcount == 1:
                conn.execute(
                    """
                    INSERT INTO orders (id, item_id, buyer_token, buyer_name, buyer_contact, price, order_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'auction_win', ?)
                    """,
                    (
                        str(uuid.uuid4())[:8],
                        item["id"],
                        bid["buyer_token"],
                        bid["buyer_name"],
                        bid["buyer_contact"],
                        bid["amount"],
                        fmt_time(now),
                    ),
                )
                closed += 1
        else:
            cur = conn.execute(
                "UPDATE items SET status = 'unsold', version = version + 1 "
                "WHERE id = ? AND version = ? AND status = 'available'",
                (item["id"], item["version"]),
            )
            closed += cur.rowcount
    return closed


def finalize_expired_auctions() -> int:
    """
    지연 마감(lazy close). 백그라운드 스케줄러 대신 조회/입찰 요청이 들어올 때 마감을 확정한다.
    Render 무료/Starter 인스턴스는 잠들거나 재시작될 수 있어 앱 내부 타이머를 믿을 수 없기 때문.
    대부분의 요청에서는 락 없이 '마감할 게 있는지'만 확인하고 바로 빠진다.
    """
    now = now_utc()
    conn = get_db()
    pending = conn.execute(
        "SELECT 1 FROM items WHERE sale_type = 'auction' AND status = 'available' AND auction_end_at <= ? LIMIT 1",
        (fmt_time(now),),
    ).fetchone()
    conn.close()
    if not pending:
        return 0
    with write_tx() as tx:
        return _close_expired(tx, now)


# ---------------------------------------------------------------------------
# AI 이미지 분석 (Claude Vision) — ANTHROPIC_API_KEY 환경변수 필요
# 키가 없거나 호출 실패 시 빈 값으로 폴백 (셀러가 직접 입력)
# ---------------------------------------------------------------------------


def analyze_image_with_ai(image_bytes: bytes, filename: str) -> dict:
    """
    이미지를 분석해 카테고리/제목/설명 초안을 생성한다.
    ANTHROPIC_API_KEY가 설정되어 있으면 Claude Vision을 사용하고,
    없거나 호출에 실패하면 빈 값으로 폴백한다 (셀러가 직접 채우면 됨).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if api_key:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
            b64_image = base64.b64encode(image_bytes).decode("utf-8")
            media_type = "image/jpeg"
            if filename.lower().endswith(".png"):
                media_type = "image/png"

            conn = get_db()
            category_names = [
                row["name"]
                for row in conn.execute("SELECT name FROM categories ORDER BY created_at ASC").fetchall()
            ]
            conn.close()
            category_list = ", ".join(category_names) if category_names else "기타"

            prompt = (
                "이 사진은 플리마켓에서 판매할 중고 물건입니다. "
                "다음 JSON 형식으로만 답하세요 (다른 텍스트 없이):\n"
                f'{{"category": "다음 중 하나: {category_list}", '
                '"title": "짧은 상품명", '
                '"description": "상태와 특징을 담은 한 줄 설명 (30자 이내)"}'
            )

            message = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=300,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_image,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            text = message.content[0].text.strip()
            # 마크다운 코드펜스 제거
            text = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(text)
            return {
                "category": result.get("category", ""),
                "title": result.get("title", ""),
                "description": result.get("description", ""),
                "ai_used": True,
            }
        except Exception as e:
            # AI 호출 실패는 서버 로그에 명확히 남김 (Render Logs 탭에서 확인 가능)
            print(f"[AI 분석 실패] {type(e).__name__}: {e}", flush=True)

    # ------------------- 폴백 (AI 미설정/실패 시 — 빈 값으로 셀러가 직접 입력) -------------------
    return {
        "category": "",
        "title": "",
        "description": "",
        "ai_used": False,
    }


# ---------------------------------------------------------------------------
# API 모델
# ---------------------------------------------------------------------------
class LikeToggle(BaseModel):
    liked: bool


class FeedbackIn(BaseModel):
    visited_time: Optional[str] = None
    interest_category: Optional[str] = None
    revisit_intent: Optional[int] = None
    comment: Optional[str] = None


# ---------------------------------------------------------------------------
# 카테고리 관리 — admin이 관리하고, 랜딩/피드/셀러등록/내상품관리가 여기서 끌어옴
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 행사 정보 (날짜/시간/장소) — admin이 설정하고 랜딩 페이지가 끌어옴
# ---------------------------------------------------------------------------
EVENT_SETTING_KEYS = ("event_date", "event_time", "event_place")


@app.get("/api/event")
def get_event_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM event_settings").fetchall()
    conn.close()
    settings = {row["key"]: row["value"] for row in rows}
    return {k: settings.get(k, "") for k in EVENT_SETTING_KEYS}


class EventSettingsUpdate(BaseModel):
    event_date: str = ""
    event_time: str = ""
    event_place: str = ""


@app.post("/api/admin/event")
def update_event_settings(payload: EventSettingsUpdate):
    conn = get_db()
    for key in EVENT_SETTING_KEYS:
        value = getattr(payload, key)
        conn.execute(
            "INSERT INTO event_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    conn.commit()
    conn.close()
    return get_event_settings()


@app.get("/api/categories")
def list_categories():
    conn = get_db()
    rows = conn.execute("SELECT * FROM categories ORDER BY created_at ASC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


class CategoryCreate(BaseModel):
    name: str
    icon: str = "📦"


@app.post("/api/admin/categories")
def create_category(payload: CategoryCreate):
    name = payload.name.strip()
    icon = payload.icon.strip() or "📦"
    if not name:
        raise HTTPException(status_code=400, detail="카테고리 이름을 입력해주세요")
    conn = get_db()
    existing = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="이미 있는 카테고리예요")
    cat_id = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT INTO categories (id, name, icon) VALUES (?, ?, ?)", (cat_id, name, icon)
    )
    conn.commit()
    conn.close()
    return {"id": cat_id, "name": name, "icon": icon}


@app.delete("/api/admin/categories/{category_id}")
def delete_category(category_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="카테고리를 찾을 수 없어요")

    category = dict(row)
    if category["name"] == "기타":
        conn.close()
        raise HTTPException(
            status_code=400, detail="'기타' 카테고리는 삭제할 수 없어요 (삭제된 카테고리의 기본값으로 쓰여요)"
        )

    # 이 카테고리를 쓰던 기존 상품들은 '기타'로 재할당
    conn.execute("UPDATE items SET category = '기타' WHERE category = ?", (category["name"],))
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()
    return {"message": "삭제되었어요. 이 카테고리로 등록됐던 상품은 '기타'로 이동됐어요."}


# ---------------------------------------------------------------------------
# 셀러 토큰 검증 (QR로 들어온 셀러만 등록 가능하게)
# ---------------------------------------------------------------------------
def get_seller_by_token(token: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM sellers WHERE token = ?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


@app.get("/api/sellers/verify")
def verify_seller_token(token: str):
    """QR로 들어왔을 때 토큰이 유효한 셀러인지 확인하고, 사전 등록된 정보를 돌려준다."""
    seller = get_seller_by_token(token)
    if not seller:
        raise HTTPException(status_code=403, detail="유효하지 않은 접근입니다. 셀러용 QR코드로 접속해주세요.")
    return seller


# ---------------------------------------------------------------------------
# 셀러 중복 체크 — 이름 / 부스 번호 / 인스타 아이디
# 표기만 다른 같은 값("7"과 "7번", "@Minji"와 "minji")도 같은 값으로 본다.
# ---------------------------------------------------------------------------
def normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def normalize_booth(value: str) -> str:
    v = re.sub(r"\s+", "", value or "").lower()
    return re.sub(r"번$", "", v)


def normalize_instagram(value: str) -> str:
    """프론트와 같은 규칙: URL/@ 제거 후 아이디만. 인스타 아이디는 대소문자 구분이 없다."""
    v = (value or "").strip()
    v = re.sub(r"^https?://", "", v, flags=re.I)
    v = re.sub(r"^(www\.)?instagram\.com/", "", v, flags=re.I)
    v = v.lstrip("@")
    v = v.split("/")[0].split("?")[0]
    return v.strip().lower()


SELLER_FIELD_LABELS = {  # 받침에 따라 '이에요/예요'가 달라서 문장째로 둔다
    "seller_name": "셀러 이름이에요",
    "booth_number": "부스 번호예요",
    "contact_link": "인스타그램 아이디예요",
}

# 셀러 데이터 모델
# - token(내부 ID): 시스템 식별자이자 셀러 화면 접속 열쇠. 바뀌지 않음
# - 부스 번호: 사람이 부르는 업무 식별자 → 필수 + 중복 불가, 관리자만 변경
# - 인스타 ID: 같으면 사실상 같은 사람 → 중복 불가 (비어 있으면 검사 안 함)
# - 셀러명: 표시용 이름. 부스로 구분되므로 중복 허용, 대신 경고만
BLOCKING_FIELDS = ("booth_number", "contact_link")
SELLER_NORMALIZERS = {
    "seller_name": normalize_name,
    "booth_number": normalize_booth,
    "contact_link": normalize_instagram,
}


def find_seller_conflicts(conn, seller_name: str, booth_number: str, contact_link: str,
                          exclude_token: Optional[str] = None) -> list:
    """
    같은 값을 이미 쓰고 있는 다른 셀러 목록.
    blocking=True(부스/인스타)면 저장 불가, False(셀러명)면 경고만.
    """
    targets = {
        "seller_name": normalize_name(seller_name),
        "booth_number": normalize_booth(booth_number),
        "contact_link": normalize_instagram(contact_link),
    }
    conflicts, seen = [], set()
    for row in conn.execute("SELECT token, seller_name, booth_number, contact_link FROM sellers").fetchall():
        if exclude_token and row["token"] == exclude_token:
            continue
        for field, target in targets.items():
            if field in seen or not target or SELLER_NORMALIZERS[field](row[field]) != target:
                continue
            seen.add(field)  # 같은 필드에서 여러 셀러와 겹치면 첫 번째만
            blocking = field in BLOCKING_FIELDS
            booth = f" · {normalize_booth(row['booth_number'])}번 부스" if normalize_booth(row["booth_number"]) else ""
            conflicts.append({
                "field": field,
                "blocking": blocking,
                "existing_seller": row["seller_name"],
                "message": (
                    f"이미 등록된 {SELLER_FIELD_LABELS[field]} ({row['seller_name']}{booth})"
                    if blocking
                    else f"같은 이름의 셀러가 이미 있어요 ({row['seller_name']}{booth}). 다른 셀러라면 그대로 저장해도 돼요"
                ),
            })
    return conflicts


def check_seller_conflicts(conn, seller_name: str, booth_number: str, contact_link: str,
                           exclude_token: Optional[str] = None) -> list:
    """차단 대상이 있으면 409, 없으면 경고 메시지 목록을 돌려준다"""
    conflicts = find_seller_conflicts(conn, seller_name, booth_number, contact_link, exclude_token)
    blocking = [c for c in conflicts if c["blocking"]]
    if blocking:
        raise HTTPException(status_code=409, detail=" / ".join(c["message"] for c in blocking))
    return [c["message"] for c in conflicts]


def clean_seller_fields(seller_name: str, booth_number: Optional[str], contact_link: str) -> tuple:
    """공통 입력 정리 + 필수값 검사. booth_number=None이면 부스는 검사하지 않음(셀러 본인 수정)"""
    seller_name = (seller_name or "").strip()
    contact_link = normalize_instagram(contact_link)
    if not seller_name:
        raise HTTPException(status_code=400, detail="셀러 이름을 입력해주세요")
    if len(seller_name) > 40:
        raise HTTPException(status_code=400, detail="셀러 이름은 40자 이내로 입력해주세요")
    if booth_number is not None:
        booth_number = booth_number.strip()
        if not normalize_booth(booth_number):
            raise HTTPException(status_code=400, detail="부스 번호를 입력해주세요")
    return seller_name, booth_number, contact_link


class SellerUpdate(BaseModel):
    seller_name: str
    contact_link: str = ""


@app.patch("/api/sellers/{token}")
def update_seller_info(token: str, payload: SellerUpdate):
    """셀러 본인: 셀러명/인스타만 수정 가능. 부스 번호는 관리자만 변경 가능."""
    seller_name, _, contact_link = clean_seller_fields(payload.seller_name, None, payload.contact_link)
    with write_tx() as tx:
        if not tx.execute("SELECT 1 FROM sellers WHERE token = ?", (token,)).fetchone():
            raise HTTPException(status_code=403, detail="유효하지 않은 셀러 토큰입니다")
        warnings = check_seller_conflicts(tx, seller_name, "", contact_link, exclude_token=token)
        tx.execute(
            "UPDATE sellers SET seller_name = ?, contact_link = ? WHERE token = ?",
            (seller_name, contact_link, token),
        )
    return {**get_seller_by_token(token), "warnings": warnings}


# ---------------------------------------------------------------------------
# 관리자: 셀러 사전 등록 / 전체 항목 수정 (본인만 사용 — QR 발급 전 미리 셀러 목록을 입력)
# ---------------------------------------------------------------------------
class SellerCreate(BaseModel):
    seller_name: str
    booth_number: str = ""
    contact_link: str = ""


@app.get("/api/admin/sellers/check")
def check_seller_duplicates(seller_name: str = "", booth_number: str = "", contact_link: str = "",
                            exclude_token: str = ""):
    """관리자 화면에서 입력하는 동안 실시간으로 중복을 알려주기 위한 조회용 API (수정 중엔 자기 자신 제외)"""
    conn = get_db()
    conflicts = find_seller_conflicts(conn, seller_name, booth_number, contact_link, exclude_token or None)
    conn.close()
    return {"conflicts": conflicts}


@app.post("/api/admin/sellers")
def create_seller(payload: SellerCreate):
    seller_name, booth_number, contact_link = clean_seller_fields(
        payload.seller_name, payload.booth_number, payload.contact_link
    )
    token = str(uuid.uuid4())[:10]
    # 검사와 저장을 한 트랜잭션에서 — 동시에 같은 부스/인스타로 두 번 등록하는 경우도 막힘
    with write_tx() as tx:
        warnings = check_seller_conflicts(tx, seller_name, booth_number, contact_link)
        tx.execute(
            "INSERT INTO sellers (token, seller_name, booth_number, contact_link) VALUES (?, ?, ?, ?)",
            (token, seller_name, booth_number, contact_link),
        )
    return {"token": token, "sell_url": f"/sell?token={token}", "warnings": warnings}


class SellerAdminUpdate(BaseModel):
    seller_name: str
    booth_number: str
    contact_link: str = ""


@app.patch("/api/admin/sellers/{token}")
def admin_update_seller(token: str, payload: SellerAdminUpdate):
    """
    관리자: 모든 항목 수정. 부스 필수화 이전에 부스 없이 등록된 셀러에게 부스를 배정하는 용도이기도 하다.
    상품 화면은 셀러 정보를 셀러 테이블에서 JOIN해 가져오므로, 여기서 바꾸면 기존 상품에도 바로 반영된다.
    """
    seller_name, booth_number, contact_link = clean_seller_fields(
        payload.seller_name, payload.booth_number, payload.contact_link
    )
    with write_tx() as tx:
        if not tx.execute("SELECT 1 FROM sellers WHERE token = ?", (token,)).fetchone():
            raise HTTPException(status_code=404, detail="셀러를 찾을 수 없어요")
        warnings = check_seller_conflicts(tx, seller_name, booth_number, contact_link, exclude_token=token)
        tx.execute(
            "UPDATE sellers SET seller_name = ?, booth_number = ?, contact_link = ? WHERE token = ?",
            (seller_name, booth_number, contact_link, token),
        )
    return {**get_seller_by_token(token), "warnings": warnings}


@app.get("/api/admin/sellers")
def list_sellers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM sellers ORDER BY created_at DESC").fetchall()
    conn.close()
    sellers = [dict(row) for row in rows]

    # 규칙 도입 전에 이미 들어간 중복 데이터(부스/인스타)를 관리자가 알아볼 수 있게 표시
    counts = {field: {} for field in BLOCKING_FIELDS}
    for s in sellers:
        for field in BLOCKING_FIELDS:
            key = SELLER_NORMALIZERS[field](s[field])
            if key:
                counts[field][key] = counts[field].get(key, 0) + 1
    for s in sellers:
        s["duplicate_fields"] = [
            field for field in BLOCKING_FIELDS
            if SELLER_NORMALIZERS[field](s[field]) and counts[field][SELLER_NORMALIZERS[field](s[field])] > 1
        ]
    return sellers


# ---------------------------------------------------------------------------
# 셀러: 이미지 업로드 → AI 분석 (등록 전 미리보기용)
# ---------------------------------------------------------------------------
@app.post("/api/analyze")
async def analyze(image: UploadFile = File(...)):
    image_bytes = await image.read()
    result = analyze_image_with_ai(image_bytes, image.filename or "upload.jpg")
    return result


# ---------------------------------------------------------------------------
# 셀러: 상품 등록 (토큰 필수 — 사전 등록된 셀러만 가능)
# ---------------------------------------------------------------------------
@app.post("/api/items")
async def create_item(
    token: str = Form(...),
    category: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    price: int = Form(...),
    image: UploadFile = File(...),
    sale_type: str = Form("fixed"),  # fixed | auction
    buy_now_price: Optional[int] = Form(None),  # 경매일 때만, 선택
    auction_minutes: Optional[int] = Form(None),  # 경매일 때만, 필수
):
    seller = get_seller_by_token(token)
    if not seller:
        raise HTTPException(status_code=403, detail="유효하지 않은 셀러 토큰입니다")
    if not normalize_booth(seller["booth_number"]):
        raise HTTPException(
            status_code=403, detail="부스 번호가 배정되지 않아 상품을 등록할 수 없어요. 운영진에게 문의해주세요"
        )

    # 이미지 저장 전에 판매 조건부터 검증 (잘못된 요청에 파일만 남는 걸 방지)
    if price <= 0 or price > MAX_PRICE:
        raise HTTPException(status_code=400, detail="가격을 다시 확인해주세요")
    auction_end_at = None
    if sale_type == "auction":
        if auction_minutes not in AUCTION_DURATIONS_MIN:
            raise HTTPException(status_code=400, detail="경매 기간을 선택해주세요")
        if buy_now_price is not None and buy_now_price <= price:
            raise HTTPException(status_code=400, detail="즉시구매가는 시작가보다 높아야 해요")
        if buy_now_price is not None and buy_now_price > MAX_PRICE:
            raise HTTPException(status_code=400, detail="즉시구매가를 다시 확인해주세요")
        auction_end_at = fmt_time(now_utc() + timedelta(minutes=auction_minutes))
    elif sale_type == "fixed":
        buy_now_price = None  # 고정가 상품은 price로 즉시구매
    else:
        raise HTTPException(status_code=400, detail="잘못된 판매 방식입니다")

    seller_name = seller["seller_name"]
    booth_number = seller["booth_number"]
    contact_link = seller["contact_link"]

    item_id = str(uuid.uuid4())[:8]
    ext = Path(image.filename or "jpg").suffix or ".jpg"
    image_filename = f"{item_id}{ext}"
    image_path = UPLOAD_DIR / image_filename

    contents = await image.read()
    with open(image_path, "wb") as f:
        f.write(contents)

    conn = get_db()
    conn.execute(
        """
        INSERT INTO items
        (id, seller_token, seller_name, booth_number, category, title, description, price, image_path, contact_link,
         sale_type, buy_now_price, auction_end_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            token,
            seller_name,
            booth_number,
            category,
            title,
            description,
            price,
            f"/uploads/{image_filename}",
            contact_link,
            sale_type,
            buy_now_price,
            auction_end_at,
        ),
    )
    conn.commit()
    conn.close()

    return {"id": item_id, "message": "등록되었습니다"}


# ---------------------------------------------------------------------------
# 소비자: 상품 목록 (카테고리 필터)
# ---------------------------------------------------------------------------
@app.get("/api/items")
def list_items(category: Optional[str] = None, status: Optional[str] = "available"):
    """
    status는 쉼표로 여러 개 지정 가능 (예: available,reserved,sold_out).
    판매중 → 예약중 → 판매완료 순으로, 같은 상태 안에서는 최신순.
    """
    finalize_expired_auctions()
    conn = get_db()
    query = "SELECT * FROM item_view WHERE 1=1"
    params = []
    if category and category != "전체":
        query += " AND category = ?"
        params.append(category)
    statuses = [s.strip() for s in (status or "").split(",") if s.strip()]
    if statuses:
        query += f" AND status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    query += """
        ORDER BY CASE status
                   WHEN 'available' THEN 0
                   WHEN 'reserved' THEN 1
                   WHEN 'sold_out' THEN 2
                   ELSE 3
                 END,
                 created_at DESC
    """
    rows = conn.execute(query, params).fetchall()
    conn.close()
    now = now_utc()
    return [public_item(row, now) for row in rows]


@app.get("/api/items/{item_id}")
def get_item(item_id: str):
    finalize_expired_auctions()
    conn = get_db()
    conn.execute("UPDATE items SET views = views + 1 WHERE id = ?", (item_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
    return public_item(row)


@app.post("/api/items/{item_id}/like")
def toggle_like(item_id: str, payload: LikeToggle):
    conn = get_db()
    delta = 1 if payload.liked else -1
    conn.execute("UPDATE items SET likes = likes + ? WHERE id = ?", (delta, item_id))
    conn.commit()
    row = conn.execute("SELECT likes FROM items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
    return {"likes": row["likes"]}


def check_item_ownership(item_id: str, token: str) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
    item = dict(row)
    if item["seller_token"] != token:
        raise HTTPException(status_code=403, detail="본인이 등록한 상품만 관리할 수 있어요")
    return item


class ItemStatusUpdate(BaseModel):
    token: str
    status: str  # available | reserved | sold_out


@app.patch("/api/items/{item_id}/status")
def update_status(item_id: str, payload: ItemStatusUpdate):
    if payload.status not in ("available", "reserved", "sold_out"):
        raise HTTPException(status_code=400, detail="잘못된 상태값입니다")
    finalize_expired_auctions()
    with write_tx() as tx:
        row = tx.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
        item = dict(row)
        if item["seller_token"] != payload.token:
            raise HTTPException(status_code=403, detail="본인이 등록한 상품만 관리할 수 있어요")

        if item["sale_type"] == "auction":
            # 진행 중/유찰된 경매를 셀러가 임의로 바꾸면 입찰자 입장에서 결과가 뒤집힌다
            if item["status"] in ("available", "unsold"):
                raise HTTPException(status_code=400, detail="경매 상품은 낙찰 후에만 상태를 바꿀 수 있어요")
            if payload.status == "available":
                raise HTTPException(status_code=400, detail="낙찰된 경매는 다시 판매중으로 돌릴 수 없어요")

        order_status = {"available": "canceled", "reserved": "pending", "sold_out": "completed"}[payload.status]
        # 고정가 상품을 '판매중'으로 되돌리면 = 즉시구매자가 거래를 안 한 것 → 해당 거래를 취소 처리
        tx.execute(
            "UPDATE orders SET status = ? WHERE item_id = ? AND status != 'canceled'",
            (order_status, item_id),
        )
        tx.execute(
            "UPDATE items SET status = ?, version = version + 1 WHERE id = ?",
            (payload.status, item_id),
        )
    return {"status": payload.status}


class ItemUpdate(BaseModel):
    token: str
    category: str
    title: str
    description: str = ""
    price: int


@app.patch("/api/items/{item_id}")
def edit_item(item_id: str, payload: ItemUpdate):
    if payload.price <= 0 or payload.price > MAX_PRICE:
        raise HTTPException(status_code=400, detail="가격을 다시 확인해주세요")
    with write_tx() as tx:
        row = tx.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
        item = dict(row)
        if item["seller_token"] != payload.token:
            raise HTTPException(status_code=403, detail="본인이 등록한 상품만 관리할 수 있어요")
        if item["sale_type"] == "auction" and payload.price != item["price"]:
            if item["bid_count"] > 0:
                raise HTTPException(status_code=400, detail="입찰이 들어온 경매는 시작가를 바꿀 수 없어요")
            if item["buy_now_price"] and payload.price >= item["buy_now_price"]:
                raise HTTPException(status_code=400, detail="시작가는 즉시구매가보다 낮아야 해요")
        tx.execute(
            "UPDATE items SET category = ?, title = ?, description = ?, price = ?, version = version + 1 WHERE id = ?",
            (payload.category, payload.title, payload.description, payload.price, item_id),
        )
        row = tx.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
    return public_item(row)


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, token: str):
    item = check_item_ownership(item_id, token)
    with write_tx() as tx:
        row = tx.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
        if row["sale_type"] == "auction" and row["status"] == "available" and row["bid_count"] > 0:
            raise HTTPException(status_code=400, detail="입찰이 진행 중인 경매는 삭제할 수 없어요")
        tx.execute("DELETE FROM bids WHERE item_id = ?", (item_id,))
        tx.execute("DELETE FROM orders WHERE item_id = ?", (item_id,))
        tx.execute("DELETE FROM items WHERE id = ?", (item_id,))
    # 업로드된 이미지 파일도 같이 정리
    try:
        img_path = UPLOAD_DIR / Path(item["image_path"]).name
        if img_path.exists():
            img_path.unlink()
    except Exception:
        pass
    return {"message": "삭제되었습니다"}


@app.get("/api/sellers/{token}/items")
def my_items(token: str):
    """셀러 본인 토큰으로 본인이 등록한 상품 목록 조회"""
    if not get_seller_by_token(token):
        raise HTTPException(status_code=403, detail="유효하지 않은 셀러 토큰입니다")
    finalize_expired_auctions()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM item_view WHERE seller_token = ? ORDER BY created_at DESC", (token,)
    ).fetchall()
    now = now_utc()
    result = []
    for row in rows:
        item = public_item(row, now)
        # 셀러 본인에게만 구매자(낙찰자) 연락처를 보여준다 — 거래는 앱 밖에서 진행
        order = conn.execute(
            "SELECT buyer_name, buyer_contact, price, order_type, status, created_at "
            "FROM orders WHERE item_id = ? AND status != 'canceled'",
            (row["id"],),
        ).fetchone()
        item["order"] = dict(order) if order else None
        result.append(item)
    conn.close()
    return result


# ---------------------------------------------------------------------------
# 구매자: 입찰 / 즉시구매
# 소비자는 로그인이 없으므로, 브라우저에서 만든 익명 토큰(buyer_token) + 닉네임 + 연락처로 식별한다.
# buyer_token은 본인 브라우저만 알고 있고, 공개 응답에는 절대 내보내지 않는다.
# ---------------------------------------------------------------------------
class BuyerIn(BaseModel):
    buyer_token: str
    buyer_name: str
    buyer_contact: str


class BidIn(BuyerIn):
    amount: int


def validate_buyer(payload: BuyerIn) -> dict:
    token = payload.buyer_token.strip()
    name = payload.buyer_name.strip()
    contact = payload.buyer_contact.strip()
    if not (16 <= len(token) <= 64):
        raise HTTPException(status_code=400, detail="구매자 정보가 올바르지 않아요. 새로고침 후 다시 시도해주세요")
    if not (1 <= len(name) <= 20):
        raise HTTPException(status_code=400, detail="닉네임을 1~20자로 입력해주세요")
    if not (1 <= len(contact) <= 50):
        raise HTTPException(status_code=400, detail="연락처를 입력해주세요")
    return {"buyer_token": token, "buyer_name": name, "buyer_contact": contact}


@app.post("/api/items/{item_id}/bids")
def place_bid(item_id: str, payload: BidIn):
    buyer = validate_buyer(payload)
    finalize_expired_auctions()
    now = now_utc()
    extended = False

    with write_tx() as tx:
        row = tx.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
        item = dict(row)
        if item["sale_type"] != "auction":
            raise HTTPException(status_code=400, detail="경매 상품이 아니에요")
        if not is_auction_open(item, now):
            raise HTTPException(status_code=409, detail="이미 종료된 경매예요")
        if buyer["buyer_token"] == item["seller_token"]:
            raise HTTPException(status_code=403, detail="본인 상품에는 입찰할 수 없어요")

        amount = payload.amount
        required = next_min_bid(item)
        if amount < required:
            raise HTTPException(
                status_code=409, detail=f"{required:,}원 이상부터 입찰할 수 있어요 (그새 다른 입찰이 들어왔을 수 있어요)"
            )
        if amount > MAX_PRICE:
            raise HTTPException(status_code=400, detail="입찰 금액을 다시 확인해주세요")
        if item["bid_count"] == 0 and item["buy_now_price"] and amount >= item["buy_now_price"]:
            raise HTTPException(status_code=400, detail="즉시구매가 이상이라면 즉시구매를 이용해주세요")

        # 스나이핑 방지: 마감 직전 입찰이면 마감을 뒤로 민다
        end_at = parse_time(item["auction_end_at"])
        if end_at - now <= ANTI_SNIPING_WINDOW:
            end_at = now + ANTI_SNIPING_WINDOW
            extended = True

        bid_id = str(uuid.uuid4())[:12]
        tx.execute(
            "INSERT INTO bids (id, item_id, buyer_token, buyer_name, buyer_contact, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bid_id, item_id, buyer["buyer_token"], buyer["buyer_name"], buyer["buyer_contact"], amount, fmt_time(now)),
        )
        cur = tx.execute(
            """
            UPDATE items
               SET current_price = ?, highest_bid_id = ?, bid_count = bid_count + 1,
                   auction_end_at = ?, version = version + 1
             WHERE id = ? AND version = ? AND status = 'available'
            """,
            (amount, bid_id, fmt_time(end_at), item_id, item["version"]),
        )
        if cur.rowcount != 1:
            # 읽은 뒤에 누군가 먼저 반영함 → 이 트랜잭션 전체(입찰 insert 포함) 롤백
            raise HTTPException(status_code=409, detail="다른 입찰이 먼저 반영됐어요. 다시 시도해주세요")
        updated = tx.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()

    return {"bid_id": bid_id, "extended": extended, "item": public_item(updated, now)}


@app.post("/api/items/{item_id}/buy-now")
def buy_now(item_id: str, payload: BuyerIn):
    buyer = validate_buyer(payload)
    finalize_expired_auctions()
    now = now_utc()

    with write_tx() as tx:
        row = tx.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
        item = dict(row)
        if buyer["buyer_token"] == item["seller_token"]:
            raise HTTPException(status_code=403, detail="본인 상품은 구매할 수 없어요")
        if item["status"] != "available":
            raise HTTPException(status_code=409, detail="이미 판매됐거나 예약된 상품이에요")
        if item["sale_type"] == "auction":
            if not item["buy_now_price"]:
                raise HTTPException(status_code=400, detail="즉시구매가 없는 경매예요")
            if not is_auction_open(item, now):
                raise HTTPException(status_code=409, detail="이미 종료된 경매예요")
            if item["bid_count"] > 0:
                raise HTTPException(status_code=409, detail="입찰이 시작돼서 즉시구매가 마감됐어요")
            price = item["buy_now_price"]
        else:
            price = item["price"]

        # 조건부 UPDATE: 입찰/다른 즉시구매가 먼저 반영됐다면 version이 달라져 0건 갱신
        cur = tx.execute(
            "UPDATE items SET status = 'reserved', version = version + 1 "
            "WHERE id = ? AND version = ? AND status = 'available' AND bid_count = 0",
            (item_id, item["version"]),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="한발 늦었어요. 다른 분이 먼저 구매했어요")

        order_id = str(uuid.uuid4())[:8]
        tx.execute(
            """
            INSERT INTO orders (id, item_id, buyer_token, buyer_name, buyer_contact, price, order_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'buy_now', ?)
            """,
            (order_id, item_id, buyer["buyer_token"], buyer["buyer_name"], buyer["buyer_contact"], price, fmt_time(now)),
        )

    return {
        "order_id": order_id,
        "price": price,
        "seller_name": item["seller_name"],
        "booth_number": item["booth_number"],
        "contact_link": item["contact_link"],
    }


@app.get("/api/items/{item_id}/bids")
def item_bids(item_id: str, buyer_token: Optional[str] = None):
    """
    경매 상세 화면 폴링용. 조회수를 올리지 않는다.
    입찰자 이름은 마스킹하고 연락처는 내보내지 않는다. buyer_token을 주면 '내 입찰' 여부만 표시.
    """
    finalize_expired_auctions()
    conn = get_db()
    row = conn.execute("SELECT * FROM item_view WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
    bids = conn.execute(
        "SELECT id, buyer_token, buyer_name, amount, created_at FROM bids "
        "WHERE item_id = ? ORDER BY amount DESC LIMIT 20",
        (item_id,),
    ).fetchall()
    order = conn.execute(
        "SELECT buyer_token, order_type FROM orders WHERE item_id = ? AND status != 'canceled'", (item_id,)
    ).fetchone()
    conn.close()

    mine = lambda token: bool(buyer_token) and token == buyer_token  # noqa: E731
    return {
        "item": public_item(row),
        "bids": [
            {
                "bidder": mask_name(b["buyer_name"]),
                "amount": b["amount"],
                "created_at": b["created_at"],
                "is_mine": mine(b["buyer_token"]),
            }
            for b in bids
        ],
        "i_am_highest": bool(bids) and mine(bids[0]["buyer_token"]),
        "i_won": bool(order) and mine(order["buyer_token"]),
    }


@app.get("/api/buyers/{buyer_token}/activity")
def buyer_activity(buyer_token: str):
    """내가 입찰했거나 구매한 상품 목록 + 각 상품에서의 내 상태"""
    finalize_expired_auctions()
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM item_view
         WHERE id IN (SELECT item_id FROM bids WHERE buyer_token = ?)
            OR id IN (SELECT item_id FROM orders WHERE buyer_token = ? AND status != 'canceled')
         ORDER BY created_at DESC
        """,
        (buyer_token, buyer_token),
    ).fetchall()
    now = now_utc()
    result = []
    for row in rows:
        my_max = conn.execute(
            "SELECT MAX(amount) m FROM bids WHERE item_id = ? AND buyer_token = ?", (row["id"], buyer_token)
        ).fetchone()["m"]
        highest = conn.execute(
            "SELECT buyer_token FROM bids WHERE id = ?", (row["highest_bid_id"],)
        ).fetchone() if row["highest_bid_id"] else None
        order = conn.execute(
            "SELECT buyer_token, order_type, price FROM orders WHERE item_id = ? AND status != 'canceled'",
            (row["id"],),
        ).fetchone()

        if order and order["buyer_token"] == buyer_token:
            my_state = "won" if order["order_type"] == "auction_win" else "bought"
        elif row["sale_type"] == "auction" and is_auction_open(dict(row), now):
            my_state = "leading" if highest and highest["buyer_token"] == buyer_token else "outbid"
        else:
            my_state = "lost"

        item = public_item(row, now)
        item["my_max_bid"] = my_max
        item["my_state"] = my_state  # leading | outbid | won | bought | lost
        item["my_price"] = order["price"] if my_state in ("won", "bought") else None
        result.append(item)
    conn.close()
    return result


# ---------------------------------------------------------------------------
# 피드백 (시장성 검증용 — 목표 2와 직결)
# ---------------------------------------------------------------------------
@app.post("/api/feedback")
def submit_feedback(fb: FeedbackIn):
    conn = get_db()
    fb_id = str(uuid.uuid4())[:8]
    conn.execute(
        """
        INSERT INTO feedback (id, visited_time, interest_category, revisit_intent, comment)
        VALUES (?, ?, ?, ?, ?)
        """,
        (fb_id, fb.visited_time, fb.interest_category, fb.revisit_intent, fb.comment),
    )
    conn.commit()
    conn.close()
    return {"message": "피드백이 저장되었습니다"}


@app.get("/api/stats")
def get_stats():
    """검증항목 대시보드: 재고소진율, 조회/찜, 피드백 요약"""
    conn = get_db()
    total_items = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    sold_items = conn.execute(
        "SELECT COUNT(*) c FROM items WHERE status = 'sold_out'"
    ).fetchone()["c"]
    total_views = conn.execute("SELECT SUM(views) v FROM items").fetchone()["v"] or 0
    total_likes = conn.execute("SELECT SUM(likes) l FROM items").fetchone()["l"] or 0
    feedback_count = conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"]
    avg_revisit = conn.execute(
        "SELECT AVG(revisit_intent) a FROM feedback WHERE revisit_intent IS NOT NULL"
    ).fetchone()["a"]
    auction_items = conn.execute("SELECT COUNT(*) c FROM items WHERE sale_type = 'auction'").fetchone()["c"]
    total_bids = conn.execute("SELECT COUNT(*) c FROM bids").fetchone()["c"]
    buy_now_orders = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE order_type = 'buy_now' AND status != 'canceled'"
    ).fetchone()["c"]
    auction_wins = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE order_type = 'auction_win' AND status != 'canceled'"
    ).fetchone()["c"]
    conn.close()
    return {
        "total_items": total_items,
        "sold_items": sold_items,
        "sell_through_rate": round(sold_items / total_items * 100, 1) if total_items else 0,
        "total_views": total_views,
        "total_likes": total_likes,
        "feedback_count": feedback_count,
        "avg_revisit_intent": round(avg_revisit, 1) if avg_revisit else None,
        "auction_items": auction_items,
        "total_bids": total_bids,
        "buy_now_orders": buy_now_orders,
        "auction_wins": auction_wins,
    }


# ---------------------------------------------------------------------------
# 정적 파일 서빙 (업로드 이미지 + 프론트엔드)
# ---------------------------------------------------------------------------
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@app.get("/sell")
def serve_sell(token: Optional[str] = None):
    if not token or not get_seller_by_token(token):
        return FileResponse(
            str(BASE_DIR / "static" / "sell_denied.html"), status_code=403
        )
    return FileResponse(str(BASE_DIR / "static" / "sell.html"))


@app.get("/feed")
def serve_feed():
    return FileResponse(str(BASE_DIR / "static" / "feed.html"))


@app.get("/dashboard")
def serve_dashboard():
    return FileResponse(str(BASE_DIR / "static" / "dashboard.html"))


@app.get("/admin")
def serve_admin():
    return FileResponse(str(BASE_DIR / "static" / "admin.html"))


@app.get("/my")
def serve_my():
    return FileResponse(str(BASE_DIR / "static" / "my.html"))
