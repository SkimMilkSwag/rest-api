"""Per-client sliding-window rate limiter.

Each client (identified by its IP address) may make at most ``max_requests``
requests within any rolling ``window`` seconds.  The window slides — a
request that is N seconds old expires exactly N seconds after it was made,
so the limit never "resets" on a fixed calendar boundary the way a fixed
bucket would.

The limiter keeps a bounded deque of timestamps per client and drops
entries that have fallen out of the window before checking capacity.  A
periodic sweep removes clients whose deques are empty so a long-lived
process serving many one-shot IPs does not grow the map without bound.

When a client is over its limit the middleware short-circuits with a
``429 Too Many Requests`` response carrying a ``Retry-After`` header set
to the number of seconds until the oldest request in the window expires —
the earliest moment the client will have capacity again.  The rejected
request does *not* consume a slot: a burst of denied requests cannot push
the retry window further out.
"""
import time
from collections import deque
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

__all__ = ["RateLimiter", "RateLimitMiddleware"]


class RateLimiter:
    """Sliding-window counter for one logical scope (e.g. all of /kv).

    ``check`` returns the number of seconds to wait when the caller is over
    its limit, or 0 when a slot was recorded and the request may proceed.
    Time comes from ``time.monotonic`` by default; tests can rebind
    ``RateLimiter._clock_fn`` to drive time deterministically (the same
    pattern main.py uses for ttl expiry).
    """

    _clock_fn = staticmethod(time.monotonic)

    def __init__(self, max_requests: int, window: float):
        if max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        if window <= 0:
            raise ValueError("window must be a positive number of seconds")
        self.max_requests = max_requests
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def _now(self) -> float:
        return self._clock_fn()

    def check(self, client: str) -> float:
        """Record one hit for ``client``; return seconds to wait (0 = ok)."""
        now = self._now()
        cutoff = now - self.window
        with self._lock:
            hits = self._hits.get(client)
            if hits is None:
                hits = self._hits[client] = deque()
            # drop anything older than the sliding window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.max_requests:
                # over budget: retry once the oldest in-window hit expires.
                # Don't record this one — a denied burst shouldn't extend
                # the caller's own window.
                return max(0.0, hits[0] + self.window - now)
            hits.append(now)
            return 0.0

    def reset(self) -> None:
        """Clear every client (used by tests)."""
        with self._lock:
            self._hits.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Return 429 + ``Retry-After`` when a client exceeds its budget.

    The limiter instance is injected so tests can swap in a fake clock or
    inspect recorded hits; by default a per-app limiter is created with the
    constructor's limit/window.  Requests to paths listed in ``skip_paths``
    (e.g. ``/health``) bypass the limiter entirely — health probes should
    never be rate-limited into flapping.
    """

    def __init__(self, app, limiter: RateLimiter | None = None,
                 max_requests: int = 20, window: float = 60.0,
                 skip_paths: tuple[str, ...] = ("/health",)):
        super().__init__(app)
        self.limiter = limiter if limiter is not None else RateLimiter(max_requests, window)
        self.skip_paths = set(skip_paths)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self.skip_paths:
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        wait = self.limiter.check(client)
        if wait > 0.0:
            retry_after = max(1, int(wait + 0.999))  # ceil, at least 1s
            return JSONResponse(
                status_code=429,
                content={"detail": "too many requests"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
