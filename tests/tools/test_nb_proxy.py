"""Tests for the 16 nb_* proxy tools in nb_proxy.py.

These tests verify:
1. Each proxy tool enforces ownership (pod owner label matches caller).
2. Each proxy tool checks pod readiness before forwarding.
3. Each proxy tool injects the token server-side and calls the upstream tool.
4. The token is never returned in any tool response.
5. ctx is injected by FastMCP (not exposed as a user-facing arg).

call_notebook_tool is patched throughout -- no real notebook is contacted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.mcpserver import MCPServer

from af_jupyterlab_mcp.config import Settings
from af_jupyterlab_mcp.k8s.errors import NotebookNotReadyError, NotFoundOrNotYoursError
from af_jupyterlab_mcp.k8s.notebooks import K8sClients, get_notebook_token
from af_jupyterlab_mcp.k8s.proxy import (
    NotebookToolTransportError,
    NotebookToolUpstreamError,
)
from af_jupyterlab_mcp.tools import jupyterlab as jlab_tools
from af_jupyterlab_mcp.tools import nb_proxy as nb_proxy_mod
from tests.k8s.fakes import FakeCoreV1Api, FakeNetworkingV1Api

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.types import CallToolResult

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
def registered_nb_tools() -> dict[str, Callable[..., Awaitable[CallToolResult]]]:
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


def _jlab_tools_dict() -> dict[str, Callable[..., Awaitable[CallToolResult]]]:
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

    def test_all_16_tools_are_registered(self) -> None:
        """Exactly 16 nb_* tools must be registered.

        nb_get_selected_cell and nb_run_all_cells are excluded because they
        require the jupyter-mcp-tools JupyterLab extension which is not
        installed in the current notebook images.
        """
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        names = [t.name for t in mcp._tool_manager.list_tools()]
        assert len(names) == 16, f"Expected 16 tools, got {len(names)}: {names}"
        assert "nb_get_selected_cell" not in names
        assert "nb_run_all_cells" not in names

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
# Tests: every tool declares annotations and an output schema
# ---------------------------------------------------------------------------


class TestNbProxyAnnotationsAndOutputSchema:
    """Drift guard, plus the read-only/mutating/destructive bucket assignment.

    See CLAUDE.md's "Tool registration pattern" for the rationale.
    """

    _READ_ONLY: ClassVar[set[str]] = {
        "nb_list_files",
        "nb_list_kernels",
        "nb_list_notebooks",
        "nb_read_notebook",
        "nb_read_cell",
    }
    _MUTATING: ClassVar[set[str]] = {
        "nb_use_notebook",
        "nb_unuse_notebook",
        "nb_insert_cell",
        "nb_edit_cell_source",
        "nb_move_cell",
    }
    _DESTRUCTIVE: ClassVar[set[str]] = {
        "nb_restart_notebook",
        "nb_overwrite_cell_source",
        "nb_delete_cell",
        "nb_execute_cell",
        "nb_insert_execute_code_cell",
        "nb_execute_code",
    }

    def test_every_tool_declares_annotations_and_output_schema(self) -> None:
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            assert tool.annotations is not None, tool.name
            assert tool.annotations.read_only_hint is not None, tool.name
            assert tool.output_schema is not None, tool.name

    def test_buckets_cover_all_16_tools_with_no_overlap(self) -> None:
        all_buckets = self._READ_ONLY | self._MUTATING | self._DESTRUCTIVE
        assert len(all_buckets) == 16
        assert not (self._READ_ONLY & self._MUTATING)
        assert not (self._READ_ONLY & self._DESTRUCTIVE)
        assert not (self._MUTATING & self._DESTRUCTIVE)

    def test_read_only_tools_are_read_only(self) -> None:
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            if tool.name in self._READ_ONLY:
                assert tool.annotations.read_only_hint is True, tool.name  # type: ignore[union-attr]

    def test_mutating_non_destructive_tools_are_not_read_only_or_destructive(
        self,
    ) -> None:
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            if tool.name in self._MUTATING:
                assert tool.annotations.read_only_hint is False, tool.name  # type: ignore[union-attr]
                assert tool.annotations.destructive_hint is None, tool.name  # type: ignore[union-attr]

    def test_destructive_tools_are_marked_destructive(self) -> None:
        mcp = MCPServer("test")
        nb_proxy_mod.register(mcp)
        for tool in mcp._tool_manager.list_tools():
            if tool.name in self._DESTRUCTIVE:
                assert tool.annotations.read_only_hint is False, tool.name  # type: ignore[union-attr]
                assert tool.annotations.destructive_hint is True, tool.name  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Tests: simple tool (nb_list_kernels — no extra args)
# ---------------------------------------------------------------------------


class TestNbListKernels:
    async def test_returns_error_for_nonexistent_notebook(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """Accessing a notebook not owned by the caller returns a formatted error."""
        ctx, _ = _make_base_ctx(settings=settings, unixname="alice")
        with patch("af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool"):
            result = await registered_nb_tools["nb_list_kernels"](
                notebook_server_id="does-not-exist", ctx=ctx
            )
        assert "**Error**" in tool_text(result)
        assert result.is_error is True

    async def test_returns_error_for_pod_not_ready(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """When pod is Pending (no Ready condition), a clear error with hint is returned."""
        ctx, _ = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](
            image=_IMAGE, name="alice-notebook-1", ctx=ctx
        )
        # Default fake pod has no Ready condition (status.conditions=[])
        with patch("af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool"):
            result = await registered_nb_tools["nb_list_kernels"](
                notebook_server_id="alice-notebook-1", ctx=ctx
            )
        output = tool_text(result)
        assert "**Error**" in output
        assert "get_jupyter_server" in output
        assert result.is_error is True

    async def test_calls_upstream_list_kernels_when_ready(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """nb_list_kernels forwards to upstream list_kernels with no extra args when pod is Ready."""
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](
            image=_IMAGE, name="alice-notebook-1", ctx=ctx
        )

        # Mark pod Ready
        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        core.pod_logs[("jupyterlab", "alice-notebook-1")] = (
            "Jupyter Server 2.x is running at"
        )

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
        assert "kernel list output" in tool_text(result)
        assert result.is_error is not True
        assert result.structured_content == {"result": "kernel list output"}


# ---------------------------------------------------------------------------
# Tests: multi-arg tool (nb_execute_code)
# ---------------------------------------------------------------------------


class TestNbExecuteCode:
    async def test_calls_upstream_execute_code_with_all_args(
        self,
        registered_nb_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
    ) -> None:
        """nb_execute_code forwards code and optional args to upstream execute_code."""
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](
            image=_IMAGE, name="alice-notebook-1", ctx=ctx
        )

        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        core.pod_logs[("jupyterlab", "alice-notebook-1")] = (
            "Jupyter Server 2.x is running at"
        )

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
        registered_nb_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """The JUPYTER_TOKEN must not appear in nb_execute_code output."""
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](
            image=_IMAGE, name="alice-notebook-1", ctx=ctx
        )

        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]
        core.pod_logs[("jupyterlab", "alice-notebook-1")] = (
            "Jupyter Server 2.x is running at"
        )

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

        output = tool_text(result)
        assert real_token not in output
        assert result.structured_content is not None
        assert real_token not in result.structured_content["result"]


# ---------------------------------------------------------------------------
# Tests: _get_ready_pod_and_token raises, never returns a sniffed string
# ---------------------------------------------------------------------------


class TestGetReadyPodAndTokenRaises:
    """`_get_ready_pod_and_token` raises typed exceptions rather than returning
    an error string a caller must ``isinstance``-sniff for -- see the
    "str-means-error" tunnel this replaced (nb_proxy.py's module docstring
    and interop plan A.2).
    """

    async def test_raises_not_found_or_not_yours_for_unknown_notebook(
        self, settings: Settings
    ) -> None:
        ctx, _ = _make_base_ctx(settings=settings, unixname="alice")
        with pytest.raises(NotFoundOrNotYoursError):
            await nb_proxy_mod._get_ready_pod_and_token(ctx, "does-not-exist")

    async def test_raises_not_ready_for_pending_pod(self, settings: Settings) -> None:
        ctx, _ = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](
            image=_IMAGE, name="alice-notebook-1", ctx=ctx
        )
        # Default fake pod has no Ready condition (status.conditions=[])
        with pytest.raises(NotebookNotReadyError):
            await nb_proxy_mod._get_ready_pod_and_token(ctx, "alice-notebook-1")

    async def test_returns_pod_and_token_when_ready(self, settings: Settings) -> None:
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](
            image=_IMAGE, name="alice-notebook-1", ctx=ctx
        )
        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]

        result_pod, token = await nb_proxy_mod._get_ready_pod_and_token(
            ctx, "alice-notebook-1"
        )
        assert result_pod is pod
        assert token == get_notebook_token(pod)


# ---------------------------------------------------------------------------
# Tests: _call_upstream formats each raised exception with the right hints
# ---------------------------------------------------------------------------


class TestCallUpstreamErrorFormatting:
    async def test_transport_error_gets_readiness_and_reachability_hints(
        self, settings: Settings, tool_text: Callable[[CallToolResult], str]
    ) -> None:
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](
            image=_IMAGE, name="alice-notebook-1", ctx=ctx
        )
        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]

        with patch(
            "af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool",
            new=AsyncMock(side_effect=NotebookToolTransportError("unreachable")),
        ):
            result = await nb_proxy_mod._call_upstream(
                ctx, "alice-notebook-1", "list_kernels", {}
            )

        output = tool_text(result)
        assert "**Error**" in output
        assert "unreachable" in output
        assert "get_jupyter_server" in output
        assert result.is_error is True

    async def test_upstream_error_is_formatted_without_extra_hints(
        self, settings: Settings, tool_text: Callable[[CallToolResult], str]
    ) -> None:
        ctx, core = _make_base_ctx(settings=settings)
        jtools = _jlab_tools_dict()
        await jtools["create_jupyter_server"](
            image=_IMAGE, name="alice-notebook-1", ctx=ctx
        )
        pod = core.pods[("jupyterlab", "alice-notebook-1")]
        pod.status.conditions = [MagicMock(type="Ready", status="True")]

        with patch(
            "af_jupyterlab_mcp.tools.nb_proxy.call_notebook_tool",
            new=AsyncMock(side_effect=NotebookToolUpstreamError("kernel not found")),
        ):
            result = await nb_proxy_mod._call_upstream(
                ctx, "alice-notebook-1", "list_kernels", {}
            )

        output = tool_text(result)
        assert "**Error**" in output
        assert "kernel not found" in output
        assert result.is_error is True
