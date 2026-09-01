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
