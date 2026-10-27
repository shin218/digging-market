"""
경매/즉시구매 테스트.
규칙 검증 + 동시성(동시에 여러 명이 입찰/구매할 때 결과가 하나로 수렴하는지) + 기존 DB 마이그레이션.

실행: pytest -q
"""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import main
from conftest import buyer, make_item


# ---------------------------------------------------------------------------
# 고정가 상품 즉시구매
# ---------------------------------------------------------------------------
def test_fixed_item_buy_now_reserves_and_exposes_buyer_only_to_seller(client, seller):
    item_id = make_item(client, seller, price=15000).json()["id"]

    res = client.post(f"/api/items/{item_id}/buy-now", json=buyer(1))
    assert res.status_code == 200
    assert res.json()["price"] == 15000

    # 두 번째 구매자는 실패
    res2 = client.post(f"/api/items/{item_id}/buy-now", json=buyer(2))
    assert res2.status_code == 409

    # 피드(판매중 목록)에서 빠지고, 셀러 화면에는 구매자 연락처가 보인다
    assert client.get("/api/items").json() == []
    mine = client.get(f"/api/sellers/{seller}/items").json()[0]
    assert mine["status"] == "reserved"
    assert mine["order"]["buyer_contact"] == "insta_1"
    assert mine["order"]["order_type"] == "buy_now"


def test_public_responses_never_leak_seller_token(client, seller):
    item_id = make_item(client, seller).json()["id"]
    listed = client.get("/api/items").json()[0]
    detail = client.get(f"/api/items/{item_id}").json()
    for payload in (listed, detail):
        assert "seller_token" not in payload
        assert "version" not in payload


def test_seller_relisting_fixed_item_cancels_order(client, seller):
    item_id = make_item(client, seller).json()["id"]
    client.post(f"/api/items/{item_id}/buy-now", json=buyer(1))

    res = client.patch(f"/api/items/{item_id}/status", json={"token": seller, "status": "available"})
    assert res.status_code == 200
    # 거래 취소 → 다른 사람이 다시 살 수 있다
    assert client.post(f"/api/items/{item_id}/buy-now", json=buyer(2)).status_code == 200


# ---------------------------------------------------------------------------
# 경매 등록 검증
# ---------------------------------------------------------------------------
def test_auction_creation_validation(client, seller):
    assert make_item(client, seller, sale_type="auction").status_code == 400  # 기간 없음
    assert make_item(client, seller, sale_type="auction", auction_minutes=7).status_code == 400  # 허용 안 된 기간
    assert (
        make_item(client, seller, sale_type="auction", auction_minutes=60, buy_now_price=10000).status_code == 400
    )  # 즉시구매가 <= 시작가
    assert make_item(client, seller, sale_type="bogus").status_code == 400
    assert make_item(client, seller, sale_type="auction", auction_minutes=60, buy_now_price=30000).status_code == 200


# ---------------------------------------------------------------------------
# 입찰 규칙
# ---------------------------------------------------------------------------
def test_bid_rules_and_buy_now_closes_after_first_bid(client, seller):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=60, buy_now_price=30000).json()["id"]

    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["buy_now_available"] is True
    assert detail["min_next_bid"] == 10000

    # 시작가 미만 거절
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=9000)).status_code == 409
    # 첫 입찰부터 즉시구매가 이상이면 즉시구매로 유도
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=30000)).status_code == 400

    res = client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000))
    assert res.status_code == 200
    state = res.json()["item"]
    assert state["display_price"] == 10000
    assert state["min_next_bid"] == 11000  # 1만원대 최소 단위 1,000원
    assert state["buy_now_available"] is False

    # 첫 입찰 이후 즉시구매 불가
    assert client.post(f"/api/items/{item_id}/buy-now", json=buyer(2)).status_code == 409
    # 최소 단위 미달 거절
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(2, amount=10500)).status_code == 409
    # 첫 입찰 이후에는 즉시구매가를 넘는 입찰도 가능
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(2, amount=35000)).status_code == 200


def test_fixed_item_rejects_bids(client, seller):
    item_id = make_item(client, seller).json()["id"]
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=20000)).status_code == 400


def test_invalid_buyer_info_rejected(client, seller):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=60).json()["id"]
    res = client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000, buyer_name="   "))
    assert res.status_code == 400
    res = client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000, buyer_token="short"))
    assert res.status_code == 400


def test_bid_history_masks_names_and_hides_contacts(client, seller):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=60).json()["id"]
    client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000))
    client.post(f"/api/items/{item_id}/bids", json=buyer(2, amount=11000))

    me = buyer(1)["buyer_token"]
    data = client.get(f"/api/items/{item_id}/bids", params={"buyer_token": me}).json()
    assert [b["amount"] for b in data["bids"]] == [11000, 10000]
    assert data["bids"][0]["bidder"] == "구***"  # '구매자2' → 첫 글자만 노출
    assert data["bids"][1]["is_mine"] is True
    assert data["i_am_highest"] is False
    assert "insta" not in str(data)
    assert "buyer-token" not in str(data)


