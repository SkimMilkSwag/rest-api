"""Request-id middleware: echo an X-Request-Id back on every response.

If the client sends ``X-Request-Id``, that value is echoed verbatim (useful
when a gateway already assigns one and you want to correlate logs end to
end).  When the header is absent, the middleware generates a fresh uuid4 so
every response carries *some* id — handy for scripting and for correlating
the access log line emitted by :class:`RequestLoggingMiddleware` with a
specific client request.

The id is stashed on ``request.state.request_id`` so downstream code (endpoints,
error handlers) can read it without re-parsing headers.
"""
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

HEADER = "X-Request-Id"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Ensure every response carries an ``X-Request-Id`` header."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get(HEADER) or str(uuid.uuid4())
        request.state.request_id = rid
        response = await call_next(request)
        response.headers[HEADER] = rid
        return response
