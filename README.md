# rest-api

A deliberately minimal **FastAPI** reference service: an in-memory key-value store
with `health`, put, get, and delete endpoints, plus a test suite and a Dockerfile.
The goal is a clean, readable starting point for a real API — not a framework tour.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then hit it:

```bash
curl localhost:8000/health
curl -X POST localhost:8000/kv -H 'Content-Type: application/json' -d '{"key":"a","value":"1"}'
curl localhost:8000/kv/a

# optional TTL (seconds): the key 404s once it expires
curl -X POST localhost:8000/kv -H 'Content-Type: application/json' -d '{"key":"session","value":"x","ttl":3600}'

# list all stored keys (values omitted, insertion order)
curl localhost:8000/kv

# bare value, no {"key","value"} wrapper — handy for piping into jq/scripts
curl 'localhost:8000/kv/a?raw=1'
```

### Bare-value reads (`?raw=1`)

`GET /kv/{key}?raw=1` returns just the stored value, JSON-encoded, without
the `{"key": ..., "value": ...}` wrapper (and no `ttl` field) — so
`curl 'localhost:8000/kv/a?raw=1' | jq .` parses straight to the value.
Strings stay quoted (they're JSON), objects and arrays pass through
unchanged, and a missing/expired key still 404s as usual.

### Idempotent delete (`?purge=1`)

`DELETE /kv/{key}?purge=1` returns 200 even when the key is already gone
(stale TTL, already deleted): the entry is dropped from the store if present,
so `/stats` item counts stay accurate after a retry. Without the flag, a
missing key is `404`. A purge that found nothing still reports
`{"deleted": ..., "purged": true}` to distinguish it from a real delete:

```bash
curl -X DELETE 'localhost:8000/kv/session?purge=1'   # 200 whether or not it existed
```

Interactive docs are at `http://localhost:8000/docs`.

## Docker

```bash
docker build -t tiny-kv .
docker run -p 8000:8000 tiny-kv
```

Or one command via compose (includes a `/health` healthcheck):

```bash
docker compose up --build -d
docker inspect --format '{{.State.Health.Status}}' tiny-kv   # -> healthy
```

## Tests

```bash
python -m pytest tests/ -v
```

## Endpoints

| Method | Path        | Description                     |
|--------|-------------|---------------------------------|
| GET    | /health     | service status                  |
| POST   | /kv         | store key/value (optional ttl)  |
| GET    | /kv         | list stored keys                |
| GET    | /kv/{key}   | fetch a value                   |
| PATCH  | /kv/{key}   | shallow-merge into an object    |
| DELETE | /kv/{key}   | remove a key                    |
| GET    | /stats      | store size + op counters        |

Every request is rate-limited per client IP (20 req / 60 s sliding window);
over-budget calls get `429` with a `Retry-After` header. `/health` is exempt
so health probes never flap. The limit and window are set in `app/main.py`.

## License

MIT — see [LICENSE](LICENSE).
