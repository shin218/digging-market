"""
Digging Market — 셀러/소비자용 경량 웹 서비스
FastAPI + SQLite, 결제 기능 없음 (외부 채널 연결 방식)
"""
import os
import sqlite3
import uuid
import base64
import json
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
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
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# AI 이미지 분석 (Claude Vision) — ANTHROPIC_API_KEY 환경변수 필요
# 키가 없으면 규칙 기반 폴백으로 동작 (데모/오프라인 대비)
# ---------------------------------------------------------------------------
CATEGORY_KEYWORDS = {
    "LP": ["lp", "vinyl", "레코드", "바이닐", "앨범"],
    "카세트": ["cassette", "카세트", "테이프"],
    "의류": ["shirt", "top", "jacket", "니트", "자켓", "옷", "티셔츠"],
}


def analyze_image_with_ai(image_bytes: bytes, filename: str) -> dict:
    """
    이미지를 분석해 카테고리/제목/설명 초안을 생성한다.
    ANTHROPIC_API_KEY가 설정되어 있으면 Claude Vision을 사용하고,
    없으면 파일명 기반의 단순 규칙으로 폴백한다 (개발/데모용).
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

            prompt = (
                "이 사진은 플리마켓에서 판매할 중고 물건입니다. "
                "다음 JSON 형식으로만 답하세요 (다른 텍스트 없이):\n"
                '{"category": "LP|카세트|의류|기타 중 하나", '
                '"title": "짧은 상품명", '
                '"description": "상태와 특징을 담은 한 줄 설명 (30자 이내)"}'
            )

            message = client.messages.create(
                model="claude-sonnet-4-5",
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
                "category": result.get("category", "기타"),
                "title": result.get("title", "제목 미정"),
                "description": result.get("description", ""),
            }
        except Exception as e:
            # AI 호출 실패 시 폴백으로 진행
            print(f"[AI 분석 실패, 폴백 사용] {e}")

    # ------------------- 폴백 (규칙 기반) -------------------
    lower_name = filename.lower()
    category = "기타"
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in lower_name for kw in keywords):
            category = cat
            break
    return {
        "category": category,
        "title": "새 상품 (제목을 입력해주세요)",
        "description": "AI 자동 분석을 사용하려면 서버에 ANTHROPIC_API_KEY를 설정하세요.",
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


class SellerUpdate(BaseModel):
    seller_name: str
    contact_link: str = ""


@app.patch("/api/sellers/{token}")
def update_seller_info(token: str, payload: SellerUpdate):
    """셀러 본인이 이름/연락처만 수정 가능. 부스번호는 관리자만 변경 가능."""
    seller = get_seller_by_token(token)
    if not seller:
        raise HTTPException(status_code=403, detail="유효하지 않은 셀러 토큰입니다")
    conn = get_db()
    conn.execute(
        "UPDATE sellers SET seller_name = ?, contact_link = ? WHERE token = ?",
        (payload.seller_name, payload.contact_link, token),
    )
    conn.commit()
    conn.close()
    return get_seller_by_token(token)


# ---------------------------------------------------------------------------
# 관리자: 셀러 사전 등록 (본인만 사용 — QR 발급 전 미리 셀러 목록을 입력)
# ---------------------------------------------------------------------------
class SellerCreate(BaseModel):
    seller_name: str
    booth_number: str = ""
    contact_link: str = ""


@app.post("/api/admin/sellers")
def create_seller(payload: SellerCreate):
    token = str(uuid.uuid4())[:10]
    conn = get_db()
    conn.execute(
        "INSERT INTO sellers (token, seller_name, booth_number, contact_link) VALUES (?, ?, ?, ?)",
        (token, payload.seller_name, payload.booth_number, payload.contact_link),
    )
    conn.commit()
    conn.close()
    return {"token": token, "sell_url": f"/sell?token={token}"}


@app.get("/api/admin/sellers")
def list_sellers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM sellers ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


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
):
    seller = get_seller_by_token(token)
    if not seller:
        raise HTTPException(status_code=403, detail="유효하지 않은 셀러 토큰입니다")

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
        (id, seller_name, booth_number, category, title, description, price, image_path, contact_link)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            seller_name,
            booth_number,
            category,
            title,
            description,
            price,
            f"/uploads/{image_filename}",
            contact_link,
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
    conn = get_db()
    query = "SELECT * FROM items WHERE 1=1"
    params = []
    if category and category != "전체":
        query += " AND category = ?"
        params.append(category)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


@app.get("/api/items/{item_id}")
def get_item(item_id: str):
    conn = get_db()
    conn.execute("UPDATE items SET views = views + 1 WHERE id = ?", (item_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다")
    return dict(row)


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


@app.patch("/api/items/{item_id}/status")
def update_status(item_id: str, status: str = Form(...)):
    if status not in ("available", "sold_out"):
        raise HTTPException(status_code=400, detail="잘못된 상태값입니다")
    conn = get_db()
    conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))
    conn.commit()
    conn.close()
    return {"status": status}


# ---------------------------------------------------------------------------
# 셀러 대시보드용 집계
# ---------------------------------------------------------------------------
@app.get("/api/sellers/{seller_name}/items")
def seller_items(seller_name: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM items WHERE seller_name = ? ORDER BY created_at DESC",
        (seller_name,),
    ).fetchall()
    conn.close()
    items = [dict(row) for row in rows]
    total = len(items)
    sold = len([i for i in items if i["status"] == "sold_out"])
    return {
        "items": items,
        "summary": {
            "total": total,
            "sold_out": sold,
            "sell_through_rate": round(sold / total * 100, 1) if total else 0,
        },
    }


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
    conn.close()
    return {
        "total_items": total_items,
        "sold_items": sold_items,
        "sell_through_rate": round(sold_items / total_items * 100, 1) if total_items else 0,
        "total_views": total_views,
        "total_likes": total_likes,
        "feedback_count": feedback_count,
        "avg_revisit_intent": round(avg_revisit, 1) if avg_revisit else None,
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
