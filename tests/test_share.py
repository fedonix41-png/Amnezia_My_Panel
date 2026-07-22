"""Integration tests for public sharing endpoints."""


class TestShareAuth:
    def test_share_auth_missing_token(self, app_client):
        r = app_client.post(
            "/api/share/nonexistent/auth",
            json={"password": "x"},
        )
        assert r.status_code == 404

    def test_share_page_not_found(self, app_client):
        r = app_client.get("/share/bogus-token")
        assert r.status_code == 404

    def test_share_rate_limit_triggers(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        # Hammer the share auth endpoint. At least some should be 429.
        statuses = []
        for _ in range(12):
            r = app_client.post(
                "/api/share/test-token/auth",
                json={"password": "x"},
            )
            statuses.append(r.status_code)

        limited = [s for s in statuses if s == 429]
        assert len(limited) >= 1, f"expected >=1 429, got statuses {statuses}"
