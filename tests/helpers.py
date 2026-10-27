from fastapi.testclient import TestClient


class DashboardClient(TestClient):
    """Represent a same-origin browser that has loaded the dashboard token."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        super().__init__(*args, **kwargs)

    def __enter__(self):
        super().__enter__()
        self.headers["X-App-Token"] = self.app.state.dashboard_token
        # Signed in as person 1 (created by the schema migration).
        from upstocks.accounts import SESSION_COOKIE
        self.cookies.set(SESSION_COOKIE, self.app.state.signer.session(1))
        return self
