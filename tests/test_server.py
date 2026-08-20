"""Tests for the HTTP transport: guard clauses and the broker-mode ASGI app.

Unlike ami-mcp (where broker mode is an optional extra and its transport
tests skip via ``pytest.importorskip("af_credentials")`` when it is not
installed), jupyterlab-mcp has af-credentials as a hard dependency -- its
only auth mode is broker-issued -- so these tests always run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import af_credentials.verifier as af_verifier
import kubernetes.config as k8s_config
import pytest
from starlette.testclient import TestClient

from jupyterlab_mcp.config import Settings
from jupyterlab_mcp.server import _make_broker_app, serve

if TYPE_CHECKING:
    from collections.abc import Iterator

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class TestServeGuards:
    def test_missing_broker_url_exits(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            serve(broker_url=None)
        assert excinfo.value.code == 1


class TestBrokerModeApp:
    @pytest.fixture
    def broker_client(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        async def fake_verify(_self: object, token: str) -> object | None:
            if token == "good-token":
                claims: object = af_verifier.BrokerClaims(
                    sub="kratsg",
                    jti="test-jti",
                    exp=4102444800,
                    uid=1000,
                    gid=1000,
                    unixname="kratsg",
                )
                return claims
            return None

        monkeypatch.setattr(af_verifier.BrokerTokenVerifier, "verify", fake_verify)
        monkeypatch.setattr(k8s_config, "load_incluster_config", lambda: None)

        app = _make_broker_app(
            jwks_url="http://broker.invalid/.well-known/jwks.json",
            issuer="http://broker.invalid",
            audience="jupyterlab-mcp",
            resource_url="http://127.0.0.1:8000",
            host="127.0.0.1",
            settings=Settings(),
        )
        with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
            yield test_client

    def test_healthz_needs_no_auth(self, broker_client: TestClient) -> None:
        response = broker_client.get("/healthz")
        assert response.status_code == 200

    def test_initialize_with_unknown_token_is_401(
        self, broker_client: TestClient
    ) -> None:
        response = broker_client.post(
            "/mcp",
            json=_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401

    def test_initialize_with_broker_token(self, broker_client: TestClient) -> None:
        response = broker_client.post(
            "/mcp",
            json=_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": "Bearer good-token"},
        )
        assert response.status_code == 200
        assert "serverInfo" in response.text
