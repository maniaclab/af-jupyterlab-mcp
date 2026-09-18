"""Tests for the HTTP transport: guard clauses and the broker-mode ASGI app.

Unlike ami-mcp (where broker mode is an optional extra and its transport
tests skip via ``pytest.importorskip("af_credentials")`` when it is not
installed), af-jupyterlab-mcp has af-credentials as a hard dependency -- its
only auth mode is broker-issued -- so these tests always run.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import af_credentials.verifier as af_verifier
import kubernetes.config as k8s_config
import pytest
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from af_jupyterlab_mcp.config import Settings
from af_jupyterlab_mcp.server import _make_broker_app, _register_all, serve

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

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


class TestEveryToolDeclaresAnnotationsAndOutputSchema:
    """Drift guard: every registered tool (both modules) must publish
    read-only annotations and an outputSchema.

    This is the one test that would catch a future tool being added (or an
    existing one refactored) without following the
    ``Annotated[CallToolResult, Model]`` + ``ToolAnnotations`` pattern all
    22 tools use today -- see CLAUDE.md's "Tool registration pattern".
    """

    def test_every_tool_declares_annotations_and_output_schema(self) -> None:
        mcp = MCPServer("test")
        _register_all(mcp)
        tools = mcp._tool_manager.list_tools()
        assert len(tools) == 22, [t.name for t in tools]
        for tool in tools:
            assert tool.annotations is not None, tool.name
            assert tool.annotations.read_only_hint is not None, tool.name
            assert tool.output_schema is not None, tool.name


@pytest.fixture
def broker_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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
        audience="af-jupyterlab-mcp",
        resource_url="http://127.0.0.1:8000",
        host="127.0.0.1",
        settings=Settings(),
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


class TestBrokerModeApp:
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


@asynccontextmanager
async def _unauthenticated_test_lifespan(
    _server: MCPServer,
) -> AsyncGenerator[dict[str, Any], None]:
    """A lifespan for the wire-level test app below: no real broker/k8s wiring.

    Safe only because the tool exercised over the wire
    (``list_supported_images``) never calls ``get_broker_claims`` or touches
    the k8s clients -- it only reads ``settings``.
    """
    yield {"broker_verifier": None, "k8s_clients": None, "settings": Settings()}


class TestToolsOverTheWire:
    """Wire-level assertions that bypass every unit test's tool.fn shortcut.

    Every existing tool test calls the raw callable directly, which never
    goes through the mcp SDK's serialization -- none of them would notice a
    missing ``annotations``/``outputSchema`` on the wire. These do, via a
    real JSON-RPC round trip through a built ASGI app (see A.4/A.1 of the
    interop plan). This uses a dedicated, unauthenticated app (rather than
    the broker-mode app above) so the round trip isn't entangled with
    ``stateless_http``/broker-auth wiring that's already covered by
    ``TestBrokerModeApp``; ``list_supported_images`` is exercised for
    ``tools/call`` because it is the one tool that touches neither the
    broker verifier nor the k8s clients.
    """

    @pytest.fixture
    def client(self) -> Iterator[TestClient]:
        mcp = MCPServer("test", lifespan=_unauthenticated_test_lifespan)
        _register_all(mcp)
        app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True)
        with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
            yield test_client

    def _session_headers(self, client: TestClient) -> dict[str, str]:
        init_resp = client.post("/mcp", json=_INITIALIZE, headers=_MCP_HEADERS)
        session_id = init_resp.headers["mcp-session-id"]
        headers = {**_MCP_HEADERS, "mcp-session-id": session_id}
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        return headers

    def test_tools_list_carries_annotations_and_output_schema(
        self, client: TestClient
    ) -> None:
        headers = self._session_headers(client)
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert resp.status_code == 200
        tools = {tool["name"]: tool for tool in resp.json()["result"]["tools"]}
        assert len(tools) == 22
        for tool in tools.values():
            assert tool["annotations"]["readOnlyHint"] is not None
            assert tool["outputSchema"] is not None
        assert tools["list_supported_images"]["annotations"]["readOnlyHint"] is True
        assert tools["delete_jupyter_server"]["annotations"]["destructiveHint"] is True

    def test_tools_call_carries_text_and_structured_content(
        self, client: TestClient
    ) -> None:
        headers = self._session_headers(client)
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_supported_images", "arguments": {}},
            },
            headers=headers,
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        assert "CPU images" in result["content"][0]["text"]
        assert result["structuredContent"] == {"cpu_images": [], "gpu_images": []}
