"""FastAPI application: a tiny in-memory key-value store with health + echo endpoints."""
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.middleware import RequestLoggingMiddleware

app = FastAPI(title="tiny-kv", version="0.1.0")
app.add_middleware(RequestLoggingMiddleware)

# make the access log visible by default (stderr via logging.basicConfig)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_store: dict[str, str] = {}


class KV(BaseModel):
    key: str
    value: str


@app.get("/health")
def health():
    return {"status": "ok", "items": len(_store)}


@app.post("/kv")
def put(item: KV):
    _store[item.key] = item.value
    return {"stored": item.key}


@app.get("/kv")
def list_kv():
    """Return all stored keys (insertion order, values omitted)."""
    return {"keys": list(_store.keys())}


@app.get("/kv/{key}")
def get(key: str):
    if key not in _store:
        raise HTTPException(status_code=404, detail="key not found")
    return {"key": key, "value": _store[key]}


@app.delete("/kv/{key}")
def delete(key: str):
    if key not in _store:
        raise HTTPException(status_code=404, detail="key not found")
    del _store[key]
    return {"deleted": key}
