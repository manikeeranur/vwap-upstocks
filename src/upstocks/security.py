"""Local dashboard boundaries; CSRF protection is not multi-user authentication."""
import hmac
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


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
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
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
