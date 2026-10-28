"""
셀러 중복 체크 테스트 — 이름 / 부스 번호 / 인스타 아이디
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import main


def create(client, name, booth="", insta=""):
    return client.post("/api/admin/sellers", json={"seller_name": name, "booth_number": booth, "contact_link": insta})


def test_blocks_same_name_ignoring_spaces_and_case(client):
    assert create(client, "민지의 레코드샵").status_code == 200
    res = create(client, " 민지의레코드샵 ")
    assert res.status_code == 409
    assert "셀러 이름" in res.json()["detail"]
    assert create(client, "Vinyl Shop").status_code == 200
    assert create(client, "vinylshop").status_code == 409


def test_blocks_same_booth_with_different_notation(client):
    assert create(client, "셀러A", booth="7").status_code == 200
    res = create(client, "셀러B", booth="7번")
    assert res.status_code == 409
    assert "부스 번호" in res.json()["detail"]
    assert create(client, "셀러C", booth=" a-3 ").status_code == 200
    assert create(client, "셀러D", booth="A-3").status_code == 409


def test_blocks_same_instagram_in_any_form(client):
    assert create(client, "셀러A", insta="minji.records").status_code == 200
    for variant in ("@Minji.Records", "https://instagram.com/minji.records/", "www.instagram.com/minji.records?igsh=x"):
        res = create(client, "셀러B", insta=variant)
        assert res.status_code == 409, variant
        assert "인스타그램" in res.json()["detail"]


def test_empty_booth_and_instagram_are_not_duplicates(client):
    assert create(client, "셀러A").status_code == 200
    assert create(client, "셀러B").status_code == 200


def test_reports_every_conflicting_field(client):
    create(client, "민지샵", booth="1", insta="minji")
    res = create(client, "민지샵", booth="1번", insta="@MINJI")
    detail = res.json()["detail"]
    assert res.status_code == 409
    assert "셀러 이름" in detail and "부스 번호" in detail and "인스타그램" in detail


def test_stores_normalized_instagram(client):
    token = create(client, "셀러A", insta="@Minji.Records").json()["token"]
    assert main.get_seller_by_token(token)["contact_link"] == "minji.records"


def test_check_endpoint_for_live_validation(client):
    create(client, "민지샵", booth="1", insta="minji")
    conflicts = client.get("/api/admin/sellers/check", params={"booth_number": "1번"}).json()["conflicts"]
    assert [c["field"] for c in conflicts] == ["booth_number"]
    assert conflicts[0]["existing_seller"] == "민지샵"
    assert client.get("/api/admin/sellers/check", params={"seller_name": "새 셀러"}).json()["conflicts"] == []


def test_seller_self_edit_cannot_take_others_name_or_instagram(client):
    create(client, "민지샵", insta="minji")
    token = create(client, "수아샵", insta="sua").json()["token"]

    assert client.patch(f"/api/sellers/{token}", json={"seller_name": "민지 샵", "contact_link": "sua"}).status_code == 409
    assert client.patch(f"/api/sellers/{token}", json={"seller_name": "수아샵", "contact_link": "@MINJI"}).status_code == 409
    # 자기 자신의 기존 값으로 저장하는 건 중복이 아님
    res = client.patch(f"/api/sellers/{token}", json={"seller_name": "수아샵", "contact_link": "@sua"})
    assert res.status_code == 200
    assert res.json()["contact_link"] == "sua"


def test_concurrent_duplicate_registration_only_one_succeeds(client):
    barrier = threading.Barrier(10)

    def register(i):
        c = TestClient(main.app)
        barrier.wait()
        return create(c, "동시등록샵", booth=str(100 + i)).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(register, range(10)))
    assert codes.count(200) == 1
    assert codes.count(409) == 9


def test_list_flags_preexisting_duplicates(client):
    # 중복 체크 도입 전에 들어간 데이터를 흉내 내서 직접 INSERT
    conn = main.get_db()
    conn.execute("INSERT INTO sellers (token, seller_name, booth_number) VALUES ('t1', '옛날샵', '5')")
    conn.execute("INSERT INTO sellers (token, seller_name, booth_number) VALUES ('t2', '다른샵', '5번')")
    conn.commit()
    conn.close()
    flagged = {s["token"]: s["duplicate_fields"] for s in client.get("/api/admin/sellers").json()}
    assert flagged["t1"] == ["booth_number"]
    assert flagged["t2"] == ["booth_number"]
