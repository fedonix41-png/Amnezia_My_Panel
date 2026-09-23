"""Integration tests for public sharing endpoints."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app
from routers import sharing


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


class TestMyConnectionConfig:
    @pytest.mark.asyncio
    async def test_returns_config_and_closes_ssh(self, monkeypatch):
        async def run_inline(func, *args):
            return func(*args)

        user = SimpleNamespace(id="user-1")
        server = SimpleNamespace(id=7, host="vpn.example.com")
        connection = SimpleNamespace(
            id="connection-1",
            user_id=user.id,
            server=server,
            protocol="awg",
            client_id="client-1",
        )
        protocol = SimpleNamespace(port=32123)

        connection_result = Mock()
        connection_result.scalar_one_or_none.return_value = connection
        protocol_result = Mock()
        protocol_result.scalar_one_or_none.return_value = protocol
        db = SimpleNamespace(execute=AsyncMock(side_effect=[connection_result, protocol_result]))

        ssh = SimpleNamespace(connect=Mock(), disconnect=Mock())
        manager = object()
        monkeypatch.setattr(sharing.asyncio, "to_thread", run_inline)
        monkeypatch.setattr(sharing.app_module, "get_current_user", AsyncMock(return_value=user))
        monkeypatch.setattr(sharing.app_module, "get_ssh", Mock(return_value=ssh))
        monkeypatch.setattr(
            sharing.app_module, "get_protocol_manager", Mock(return_value=manager)
        )
        manager_call = Mock(return_value="generated config")
        monkeypatch.setattr(sharing.app_module, "_manager_call", manager_call)
        monkeypatch.setattr(
            sharing.app_module, "generate_vpn_link", Mock(return_value="vpn://generated")
        )
        monkeypatch.setattr(
            sharing.app_module, "generate_qr_content", Mock(return_value="qr-content")
        )

        response = await sharing.api_my_connection_config(
            request=object(), connection_id=connection.id, db=db
        )

        assert response == {
            "config": "generated config",
            "vpn_link": "vpn://generated",
            "qr_content": "qr-content",
        }
        ssh.connect.assert_called_once_with()
        ssh.disconnect.assert_called_once_with()
        manager_call.assert_called_once_with(
            manager,
            "get_client_config",
            "awg",
            "client-1",
            "vpn.example.com",
            32123,
        )

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated_user(self, monkeypatch):
        monkeypatch.setattr(
            sharing.app_module, "get_current_user", AsyncMock(return_value=None)
        )
        db = SimpleNamespace(execute=AsyncMock())

        response = await sharing.api_my_connection_config(
            request=object(), connection_id="connection-1", db=db
        )

        assert response.status_code == 403
        db.execute.assert_not_awaited()
