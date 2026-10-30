"""FastAPI application: a tiny in-memory key-value store with health + echo endpoints."""
import json
import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.middleware import RequestLoggingMiddleware
from app.ratelimit import RateLimitMiddleware, RateLimiter

app = FastAPI(title="tiny-kv", version="0.1.0")

# sliding-window rate limiter: 20 requests / 60s per client IP; /health is
# exempt so health probes don't flap. Tests replace this instance via the
# module-level `limiter` global below.
limiter = RateLimiter(max_requests=20, window=60.0)
app.add_middleware(RateLimitMiddleware, limiter=limiter)
app.add_middleware(RequestLoggingMiddleware)

# make the access log visible by default (stderr via logging.basicConfig)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


class KV(BaseModel):
    key: str
    value: Any  # any JSON value (string, object, list, number, ...); objects support PATCH
    ttl: Optional[int] = None  # optional expiry in seconds; omit for a permanent entry


# key -> (value, expires_at). value is any JSON value; expires_at is None for permanent entries.
_store: dict[str, tuple[Any, Optional[float]]] = {}

# request counters since boot; mutated by the counting wrapper below
_ops_total = 0
_ops_by_kind: dict[str, int] = {}


def _bump(kind: str):
    """Record one kv operation (put/get/delete/list/patch) for /stats."""
    global _ops_total
    _ops_total += 1
    _ops_by_kind[kind] = _ops_by_kind.get(kind, 0) + 1


def _now() -> float:
    """Clock used for expiry bookkeeping.

    Delegates to ``_clock_fn`` so tests can swap in a fake clock by rebinding
    that single module global — the endpoints call this function (not
    ``time.monotonic`` directly), and rebinding ``_clock_fn`` takes effect
    immediately because every call resolves it through this module's globals.
    """
    return _clock_fn()


_clock_fn = time.monotonic  # tests: monkeypatch m._clock_fn to control time


def _purge_expired():
    """Drop entries whose ttl has elapsed. Called lazily on reads and deletes."""
    t = _now()
    expired = [k for k, (_, exp) in list(_store.items()) if exp is not None and exp < t]
    for k in expired:
        del _store[k]


@app.get("/health")
def health():
    _purge_expired()
    return {"status": "ok", "items": len(_store)}


@app.post("/kv")
def put(item: KV):
    _bump("put")
    if item.ttl is not None and item.ttl <= 0:
        raise HTTPException(status_code=422, detail="ttl must be a positive integer (seconds)")
    expires_at = _now() + item.ttl if item.ttl is not None else None
    _store[item.key] = (item.value, expires_at)
    return {"stored": item.key, "ttl": item.ttl}


@app.get("/kv")
def list_kv():
    """Return all stored keys (insertion order, values omitted). Expired entries are purged first."""
    _bump("list")
    _purge_expired()
    return {"keys": list(_store.keys())}


@app.get("/kv/{key}")
def get(key: str, raw: bool = Query(False, description="return the bare value as JSON without the {\"key\",\"value\"} wrapper (for scripting)")):
    _bump("get")
    _purge_expired()
    if key not in _store:
        raise HTTPException(status_code=404, detail="key not found")
    value, expires_at = _store[key]
    if raw:
        # The body IS the value — JSON-encoded (strings stay quoted), so
        # `curl ...?raw=1 | jq` parses straight to it. No wrapper, no ttl.
        return Response(content=json.dumps(value), media_type="application/json")
    out = {"key": key, "value": value}
    if expires_at is not None:
        out["ttl"] = max(0.0, expires_at - _now())
    return out


@app.delete("/kv/{key}")
def delete(key: str, purge: bool = Query(False, description="succeed even if the key is already gone (or expired): removes it from the store if present so /stats reflects the removal")):
    _bump("delete")
    _purge_expired()
    if key not in _store:
        if purge:
            # Idempotent delete: the caller wanted the key gone, and it is.
            return {"deleted": key, "purged": True}
        raise HTTPException(status_code=404, detail="key not found")
    del _store[key]
    return {"deleted": key}


@app.patch("/kv/{key}")
def patch(key: str, body: dict):
    """Shallow-merge `body` into the stored value at `key`.

    The stored value must be a JSON object (dict): top-level keys in `body`
    replace any existing ones, and new keys are added — but nested objects are
    REPLACED wholesale, not merged (hence "shallow"). Patching a missing key
    is 404; patching a non-object value is 409. The entry's ttl (if any) is
    preserved.
    """
    _bump("patch")
    _purge_expired()
    if key not in _store:
        raise HTTPException(status_code=404, detail="key not found")
    value, expires_at = _store[key]
    if not isinstance(value, dict):
        raise HTTPException(status_code=409, detail="stored value is not an object; cannot merge")
    merged = {**value, **body}  # shallow: body wins on key conflicts, nested replaced
    _store[key] = (merged, expires_at)
    return {"key": key, "value": merged}


@app.get("/stats")
def stats():
    """Store size + total kv ops since boot, broken down by kind (put/get/list/delete)."""
    _purge_expired()
    return {
        "items": len(_store),
        "total_ops": _ops_total,
        "ops_by_kind": dict(sorted(_ops_by_kind.items())),
    }
