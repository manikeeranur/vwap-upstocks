"""ASGI request tracing, including streamed responses and failures."""
import asyncio
import logging
import time
import uuid

from .observability import exception_details, request_id

access = logging.getLogger("upstocks.access")
log = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        token = request_id.set(rid)
        start, status = time.perf_counter(), 500
        failure = None

        async def traced_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                headers.extend([(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                                (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                                (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; form-action 'self'")])
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        except Exception as exc:
            failure = exception_details(exc)
            log.exception("Unhandled request error")
            raise
        finally:
            # Record route templates, never query strings, headers, or bodies.
            route = getattr(scope.get("route"), "path", "unmatched")
            details = {"method": scope["method"], "route": route, "status_code": status,
                       "duration_ms": round((time.perf_counter() - start) * 1000, 2)}
            access.info("Request completed", extra=details)
            if failure:
                details["exception"] = failure
            state = scope["app"].state
            if getattr(state, "ready", False):
                try:
                    await asyncio.to_thread(state.repository.event, "http", "Request failed" if status >= 500 else "Request completed",
                                            component="http", level="ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO", details=details)
                except Exception:
                    state.audit_failures += 1
                    log.exception("Unable to persist HTTP activity; file log retained")
            request_id.reset(token)
