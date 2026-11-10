import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from app.main import app


def test_patch_non_object_detail_lists_value_shape():
    """The 409 detail names the actual value type, so a client knows what it hit."""
    c = TestClient(app)
    assert c.post("/kv", json={"key": "num", "value": 7}).status_code == 200
    r = c.patch("/kv/num", json={"a": 1})
    assert r.status_code == 409
    assert "int" in r.json()["detail"]

    # a list value is named too — not lumped into some generic 'scalar'
    assert c.post("/kv", json={"key": "lst", "value": [1, 2]}).status_code == 200
    r = c.patch("/kv/lst", json={"a": 1})
    assert r.status_code == 409
    assert "list" in r.json()["detail"]


def test_patch_null_body_is_noop():
    """PATCH with an empty JSON object merges nothing but still returns 200."""
    c = TestClient(app)
    assert c.post("/kv", json={"key": "noop", "value": {"a": 1}}).status_code == 200
    r = c.patch("/kv/noop", json={})
    assert r.status_code == 200
    assert r.json()["value"] == {"a": 1}


def test_patch_null_value_in_body_wins():
    """An explicit null in the patch body overwrites an existing key."""
    c = TestClient(app)
    assert c.post("/kv", json={"key": "nv", "value": {"a": 1, "b": 2}}).status_code == 200
    r = c.patch("/kv/nv", json={"a": None})
    assert r.status_code == 200
    assert r.json()["value"] == {"a": None, "b": 2}


def test_patch_non_object_string_detail():
    """A string value is named as 'string' in the 409 detail."""
    c = TestClient(app)
    assert c.post("/kv", json={"key": "s", "value": "just-a-string"}).status_code == 200
    r = c.patch("/kv/s", json={"a": 1})
    assert r.status_code == 409
    assert "str" in r.json()["detail"]


def test_patch_expired_key_404(monkeypatch):
    """A key whose ttl elapsed is gone before the patch runs — same as any other read."""
    import app.main as m

    clock = [100.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])
    c = TestClient(app)
    assert c.post("/kv", json={"key": "epx", "value": {"a": 1}, "ttl": 2}).status_code == 200
    clock[0] = 105.0
    r = c.patch("/kv/epx", json={"b": 2})
    assert r.status_code == 404
    assert r.json()["detail"] == "key not found"


def test_patch_null_body_preserves_ttl(monkeypatch):
    """A no-op merge still preserves the existing expiry, like a real patch does."""
    import app.main as m

    clock = [100.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])
    c = TestClient(app)
    assert c.post("/kv", json={"key": "noop2", "value": {"a": 1}, "ttl": 5}).status_code == 200
    clock[0] = 101.0
    r = c.patch("/kv/noop2", json={})
    assert r.status_code == 200
    # ttl preserved (not reset by the patch): still ~4s remaining at t=101.5
    clock[0] = 101.5
    data = c.get("/kv/noop2").json()
    assert abs(data["ttl"] - 3.5) < 1e-6
    # and it expires at the original deadline (105), not after the patch
    clock[0] = 105.5
    assert c.get("/kv/noop2").status_code == 404


def test_patch_null_body_counts_as_op():
    """A no-op merge is still a counted op, on top of whatever was there before."""
    c = TestClient(app)
    base_total = c.get("/stats").json()["total_ops"]
    assert c.post("/kv", json={"key": "noop3", "value": {"a": 1}}).status_code == 200
    assert c.patch("/kv/noop3", json={}).status_code == 200
    r = c.get("/stats").json()
    assert r["total_ops"] >= base_total + 2
    assert r["ops_by_kind"].get("patch", 0) >= 1
