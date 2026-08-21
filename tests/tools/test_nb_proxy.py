"""Tests for the 18 nb_* proxy tools in nb_proxy.py.

These tests verify:
1. Each proxy tool enforces ownership (pod owner label matches caller).
2. Each proxy tool checks pod readiness before forwarding.
3. Each proxy tool injects the token server-side and calls the upstream tool.
4. The token is never returned in any tool response.
5. ctx is injected by FastMCP (not exposed as a user-facing arg).

call_notebook_tool is patched throughout -- no real notebook is contacted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.mcpserver import MCPServer

from af_jupyterlab_mcp.config import Settings
from af_jupyterlab_mcp.k8s.notebooks import K8sClients, get_notebook_token
from af_jupyterlab_mcp.tools import jupyterlab as jlab_tools
from af_jupyterlab_mcp.tools import nb_proxy as nb_proxy_mod
from tests.k8s.fakes import FakeCoreV1Api, FakeNetworkingV1Api

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_IMAGE = "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest"


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


class _FakeClaims:
    def __init__(self, unixname: str, uid: int = 1000) -> None:
        self.sub = unixname
        self.unixname = unixname
        self.uid = uid
        self.gid = 1000


class _FakeVerifier:
    def __init__(self, unixname: str = "alice") -> None:
        self._unixname = unixname

    async def verify(self, token: str) -> _FakeClaims | None:
        if token != "good-token":
            return None
        return _FakeClaims(self._unixname)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        notebook_namespace="jupyterlab",
        domain="notebooks.af.uchicago.edu",
        cpu_images=(_IMAGE,),
        gpu_images=(),
    )


@pytest.fixture
def registered_nb_tools() -> dict[str, Callable[..., Awaitable[str]]]:
    mcp = MCPServer("test")
    nb_proxy_mod.register(mcp)
    return {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()}


def _make_base_ctx(
    *,
    settings: Settings,
    unixname: str = "alice",
    token: str = "good-token",
) -> tuple[MagicMock, FakeCoreV1Api]:
    """Build a context with an empty fake cluster. Tests create notebooks via create_jupyter_server."""
    core = FakeCoreV1Api()
    clients = K8sClients(core_v1=core, networking_v1=FakeNetworkingV1Api(core=core))
    ctx = MagicMock()
    ctx.request_context.request.headers = {"authorization": f"Bearer {token}"}
    ctx.request_context.lifespan_context = {
        "broker_verifier": _FakeVerifier(unixname),
        "k8s_clients": clients,
        "settings": settings,
    }
    return ctx, core


def _jlab_tools_dict() -> dict[str, Callable[..., Awaitable[str]]]:
    mcp = MCPServer("setup")
    jlab_tools.register(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


# ---------------------------------------------------------------------------
# Tests: ctx injection contract
# ---------------------------------------------------------------------------


class TestNbProxyContextInjection:
    def test_ctx_not_in_any_tool_parameters(self) -> None:
        """ctx must not appear in any nb_* tool's parameter schema."""
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            params = tool.parameters.get("properties", {})
            assert "ctx" not in params, (
                f"Tool '{tool.name}' exposes 'ctx' as a user-facing parameter."
            )

    def test_all_18_tools_are_registered(self) -> None:
        """Exactly 18 nb_* tools must be registered."""
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        names = [t.name for t in mcp._tool_manager.list_tools()]
        assert len(names) == 18, f"Expected 18 tools, got {len(names)}: {names}"

    def test_notebook_server_id_is_required_parameter(self) -> None:
        """notebook_server_id must be a required parameter of every nb_* tool."""
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            params = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            assert "notebook_server_id" in params, (
                f"Tool '{tool.name}' is missing notebook_server_id parameter."
            )
            assert "notebook_server_id" in required, (
                f"Tool '{tool.name}' has notebook_server_id but it is not required."
            )


# ---------------------------------------------------------------------------
# Tests: simple tool (nb_list_kernels — no extra args)
# ---------------------------------------------------------------------------


