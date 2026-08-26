import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app.main import app


def test_health():
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


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
