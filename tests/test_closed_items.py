"""
예약중·판매완료 상품: 목록에는 남아 있되 다른 고객은 입찰/구매할 수 없어야 한다.
"""
from conftest import buyer, make_item

FEED_STATUSES = {"status": "available,reserved,sold_out"}


def test_feed_keeps_reserved_and_sold_items_in_status_order(client, seller, clock):
    sold = make_item(client, seller, title="판매완료 상품").json()["id"]
    reserved = make_item(client, seller, title="예약 상품").json()["id"]
    unsold = make_item(client, seller, title="유찰 경매", sale_type="auction", auction_minutes=10).json()["id"]
    available = make_item(client, seller, title="판매중 상품").json()["id"]

    client.post(f"/api/items/{reserved}/buy-now", json=buyer(1))
    client.post(f"/api/items/{sold}/buy-now", json=buyer(2))
    client.patch(f"/api/items/{sold}/status", json={"token": seller, "status": "sold_out"})
    clock.advance(minutes=11)  # 입찰 없던 경매 → 유찰

    items = client.get("/api/items", params=FEED_STATUSES).json()
    assert [i["id"] for i in items] == [available, reserved, sold]  # 판매중 → 예약중 → 판매완료, 유찰 제외
    assert unsold not in [i["id"] for i in items]
    assert [i["buy_now_available"] for i in items] == [True, False, False]


def test_default_listing_still_returns_only_available(client, seller):
    reserved = make_item(client, seller).json()["id"]
    make_item(client, seller)
    client.post(f"/api/items/{reserved}/buy-now", json=buyer(1))
    assert [i["status"] for i in client.get("/api/items").json()] == ["available"]


def test_category_filter_combined_with_statuses(client, seller):
    lp = make_item(client, seller, category="LP").json()["id"]
    make_item(client, seller, category="카세트")
    client.post(f"/api/items/{lp}/buy-now", json=buyer(1))
    items = client.get("/api/items", params={**FEED_STATUSES, "category": "LP"}).json()
    assert [(i["id"], i["status"]) for i in items] == [(lp, "reserved")]


def test_reserved_and_sold_fixed_items_cannot_be_bought(client, seller):
    item_id = make_item(client, seller).json()["id"]
    assert client.post(f"/api/items/{item_id}/buy-now", json=buyer(1)).status_code == 200
    assert client.post(f"/api/items/{item_id}/buy-now", json=buyer(2)).status_code == 409  # 예약중

    client.patch(f"/api/items/{item_id}/status", json={"token": seller, "status": "sold_out"})
    assert client.post(f"/api/items/{item_id}/buy-now", json=buyer(3)).status_code == 409  # 판매완료


def test_seller_marked_reserved_item_cannot_be_bought(client, seller):
    """현장에서 셀러가 직접 '예약중'으로 바꾼 상품도 앱으로 구매 불가"""
    item_id = make_item(client, seller).json()["id"]
    client.patch(f"/api/items/{item_id}/status", json={"token": seller, "status": "reserved"})
    assert client.post(f"/api/items/{item_id}/buy-now", json=buyer(1)).status_code == 409


def test_won_or_completed_auction_rejects_bids_and_buy_now(client, seller, clock):
    item_id = make_item(client, seller, sale_type="auction", auction_minutes=10, buy_now_price=50000).json()["id"]
    client.post(f"/api/items/{item_id}/bids", json=buyer(1, amount=10000))
    clock.advance(minutes=11)

    listed = {i["id"]: i for i in client.get("/api/items", params=FEED_STATUSES).json()}
    assert listed[item_id]["status"] == "reserved"
    assert listed[item_id]["auction_open"] is False
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(2, amount=99000)).status_code == 409
    assert client.post(f"/api/items/{item_id}/buy-now", json=buyer(2)).status_code == 409

    client.patch(f"/api/items/{item_id}/status", json={"token": seller, "status": "sold_out"})
    assert client.post(f"/api/items/{item_id}/bids", json=buyer(2, amount=99000)).status_code == 409


def test_buyer_can_recognize_own_purchase_from_feed(client, seller):
    item_id = make_item(client, seller).json()["id"]
    client.post(f"/api/items/{item_id}/buy-now", json=buyer(1))
    mine = client.get(f"/api/items/{item_id}/bids", params={"buyer_token": buyer(1)["buyer_token"]}).json()
    other = client.get(f"/api/items/{item_id}/bids", params={"buyer_token": buyer(2)["buyer_token"]}).json()
    assert mine["i_won"] is True
    assert other["i_won"] is False
