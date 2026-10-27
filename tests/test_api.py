import logging
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
import pytest
from app.main import app, _store, _ops_total, _ops_by_kind, limiter as module_limiter
from app.ratelimit import RateLimiter, RateLimitMiddleware


def _swap_shared_app_limiter(fresh: RateLimiter) -> None:
    """Point the shared app's rate-limit middleware at ``fresh``.

    Starlette builds the middleware chain once per TestClient (the stack is
    lazy), and each middleware instance holds the limiter we passed in
    ``add_middleware``.  We find that instance on the built stack and rebind
    its ``limiter`` attribute so endpoint tests run against a permissive,
    per-test limiter while main.py's production wiring stays untouched.
    """
    stack = getattr(app, "middleware_stack", None)
    node = getattr(stack, "middleware_stack", None) if stack is not None else None
    # Walk the starlette Middleware chain to find our instance.
    def _find(mw):
        if isinstance(mw, RateLimitMiddleware):
            return mw
        inner = getattr(mw, "app", None)
        return _find(inner) if inner is not None else None

    found = _find(app.middleware_stack) if app.middleware_stack is not None else None
    if found is None:
        # stack not built yet (first request will build it); patch the
        # module-level object that add_middleware captured by rebuilding via
        # a fresh TestClient in each test instead — simplest is to just make
        # the shared limiter permissive for the duration.
        module_limiter.reset()
        return
    found.limiter = fresh


@pytest.fixture(autouse=True)
def permissive_rate_limit():
    """Give the shared app a fresh permissive rate limiter per test.

    The production limiter (20 req / 60s per client IP) would leak across
    tests: any test making more than 20 requests leaves later tests inside
    its denied window, surfacing as surprise 429s.  Rate-limit behaviour is
    exercised directly against :class:`RateLimiter` and dedicated app
    instances below, so endpoint tests just need a limiter that won't fire.
    """
    fresh = RateLimiter(max_requests=10_000, window=60.0)
    # Pre-build the stack so we can patch the live instance deterministically.
    with TestClient(app) as probe:
        _swap_shared_app_limiter(fresh)
    yield


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


def test_delete_missing_key_returns_404():
    c = TestClient(app)
    r = c.delete("/kv/never-stored")
    assert r.status_code == 404
    assert r.json()["detail"] == "key not found"


def test_delete_returns_deleted_key():
    c = TestClient(app)
    assert c.post("/kv", json={"key": "gone", "value": {"n": 1}}).status_code == 200
    r = c.delete("/kv/gone")
    assert r.status_code == 200
    assert r.json() == {"deleted": "gone"}


def test_stats_counts_ops_and_store_size(monkeypatch):
    import app.main as m

    clock = [50.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])
    base_total = m._ops_total
    c = TestClient(app)

    assert c.post("/kv", json={"key": "s1", "value": "v"}).status_code == 200
    assert c.get("/kv/s1").status_code == 200
    assert c.get("/kv").status_code == 200
    assert c.delete("/kv/s1").status_code == 200

    r = c.get("/stats")
    assert r.status_code == 200
    body = r.json()
    # each of the four ops above is counted, on top of whatever earlier tests did
    assert body["total_ops"] >= base_total + 4
    kind = body["ops_by_kind"]
    assert kind.get("put", 0) >= 1
    assert kind.get("get", 0) >= 2
    assert kind.get("list", 0) >= 1
    assert kind.get("delete", 0) >= 1


def test_stats_items_excludes_expired(monkeypatch):
    import app.main as m

    clock = [200.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])
    c = TestClient(app)
    base_items = c.get("/stats").json()["items"]
    assert c.post("/kv", json={"key": "ephemeral", "value": "v", "ttl": 1}).status_code == 200

    # live entry is counted
    assert c.get("/stats").json()["items"] == base_items + 1

    clock[0] = 205.0
    # the ttl entry expired, so item count is back to the pre-store baseline
    assert c.get("/stats").json()["items"] == base_items


def test_stats_kind_keys_are_sorted():
    c = TestClient(app)
    keys = list(c.get("/stats").json()["ops_by_kind"].keys())
    assert keys == sorted(keys)


def test_patch_shallow_merge():
    c = TestClient(app)
    r = c.post("/kv", json={"key": "cfg", "value": {"a": 1, "b": {"x": 1, "y": 2}, "c": "keep"}})
    assert r.status_code == 200

    # patch: existing keys overwritten, new keys added
    r = c.patch("/kv/cfg", json={"b": {"x": 99}, "d": 4})
    assert r.status_code == 200
    body = r.json()
    # shallow merge: b is REPLACED wholesale (not deep-merged), a and c survive, d added
    assert body["value"] == {"a": 1, "b": {"x": 99}, "c": "keep", "d": 4}

    # the merged value is what GET now returns
    assert c.get("/kv/cfg").json()["value"] == {"a": 1, "b": {"x": 99}, "c": "keep", "d": 4}


