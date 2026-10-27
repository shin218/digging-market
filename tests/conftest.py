"""
테스트 공통 설정.
main.py는 import 시점에 DB를 초기화하므로, import 전에 DATA_DIR을 임시 폴더로 돌려둔다.
"""
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="digging-test-")
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


class Clock:
    """main.now_utc를 대체하는 조작 가능한 시계"""

    def __init__(self):
        self.now = datetime(2026, 11, 1, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(main, "now_utc", c)
    return c


@pytest.fixture
def client(clock):
    conn = main.get_db()
    for table in ("bids", "orders", "items", "sellers"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    return TestClient(main.app)


@pytest.fixture
def seller(client):
    res = client.post("/api/admin/sellers", json={"seller_name": "디깅레코드", "booth_number": "7", "contact_link": "digging"})
    return res.json()["token"]


def make_item(client, seller_token, **overrides):
    data = {"token": seller_token, "category": "LP", "title": "Abbey Road", "description": "", "price": "10000"}
    data.update({k: str(v) for k, v in overrides.items()})
    files = {"image": ("cover.jpg", io.BytesIO(b"\xff\xd8fake"), "image/jpeg")}
    return client.post("/api/items", data=data, files=files)


def buyer(n: int, **extra):
    body = {"buyer_token": f"buyer-token-{n:04d}-xxxxxxxx", "buyer_name": f"구매자{n}", "buyer_contact": f"insta_{n}"}
    body.update(extra)
    return body
