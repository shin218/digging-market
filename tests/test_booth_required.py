"""
부스 번호: 셀러 등록 시 필수 + 중복 불가, 부스 없는 셀러는 상품 등록 불가
관리자는 셀러의 모든 항목을 수정할 수 있고, 셀러 정보는 상품 화면에 바로 반영된다(JOIN)
"""
import main
from conftest import make_item


def create(client, name, booth, insta=""):
    return client.post("/api/admin/sellers", json={"seller_name": name, "booth_number": booth, "contact_link": insta})


def legacy_seller_without_booth(token="legacy-01", name="옛날셀러"):
    """부스 필수화 이전에 부스 없이 등록된 셀러를 흉내 낸다"""
    conn = main.get_db()
    conn.execute("INSERT INTO sellers (token, seller_name, booth_number) VALUES (?, ?, '')", (token, name))
    conn.commit()
    conn.close()
    return token


def test_booth_is_required_on_registration(client):
    for booth in ("", "   ", "번"):
        res = create(client, "새 셀러", booth)
        assert res.status_code == 400, repr(booth)
        assert "부스 번호" in res.json()["detail"]
    assert create(client, "새 셀러", "3").status_code == 200


def test_booth_cannot_be_duplicated(client):
    assert create(client, "셀러A", "3").status_code == 200
    assert create(client, "셀러B", "3번").status_code == 409


def test_seller_without_booth_cannot_create_items(client):
    token = legacy_seller_without_booth()
    files_before = set(main.UPLOAD_DIR.iterdir())
    res = make_item(client, token)
    assert res.status_code == 403
    assert "부스 번호가 배정되지 않아" in res.json()["detail"]
    assert set(main.UPLOAD_DIR.iterdir()) == files_before  # 거절된 요청의 이미지는 저장되지 않음


def admin_edit(client, token, name, booth, insta=""):
    return client.patch(f"/api/admin/sellers/{token}", json={"seller_name": name, "booth_number": booth, "contact_link": insta})


def test_admin_assigns_booth_then_seller_can_create_items(client):
    token = legacy_seller_without_booth()
    res = admin_edit(client, token, "옛날셀러", "8")
    assert res.status_code == 200
    assert res.json()["booth_number"] == "8"
    assert make_item(client, token).status_code == 200


def test_admin_edit_validates_every_field(client):
    create(client, "셀러A", "1", insta="seller_a")
    token = create(client, "셀러B", "2", insta="seller_b").json()["token"]
    assert admin_edit(client, token, "셀러B", "1번", "seller_b").status_code == 409   # 부스 중복
    assert admin_edit(client, token, "셀러B", "2", "@Seller_A").status_code == 409    # 인스타 중복
    assert admin_edit(client, token, "셀러B", " ", "seller_b").status_code == 400      # 부스 필수
    assert admin_edit(client, token, " ", "2", "seller_b").status_code == 400          # 이름 필수
    assert admin_edit(client, "nope", "x", "9").status_code == 404
    res = admin_edit(client, token, "셀러A", "2", "seller_b")                          # 이름 중복은 경고만
    assert res.status_code == 200 and res.json()["warnings"]


def test_admin_can_change_every_field(client):
    token = create(client, "셀러A", "4", insta="old_id").json()["token"]
    res = admin_edit(client, token, "새 이름", "4번", "@New_ID")  # 자기 부스를 다른 표기로 저장해도 중복 아님
    assert res.status_code == 200
    body = res.json()
    assert (body["seller_name"], body["booth_number"], body["contact_link"]) == ("새 이름", "4번", "new_id")


def test_seller_info_changes_show_up_on_existing_items(client):
    token = create(client, "셀러A", "2", insta="seller_a").json()["token"]
    item_id = make_item(client, token).json()["id"]

    admin_edit(client, token, "셀러A", "11", "seller_a")                          # 관리자: 부스 변경
    client.patch(f"/api/sellers/{token}", json={"seller_name": "이름 바꾼 셀러", "contact_link": "renamed"})  # 셀러 본인

    detail = client.get(f"/api/items/{item_id}").json()
    listed = client.get("/api/items").json()[0]
    mine = client.get(f"/api/sellers/{token}/items").json()[0]
    for payload in (detail, listed, mine):
        assert (payload["seller_name"], payload["booth_number"], payload["contact_link"]) == ("이름 바꾼 셀러", "11", "renamed")


def test_buy_now_response_uses_current_seller_contact(client):
    token = create(client, "셀러A", "2", insta="before").json()["token"]
    item_id = make_item(client, token).json()["id"]
    client.patch(f"/api/sellers/{token}", json={"seller_name": "셀러A", "contact_link": "after"})
    res = client.post(f"/api/items/{item_id}/buy-now",
                      json={"buyer_token": "buyer-token-0001-xxxxxxxx", "buyer_name": "a", "buyer_contact": "a"})
    assert res.json()["contact_link"] == "after"