# ---------------------------------------------------------------------------
# 마감 처리 (lazy close) / 스나이핑 방지
# ---------------------------------------------------------------------------
def test_anti_sniping_extends_end_time(client, seller, clock):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=10).json()["id"]
    original_end = client.get(f"/api/items/{item_id}").json()["auction_end_at"]

    clock.advance(minutes=9)  # 마감 1분 전
    res = client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000)).json()
    assert res["extended"] is True
    assert res["item"]["auction_end_at"] > original_end
    assert res["item"]["auction_end_at"] == "2026-11-01 12:12:00"  # 입찰 시각(12:09) + 3분

    clock.advance(minutes=2, seconds=30)  # 원래 마감은 지났지만 연장된 마감 전
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(2, amount=11000)).status_code == 200


def test_expired_auction_goes_to_highest_bidder(client, seller, clock):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=30).json()["id"]
    client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000))
    client.post(f"/api/items/{item_id}/bids", json=buyer(2, amount=12000))

    clock.advance(minutes=31)
    # 마감 이후 입찰은 거절
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(3, amount=50000)).status_code == 409

    mine = client.get(f"/api/sellers/{seller}/items").json()[0]
    assert mine["status"] == "reserved"
    assert mine["order"]["buyer_contact"] == "insta_2"
    assert mine["order"]["price"] == 12000
    assert mine["order"]["order_type"] == "auction_win"

    won = client.get(f"/api/buyers/{buyer(2)['buyer_token']}/activity").json()[0]
    lost = client.get(f"/api/buyers/{buyer(1)['buyer_token']}/activity").json()[0]
    assert (won["my_state"], won["my_price"]) == ("won", 12000)
    assert lost["my_state"] == "lost"


def test_expired_auction_without_bids_is_unsold(client, seller, clock):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=10).json()["id"]
    clock.advance(minutes=11)
    assert client.get(f"/api/items/{item_id}").json()["status"] == "unsold"
    conn = main.get_db()
    assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0
    conn.close()


def test_activity_shows_leading_and_outbid(client, seller):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=60).json()["id"]
    client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000))
    client.post(f"/api/items/{item_id}/bids", json=buyer(2, amount=11000))
    assert client.get(f"/api/buyers/{buyer(1)['buyer_token']}/activity").json()[0]["my_state"] == "outbid"
    assert client.get(f"/api/buyers/{buyer(2)['buyer_token']}/activity").json()[0]["my_state"] == "leading"


# ---------------------------------------------------------------------------
# 셀러 측 제약
# ---------------------------------------------------------------------------
def test_seller_cannot_tamper_with_running_auction(client, seller):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=60).json()["id"]
    client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000))

    res = client.patch(f"/api/items/{item_id}/status", json={"token": seller, "status": "sold_out"})
    assert res.status_code == 400
    res = client.patch(
        f"/api/items/{item_id}",
        json={"token": seller, "category": "LP", "title": "x", "description": "", "price": 5000},
    )
    assert res.status_code == 400
    assert client.delete(f"/api/items/{item_id}", params={"token": seller}).status_code == 400

    # 제목/설명 수정은 가능
    res = client.patch(
        f"/api/items/{item_id}",
        json={"token": seller, "category": "LP", "title": "Abbey Road (UK)", "description": "", "price": 10000},
    )
    assert res.status_code == 200


def test_won_auction_can_be_marked_sold_but_not_relisted(client, seller, clock):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=10).json()["id"]
    client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000))
    clock.advance(minutes=20)
    client.get("/api/items")

    assert client.patch(f"/api/items/{item_id}/status", json={"token": seller, "status": "available"}).status_code == 400
    assert client.patch(f"/api/items/{item_id}/status", json={"token": seller, "status": "sold_out"}).status_code == 200
    mine = client.get(f"/api/sellers/{seller}/items").json()[0]
    assert mine["order"]["status"] == "completed"


# ---------------------------------------------------------------------------
# 동시성
# ---------------------------------------------------------------------------
def _run_concurrently(n, fn):
    """n개의 요청을 최대한 같은 순간에 출발시킨다"""
    barrier = threading.Barrier(n)

    def task(i):
        c = TestClient(main.app)
        barrier.wait()
        return fn(c, i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(task, range(n)))


def _db_scalar(sql, *params):
    conn = main.get_db()
    value = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return value


def test_concurrent_buy_now_only_one_wins(client, seller):
    item_id = make_item(client, seller).json()["id"]
    codes = _run_concurrently(30, lambda c, i: c.post(f"/api/items/{item_id}/buy-now", json=buyer(i)).status_code)

    assert codes.count(200) == 1
    assert codes.count(409) == 29
    assert _db_scalar("SELECT COUNT(*) FROM orders WHERE item_id = ?", item_id) == 1


