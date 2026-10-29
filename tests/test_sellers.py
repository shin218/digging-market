"""
셀러 중복 체크 테스트
- 부스 번호, 인스타 아이디: 중복이면 저장 불가 (409)
- 셀러명: 중복 허용, 경고만 (warnings)
"""
import itertools
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import main


_auto_booth = itertools.count(500)


def create(client, name, booth=None, insta=""):
    """부스는 필수라서, 테스트가 부스를 신경 쓰지 않을 땐 겹치지 않는 번호를 자동으로 채운다"""
    if booth is None:
        booth = f"Z{next(_auto_booth)}"
    return client.post("/api/admin/sellers", json={"seller_name": name, "booth_number": booth, "contact_link": insta})


def test_same_name_is_allowed_with_warning(client):
    assert create(client, "민지의 레코드샵").json()["warnings"] == []
    res = create(client, " 민지의레코드샵 ")  # 띄어쓰기만 다른 같은 이름
    assert res.status_code == 200
    assert len(res.json()["warnings"]) == 1
    assert "같은 이름의 셀러" in res.json()["warnings"][0]


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


def test_empty_instagram_is_not_duplicate(client):
    assert create(client, "셀러A").status_code == 200
    assert create(client, "셀러B").status_code == 200


def test_reports_every_blocking_field_only(client):
    create(client, "민지샵", booth="1", insta="minji")
    res = create(client, "민지샵", booth="1번", insta="@MINJI")
    detail = res.json()["detail"]
    assert res.status_code == 409
    assert "부스 번호" in detail and "인스타그램" in detail
    assert "같은 이름" not in detail  # 이름 중복은 차단 사유가 아님


def test_stores_normalized_instagram(client):
    token = create(client, "셀러A", insta="@Minji.Records").json()["token"]
    assert main.get_seller_by_token(token)["contact_link"] == "minji.records"


def test_check_endpoint_for_live_validation(client):
    create(client, "민지샵", booth="1", insta="minji")
    conflicts = client.get("/api/admin/sellers/check", params={"booth_number": "1번"}).json()["conflicts"]
    assert [(c["field"], c["blocking"]) for c in conflicts] == [("booth_number", True)]
    assert conflicts[0]["existing_seller"] == "민지샵"
    name_conflicts = client.get("/api/admin/sellers/check", params={"seller_name": "민지 샵"}).json()["conflicts"]
    assert [(c["field"], c["blocking"]) for c in name_conflicts] == [("seller_name", False)]
    assert client.get("/api/admin/sellers/check", params={"seller_name": "새 셀러"}).json()["conflicts"] == []


def test_check_endpoint_excludes_self_when_editing(client):
    token = create(client, "민지샵", booth="1", insta="minji").json()["token"]
    params = {"seller_name": "민지샵", "booth_number": "1", "contact_link": "minji", "exclude_token": token}
    assert client.get("/api/admin/sellers/check", params=params).json()["conflicts"] == []


def test_seller_self_edit_rules(client):
    create(client, "민지샵", insta="minji")
    token = create(client, "수아샵", insta="sua").json()["token"]

    # 다른 셀러와 같은 이름: 허용 + 경고
    res = client.patch(f"/api/sellers/{token}", json={"seller_name": "민지 샵", "contact_link": "sua"})
    assert res.status_code == 200
    assert res.json()["warnings"]
    # 다른 셀러의 인스타: 차단
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
        return create(c, f"동시등록샵{i}", booth="100").status_code  # 같은 부스로 동시에

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
