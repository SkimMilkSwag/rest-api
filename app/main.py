"""FastAPI application: a tiny in-memory key-value store with health + echo endpoints."""
import logging
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.middleware import RequestLoggingMiddleware

app = FastAPI(title="tiny-kv", version="0.1.0")
app.add_middleware(RequestLoggingMiddleware)

# make the access log visible by default (stderr via logging.basicConfig)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


class KV(BaseModel):
    key: str
    value: str
    ttl: Optional[int] = None  # optional expiry in seconds; omit for a permanent entry


# key -> (value, expires_at). expires_at is None for entries stored without a ttl.
_store: dict[str, tuple[str, Optional[float]]] = {}


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
    if item.ttl is not None and item.ttl <= 0:
        raise HTTPException(status_code=422, detail="ttl must be a positive integer (seconds)")
    expires_at = _now() + item.ttl if item.ttl is not None else None
    _store[item.key] = (item.value, expires_at)
    return {"stored": item.key, "ttl": item.ttl}


@app.get("/kv")
def list_kv():
    """Return all stored keys (insertion order, values omitted). Expired entries are purged first."""
    _purge_expired()
    return {"keys": list(_store.keys())}


@app.get("/kv/{key}")
def get(key: str):
    _purge_expired()
    if key not in _store:
        raise HTTPException(status_code=404, detail="key not found")
    value, expires_at = _store[key]
    out = {"key": key, "value": value}
    if expires_at is not None:
        out["ttl"] = max(0.0, expires_at - _now())
    return out


@app.delete("/kv/{key}")
def delete(key: str):
    _purge_expired()
    if key not in _store:
        raise HTTPException(status_code=404, detail="key not found")
    del _store[key]
    return {"deleted": key}
