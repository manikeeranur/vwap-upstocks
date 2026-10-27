"""Dashboard boundaries: trusted hosts, CSRF token on commands, and per-person sign-in."""
import hmac
from urllib.parse import quote, urlsplit

from starlette.responses import JSONResponse, RedirectResponse

from .accounts import SESSION_COOKIE

# Reachable without signing in. The Upstox notifier is Upstox's server posting a token (verified separately).
PUBLIC_PATHS = frozenset({"/login", "/api/login", "/health/live", "/health/ready", "/broker/upstox/notify", "/favicon.ico"})
# Server-to-server calls that can't carry the dashboard's command token.
NO_CSRF_PATHS = frozenset({"/broker/upstox/notify"})


class DashboardGuard:
    def __init__(self, app, allowed_hosts):
        self.app = app
        self.allowed_hosts = frozenset(allowed_hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v.decode("latin1") for k, v in scope.get("headers", [])}

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

        try:
            host = urlsplit("http://" + headers.get(b"host", ""))
            if host.hostname not in self.allowed_hosts or host.username or host.password or host.path or host.query or host.fragment:
                return await reject(400, "Untrusted dashboard host")
            host.port  # validate the port as well
        except ValueError:
            return await reject(400, "Invalid dashboard host")
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"} and scope.get("path") not in NO_CSRF_PATHS:
            expected = getattr(scope["app"].state, "dashboard_token", "")
            supplied = headers.get(b"x-app-token", "")
            if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                return await reject(403, "Dashboard session expired. Reload the page before trying again.")
            origin = headers.get(b"origin")
            actual = f"{scope['scheme']}://{headers[b'host']}"
            if origin and origin != actual or headers.get(b"sec-fetch-site") == "cross-site":
                return await reject(403, "Cross-origin dashboard actions are not allowed")
            # All dashboard mutations are small JSON commands. Buffer a bounded
            # body so chunked requests cannot bypass the Content-Length check.
            if headers.get(b"content-type", "").split(";", 1)[0].lower() != "application/json":
                return await reject(415, "Use application/json for dashboard actions")
            data = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                data.extend(message.get("body", b""))
                if len(data) > 1_048_576:
                    return await reject(413, "Request body is too large")
                if not message.get("more_body", False):
                    break
            original_receive = receive
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(data), "more_body": False}
                return await original_receive()

            receive = replay
        await self.app(scope, receive, send)


class SessionAuth:
    """Every page and API call belongs to a signed-in person (request.state.user_id)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        signer = getattr(scope["app"].state, "signer", None)
        cookie = ""
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin1").split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == SESSION_COOKIE:
                        cookie = value
        user_id = signer.verify(cookie, "s") if signer else None
        if user_id is None:
            user_id = getattr(scope["app"].state, "auto_user", None)
        scope.setdefault("state", {})["user_id"] = user_id
        if user_id is None and scope["path"] not in PUBLIC_PATHS:
            if scope["method"] == "GET" and not scope["path"].startswith("/api/"):
                target = scope["path"] + ("?" + scope["query_string"].decode() if scope.get("query_string") else "")
                return await RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)(scope, receive, send)
            return await JSONResponse({"detail": "Sign in to continue."}, status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)
