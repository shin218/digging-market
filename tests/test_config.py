import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def test_config_without_key_disables_analytics(monkeypatch):
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    assert client.get("/api/config").json() == {"posthog_key": "", "posthog_host": ""}


def test_config_with_key_uses_default_host(monkeypatch):
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    assert client.get("/api/config").json() == {
        "posthog_key": "phc_test",
        "posthog_host": "https://us.i.posthog.com",
    }


def test_config_rejects_non_https_host(monkeypatch):
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("POSTHOG_HOST", "http://evil.example")
    assert client.get("/api/config").json()["posthog_key"] == ""


def test_pages_load_analytics_but_admin_does_not():
    for path in ["/", "/feed", "/my"]:
        assert "/static/analytics.js" in client.get(path).text
    assert "/static/analytics.js" not in client.get("/admin").text
