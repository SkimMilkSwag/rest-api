import logging
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app.main import app, _store


def test_health():
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_request_logging(caplog):
    with caplog.at_level(logging.INFO, logger="tiny_kv.access"):
        c = TestClient(app)
        assert c.get("/health").status_code == 200
        assert c.post("/kv", json={"key": "logme", "value": "v"}).status_code == 200
        assert c.get("/kv/missing").status_code == 404

    messages = [r.getMessage() for r in caplog.records]
    # one line per request: method path -> status
    assert any("GET /health -> 200" in m for m in messages), messages
    assert any("POST /kv -> 200" in m for m in messages), messages
    assert any("GET /kv/missing -> 404" in m for m in messages), messages
    _store.clear()


def test_kv_roundtrip():
    c = TestClient(app)
    assert c.post("/kv", json={"key": "a", "value": "1"}).status_code == 200
    r = c.get("/kv/a")
    assert r.status_code == 200
    assert r.json()["value"] == "1"
    assert c.delete("/kv/a").status_code == 200
    assert c.get("/kv/a").status_code == 404


def test_kv_404():
    c = TestClient(app)
    assert c.get("/kv/nope").status_code == 404


def test_list_kv():
    c = TestClient(app)
    assert c.post("/kv", json={"key": "x", "value": "1"}).status_code == 200
    assert c.post("/kv", json={"key": "y", "value": "2"}).status_code == 200
    r = c.get("/kv")
    assert r.status_code == 200
    body = r.json()
    # keys only, no values in the listing
    assert sorted(body["keys"]) == ["x", "y"]
    assert set(body.keys()) == {"keys"}
    # deletion is reflected in the listing
    assert c.delete("/kv/x").status_code == 200
    assert c.get("/kv").json()["keys"] == ["y"]


def test_ttl_entry_expires_to_404(monkeypatch):
    import app.main as m

    clock = [1000.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])

    c = TestClient(app)
    r = c.post("/kv", json={"key": "temp", "value": "gone-soon", "ttl": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] == "temp" and body["ttl"] == 5

    # within the ttl: readable, remaining ttl reported
    r = c.get("/kv/temp")
    assert r.status_code == 200
    data = r.json()
    assert data["value"] == "gone-soon"
    assert abs(data["ttl"] - 5.0) < 1e-6

    # list still includes it
    assert "temp" in c.get("/kv").json()["keys"]

    # advance the clock past expiry: GET -> 404, key purged from listing
    clock[0] = 1005.5
    assert c.get("/kv/temp").status_code == 404
    assert "temp" not in c.get("/kv").json()["keys"]


def test_ttl_boundary_not_expired_at_exact_deadline(monkeypatch):
    import app.main as m

    clock = [0.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])

    c = TestClient(app)
    assert c.post("/kv", json={"key": "edge", "value": "v", "ttl": 2}).status_code == 200
    clock[0] = 2.0  # exactly at the deadline: still alive (purge uses exp < now)
    assert c.get("/kv/edge").status_code == 200
    clock[0] = 2.0 + 1e-9  # one instant past it: gone
    assert c.get("/kv/edge").status_code == 404


def test_ttl_overwrite_replaces_expiry(monkeypatch):
    import app.main as m

    clock = [100.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])

    c = TestClient(app)
    assert c.post("/kv", json={"key": "k", "value": "v1", "ttl": 3}).status_code == 200
    # overwrite at t=105 with a longer ttl; expiry must restart from now
    assert c.post("/kv", json={"key": "k", "value": "v2", "ttl": 10}).status_code == 200

    clock[0] = 104.0  # old deadline (103) passed, new one (115) has not
    assert c.get("/kv/k").status_code == 200
    assert c.get("/kv/k").json()["value"] == "v2"

    clock[0] = 115.5
    assert c.get("/kv/k").status_code == 404


def test_no_ttl_entry_never_expires(monkeypatch):
    import app.main as m

    clock = [1000.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])

    c = TestClient(app)
    assert c.post("/kv", json={"key": "perm", "value": "forever"}).status_code == 200
    clock[0] = 1e9
    r = c.get("/kv/perm")
    assert r.status_code == 200
    # no ttl field for permanent entries (only key/value)
    assert set(r.json().keys()) == {"key", "value"}


def test_ttl_validation_rejects_non_positive(monkeypatch):
    import app.main as m

    monkeypatch.setattr(m, "_now", lambda: 0.0)
    c = TestClient(app)
    r = c.post("/kv", json={"key": "bad", "value": "v", "ttl": 0})
    assert r.status_code == 422
    r = c.post("/kv", json={"key": "bad2", "value": "v", "ttl": -5})
    assert r.status_code == 422


def test_delete_expired_key_returns_404(monkeypatch):
    import app.main as m

    clock = [10.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])

    c = TestClient(app)
    assert c.post("/kv", json={"key": "short", "value": "v", "ttl": 1}).status_code == 200
    # deleting a live key works as usual
    assert c.delete("/kv/short").status_code == 200

    assert c.post("/kv", json={"key": "short", "value": "v", "ttl": 1}).status_code == 200
    clock[0] = 10.5  # halfway through the ttl — still alive (expiry was 11.0)
    assert c.delete("/kv/short").status_code == 200

    # re-store from t=10.5: expiry is now 11.5; jump past it for the 404 check
    assert c.post("/kv", json={"key": "short", "value": "v", "ttl": 1}).status_code == 200
    clock[0] = 12.0
    # once expired, the key is gone before delete even looks — 404
    assert c.delete("/kv/short").status_code == 404
