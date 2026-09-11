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
```

Interactive docs are at `http://localhost:8000/docs`.

## Docker

```bash
docker build -t tiny-kv .
docker run -p 8000:8000 tiny-kv
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
| DELETE | /kv/{key}   | remove a key                    |
| GET    | /stats      | store size + op counters        |

## License

MIT — see [LICENSE](LICENSE).
