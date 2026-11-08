"""Request logging middleware: one line per request to stdout.

The access-log line carries the request id (``X-Request-Id``) when one is
available — :class:`app.requestid.RequestIdMiddleware` stashes it on
``request.state.request_id`` before this logger runs, so the id appears at
the end of every log line for easy correlation with client-side tracing.
Requests that bypass the request-id middleware (or arrive before it) fall
back to ``-``.
"""
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("tiny_kv.access")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status code, duration, and request id for every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000.0
        rid = getattr(request.state, "request_id", None) or "-"
        logger.info(
            "%s %s -> %d (%.1f ms) [%s]",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            rid,
        )
        return response