def test_concurrent_same_amount_bids_only_one_accepted(client, seller):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=60).json()["id"]
    codes = _run_concurrently(
        20, lambda c, i: c.post(f"/api/items/{item_id}/bids", json=buyer(i, amount=10000)).status_code
    )
    assert codes.count(200) == 1
    assert _db_scalar("SELECT COUNT(*) FROM bids WHERE item_id = ?", item_id) == 1
    assert _db_scalar("SELECT bid_count FROM items WHERE id = ?", item_id) == 1


def test_concurrent_rising_bids_keep_invariants(client, seller):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=60).json()["id"]
    # 서로 다른 금액으로 동시에 입찰 — 순서에 따라 일부는 '최소 단위 미달'로 거절될 수 있다
    codes = _run_concurrently(
        25, lambda c, i: c.post(f"/api/items/{item_id}/bids", json=buyer(i, amount=10000 + i * 1000)).status_code
    )
    accepted = codes.count(200)
    assert accepted >= 1
    assert set(codes) <= {200, 409}

    # 불변식: 저장된 입찰 수 == 성공 응답 수 == bid_count, 현재가 == 최고 입찰가, highest_bid_id가 그 입찰을 가리킴
    assert _db_scalar("SELECT COUNT(*) FROM bids WHERE item_id = ?", item_id) == accepted
    assert _db_scalar("SELECT bid_count FROM items WHERE id = ?", item_id) == accepted
    max_bid = _db_scalar("SELECT MAX(amount) FROM bids WHERE item_id = ?", item_id)
    assert _db_scalar("SELECT current_price FROM items WHERE id = ?", item_id) == max_bid
    assert _db_scalar(
        "SELECT b.amount FROM items i JOIN bids b ON b.id = i.highest_bid_id WHERE i.id = ?", item_id
    ) == max_bid
    # 받아들여진 입찰은 시간 순으로 항상 증가해야 한다
    conn = main.get_db()
    amounts = [r[0] for r in conn.execute("SELECT amount FROM bids WHERE item_id = ? ORDER BY rowid", (item_id,))]
    conn.close()
    assert amounts == sorted(amounts) and len(set(amounts)) == len(amounts)


def test_concurrent_bid_vs_buy_now_never_both(client, seller):
    for _ in range(5):  # 경쟁 상황을 여러 번 반복
        item_id = make_item(client, seller, sale_type="auction", auction_minutes=60, buy_now_price=50000).json()["id"]

        def act(c, i):
            if i % 2 == 0:
                return ("buy", c.post(f"/api/items/{item_id}/buy-now", json=buyer(i)).status_code)
            return ("bid", c.post(f"/api/items/{item_id}/bids", json=buyer(i, amount=10000 + i * 1000)).status_code)

        results = _run_concurrently(20, act)
        buys = [code for kind, code in results if kind == "buy" and code == 200]
        bids = [code for kind, code in results if kind == "bid" and code == 200]

        # 즉시구매가 성공했다면 그 전에 들어간 입찰이 없어야 하고, 이후 입찰도 없어야 한다
        assert len(buys) <= 1
        if buys:
            assert bids == []
            assert _db_scalar("SELECT status FROM items WHERE id = ?", item_id) == "reserved"
        else:
            assert len(bids) >= 1
        assert _db_scalar("SELECT COUNT(*) FROM orders WHERE item_id = ?", item_id) == len(buys)


def test_concurrent_lazy_close_creates_single_order(client, seller, clock):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=10).json()["id"]
    client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000))
    clock.advance(minutes=11)

    _run_concurrently(20, lambda c, i: c.get("/api/items").status_code)
    assert _db_scalar("SELECT COUNT(*) FROM orders WHERE item_id = ?", item_id) == 1


# ---------------------------------------------------------------------------
# 기존 운영 DB 마이그레이션
# ---------------------------------------------------------------------------
def test_migrates_existing_database(tmp_path, monkeypatch):
    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.execute(
        """
        CREATE TABLE items (
            id TEXT PRIMARY KEY, seller_token TEXT NOT NULL, seller_name TEXT NOT NULL, booth_number TEXT,
            category TEXT NOT NULL, title TEXT NOT NULL, description TEXT, price INTEGER NOT NULL,
            image_path TEXT, status TEXT DEFAULT 'available', likes INTEGER DEFAULT 0, views INTEGER DEFAULT 0,
            contact_link TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO items (id, seller_token, seller_name, category, title, price) "
        "VALUES ('old1', 'tok', '셀러', 'LP', '옛날 상품', 5000)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(main, "DB_PATH", old_db)
    main.init_db()
    main.init_db()  # 두 번 실행해도 안전해야 함

    conn = main.get_db()
    row = dict(conn.execute("SELECT * FROM items WHERE id = 'old1'").fetchone())
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert row["sale_type"] == "fixed"
    assert row["bid_count"] == 0
    assert row["title"] == "옛날 상품"
    assert {"bids", "orders"} <= tables