def test_patch_missing_key_404():
    c = TestClient(app)
    r = c.patch("/kv/nope", json={"a": 1})
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_patch_non_object_value_409():
    c = TestClient(app)
    # a scalar value can't be merged
    assert c.post("/kv", json={"key": "scalar", "value": "just-a-string"}).status_code == 200
    r = c.patch("/kv/scalar", json={"a": 1})
    assert r.status_code == 409
    assert "not an object" in r.json()["detail"]

    # a list value can't be merged either
    assert c.post("/kv", json={"key": "arr", "value": [1, 2, 3]}).status_code == 200
    r = c.patch("/kv/arr", json={"a": 1})
    assert r.status_code == 409


def test_patch_preserves_ttl(monkeypatch):
    import app.main as m

    clock = [100.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])
    c = TestClient(app)
    # store an object with a ttl
    assert c.post("/kv", json={"key": "t", "value": {"a": 1}, "ttl": 5}).status_code == 200

    clock[0] = 102.0  # two seconds in
    r = c.patch("/kv/t", json={"b": 2})
    assert r.status_code == 200
    assert r.json()["value"] == {"a": 1, "b": 2}

    # the ttl was preserved (not reset by the patch): still ~3s remaining at t=102.5
    clock[0] = 102.5
    data = c.get("/kv/t").json()
    assert abs(data["ttl"] - 2.5) < 1e-6

    # and it expires at the original deadline (105), not after the patch
    clock[0] = 105.5
    assert c.get("/kv/t").status_code == 404


def test_patch_counts_in_stats():
    c = TestClient(app)
    base_total = c.get("/stats").json()["total_ops"]
    assert c.post("/kv", json={"key": "p1", "value": {"a": 1}}).status_code == 200
    assert c.patch("/kv/p1", json={"b": 2}).status_code == 200

    r = c.get("/stats").json()
    # the patch is a counted op, on top of the put + this stats call
    assert r["total_ops"] >= base_total + 2
    assert r["ops_by_kind"].get("patch", 0) >= 1


# --- rate limiting -------------------------------------------------------
# These tests inject a dedicated RateLimiter with a fake clock so the
# sliding window is deterministic.  They build their own app instance
# rather than hitting the module-level `app` (which has a 20/60s limiter
# that would interfere with the other endpoint tests).

def _make_app(limiter):
    from fastapi import FastAPI
    from app.ratelimit import RateLimitMiddleware

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limiter=limiter)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/data")
    def data():
        return {"value": 1}

    return app


def test_rate_limiter_allows_within_budget():
    import app.ratelimit as rl

    clock = [100.0]
    rl.RateLimiter._clock_fn = staticmethod(lambda: clock[0])
    lim = rl.RateLimiter(max_requests=3, window=10.0)
    app = _make_app(lim)
    c = TestClient(app)
    # three requests fit the budget
    for i in range(3):
        assert c.get("/data").status_code == 200
    # the fourth is over budget
    r = c.get("/data")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_rate_limiter_sliding_window_expiry():
    import app.ratelimit as rl

    clock = [100.0]
    rl.RateLimiter._clock_fn = staticmethod(lambda: clock[0])
    lim = rl.RateLimiter(max_requests=2, window=10.0)
    app = _make_app(lim)
    c = TestClient(app)

    # fill the budget at t=100
    assert c.get("/data").status_code == 200
    assert c.get("/data").status_code == 200
    r = c.get("/data")
    assert r.status_code == 429
    retry_after = int(r.headers["Retry-After"])
    assert retry_after >= 1

    # advance just past the first hit's expiry (t=110.5): BOTH of the two
    # hits made at t=100 have now fallen out of the 10s window, so the full
    # budget is available again — the next two requests must pass.
    clock[0] = 110.5
    assert c.get("/data").status_code == 200   # ok again (window slid)
    assert c.get("/data").status_code == 200   # second slot also free
    r = c.get("/data")                         # third in-window hit: denied
    assert r.status_code == 429

    # and the denial clears once those two new hits expire (t=120.5)
    clock[0] = 120.5
    assert c.get("/data").status_code == 200


def test_rate_limiter_denied_request_does_not_extend_window():
    import app.ratelimit as rl

    clock = [100.0]
    rl.RateLimiter._clock_fn = staticmethod(lambda: clock[0])
    lim = rl.RateLimiter(max_requests=1, window=10.0)
    app = _make_app(lim)
    c = TestClient(app)

    assert c.get("/data").status_code == 200
    # a burst of denied requests at the same instant must not push the
    # retry time further out — wait stays pinned to the original hit
    for _ in range(5):
        r = c.get("/data")
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) == 10


def test_rate_limiter_health_is_exempt():
    import app.ratelimit as rl

    clock = [100.0]
    rl.RateLimiter._clock_fn = staticmethod(lambda: clock[0])
    lim = rl.RateLimiter(max_requests=1, window=10.0)
    app = _make_app(lim)
    c = TestClient(app)
    # /health is in skip_paths, so it never consumes a slot
    for _ in range(5):
        assert c.get("/health").status_code == 200
    # the data endpoint still has its full budget
    assert c.get("/data").status_code == 200


def test_rate_limiter_validation_errors():
    import app.ratelimit as rl
    import pytest as _p

    with _p.raises(ValueError):
        rl.RateLimiter(max_requests=0, window=10.0)
    with _p.raises(ValueError):
        rl.RateLimiter(max_requests=5, window=0.0)