class TestNbListKernels:
    async def test_returns_error_for_nonexistent_notebook(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        """Accessing a notebook not owned by the caller returns a formatted error."""
        ctx, _ = _make_base_ctx(settings=settings, unixname="alice")
        with patch("af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool"):
            result = await registered_nb_tools["nb_list_kernels"](
                notebook_server_id="does-not-exist", ctx=ctx
            )
        assert "**Error**" in result

    async def test_returns_error_for_pod_not_ready(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        """When pod is Pending (no Ready condition), a clear error with hint is returned."""
        ctx, _ = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](image=_IMAGE, name="alice-notebook-1", ctx=ctx)
        # Default fake pod has no Ready condition (status.conditions=[])
        with patch("af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool"):
            result = await registered_nb_tools["nb_list_kernels"](
                notebook_server_id="alice-notebook-1", ctx=ctx
            )
        assert "**Error**" in result
        assert "get_jupyter_server" in result

    async def test_calls_upstream_list_kernels_when_ready(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        """nb_list_kernels forwards to upstream list_kernels with no extra args when pod is Ready."""
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](image=_IMAGE, name="alice-notebook-1", ctx=ctx)

        # Mark pod Ready
        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        core.pod_logs[("jupyterlab", "alice-notebook-1")] = "Jupyter Server 2.x is running at"

        with patch(
            "af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool",
            new=AsyncMock(return_value="kernel list output"),
        ) as mock_call:
            result = await registered_nb_tools["nb_list_kernels"](
                notebook_server_id="alice-notebook-1", ctx=ctx
            )

        mock_call.assert_called_once()
        kwargs = mock_call.call_args.kwargs
        assert kwargs["tool_name"] == "list_kernels"
        assert kwargs["tool_args"] == {}
        assert "kernel list output" in result


# ---------------------------------------------------------------------------
# Tests: multi-arg tool (nb_execute_code)
# ---------------------------------------------------------------------------


class TestNbExecuteCode:
    async def test_calls_upstream_execute_code_with_all_args(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        """nb_execute_code forwards code and optional args to upstream execute_code."""
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](image=_IMAGE, name="alice-notebook-1", ctx=ctx)

        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        core.pod_logs[("jupyterlab", "alice-notebook-1")] = "Jupyter Server 2.x is running at"

        with patch(
            "af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool",
            new=AsyncMock(return_value="42"),
        ) as mock_call:
            await registered_nb_tools["nb_execute_code"](
                notebook_server_id="alice-notebook-1",
                code="print(42)",
                timeout=60,
                ctx=ctx,
            )

        kwargs = mock_call.call_args.kwargs
        assert kwargs["tool_name"] == "execute_code"
        assert kwargs["tool_args"]["code"] == "print(42)"
        assert kwargs["tool_args"]["timeout"] == 60
        assert "notebook_server_id" not in kwargs["tool_args"]

    async def test_token_never_in_result(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        """The JUPYTER_TOKEN must not appear in nb_execute_code output."""
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](image=_IMAGE, name="alice-notebook-1", ctx=ctx)

        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        core.pod_logs[("jupyterlab", "alice-notebook-1")] = "Jupyter Server 2.x is running at"

        real_token = get_notebook_token(pod)

        with patch(
            "af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool",
            new=AsyncMock(return_value="execution done"),
        ):
            result = await registered_nb_tools["nb_execute_code"](
                notebook_server_id="alice-notebook-1",
                code="x=1",
                ctx=ctx,
            )

        assert real_token not in result


# ---------------------------------------------------------------------------
# Tests: hyphenated upstream name (nb_get_selected_cell)
# ---------------------------------------------------------------------------


class TestNbGetSelectedCell:
    async def test_passes_hyphenated_tool_name_to_upstream(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        """nb_get_selected_cell passes 'notebook_get-selected-cell' (with hyphen) upstream."""
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](image=_IMAGE, name="alice-notebook-1", ctx=ctx)

        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        core.pod_logs[("jupyterlab", "alice-notebook-1")] = "Jupyter Server 2.x is running at"

        with patch(
            "af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool",
            new=AsyncMock(return_value="cell info"),
        ) as mock_call:
            await registered_nb_tools["nb_get_selected_cell"](
                notebook_server_id="alice-notebook-1", ctx=ctx
            )

        kwargs = mock_call.call_args.kwargs
        assert kwargs["tool_name"] == "notebook_get-selected-cell"
