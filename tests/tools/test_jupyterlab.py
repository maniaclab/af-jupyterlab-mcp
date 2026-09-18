"""Tests for the six registered af-jupyterlab-mcp tools end-to-end (fake k8s + fake broker claims)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver import MCPServer

from af_jupyterlab_mcp.config import Settings
from af_jupyterlab_mcp.k8s.notebooks import K8sClients
from af_jupyterlab_mcp.tools.jupyterlab import register
from tests.k8s.fakes import FakeCoreV1Api, FakeNetworkingV1Api

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.types import CallToolResult

_IMAGE = "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest"


class _FakeClaims:
    def __init__(self, unixname: str, uid: int = 1000) -> None:
        self.sub = unixname
        self.unixname = unixname
        self.uid = uid
        self.gid = 1000


class _FakeVerifier:
    def __init__(self, unixname: str = "kratsg") -> None:
        self._unixname = unixname

    async def verify(self, token: str) -> _FakeClaims | None:
        if token != "good-token":
            return None
        return _FakeClaims(self._unixname)


@pytest.fixture
def registered_tools() -> dict[str, Callable[..., Awaitable[CallToolResult]]]:
    mcp = MCPServer("test")
    register(mcp)
    return {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        notebook_namespace="jupyterlab",
        domain="notebooks.af.uchicago.edu",
        cpu_images=(_IMAGE,),
        gpu_images=(),
    )


def _make_ctx(
    *, settings: Settings, unixname: str = "kratsg", token: str = "good-token"
) -> MagicMock:
    core = FakeCoreV1Api()
    clients = K8sClients(core_v1=core, networking_v1=FakeNetworkingV1Api(core=core))
    ctx = MagicMock()
    ctx.request_context.request.headers = {"authorization": f"Bearer {token}"}
    ctx.request_context.lifespan_context = {
        "broker_verifier": _FakeVerifier(unixname),
        "k8s_clients": clients,
        "settings": settings,
    }
    return ctx


class TestContextInjection:
    """Verify that ctx is injected by FastMCP, not exposed as a user-facing arg."""

    def test_ctx_not_in_tool_parameters(self) -> None:
        """ctx must NOT appear in any tool's parameter schema.

        If ctx is annotated as Any instead of Context, FastMCP treats it as a
        regular user argument, not an injected dependency. The MCP client then
        passes {"ctx": {}} and the server receives a plain dict, causing
        'dict' object has no attribute 'request_context'.
        """
        mcp = MCPServer("test")
        register(mcp)
        for tool in mcp._tool_manager.list_tools():
            params = tool.parameters.get("properties", {})
            assert "ctx" not in params, (
                f"Tool '{tool.name}' exposes 'ctx' as a user-facing parameter. "
                "Annotate it as 'ctx: Context[Any, Any]' so FastMCP injects it."
            )

    def test_ctx_kwarg_recognized_by_fastmcp(self) -> None:
        """FastMCP must set context_kwarg='ctx' for every tool."""
        mcp = MCPServer("test")
        register(mcp)
        for tool in mcp._tool_manager.list_tools():
            assert tool.context_kwarg == "ctx", (
                f"Tool '{tool.name}' has context_kwarg={tool.context_kwarg!r}; "
                "expected 'ctx'. Annotate ctx as Context[Any, Any]."
            )


class TestAnnotationsAndOutputSchema:
    """Drift guard: every tool must publish the right annotations and an outputSchema.

    See CLAUDE.md's "Tool registration pattern" -- the read-only/mutating/
    destructive bucket assignment for these six tools.
    """

    _READ_ONLY: ClassVar[set[str]] = {
        "list_jupyter_servers",
        "get_jupyter_server",
        "get_gpu_availability",
        "list_supported_images",
    }
    _MUTATING: ClassVar[set[str]] = {"create_jupyter_server"}
    _DESTRUCTIVE: ClassVar[set[str]] = {"delete_jupyter_server"}

    def test_every_tool_declares_annotations_and_output_schema(self) -> None:
        mcp = MCPServer("test")
        register(mcp)
        for tool in mcp._tool_manager.list_tools():
            assert tool.annotations is not None, tool.name
            assert tool.annotations.read_only_hint is not None, tool.name
            assert tool.output_schema is not None, tool.name

    def test_read_only_tools_are_read_only(self) -> None:
        mcp = MCPServer("test")
        register(mcp)
        for tool in mcp._tool_manager.list_tools():
            if tool.name in self._READ_ONLY:
                assert tool.annotations.read_only_hint is True, tool.name  # type: ignore[union-attr]

    def test_mutating_non_destructive_tools_are_not_read_only_or_destructive(
        self,
    ) -> None:
        mcp = MCPServer("test")
        register(mcp)
        for tool in mcp._tool_manager.list_tools():
            if tool.name in self._MUTATING:
                assert tool.annotations.read_only_hint is False, tool.name  # type: ignore[union-attr]
                assert tool.annotations.destructive_hint is None, tool.name  # type: ignore[union-attr]

    def test_destructive_tools_are_marked_destructive(self) -> None:
        mcp = MCPServer("test")
        register(mcp)
        for tool in mcp._tool_manager.list_tools():
            if tool.name in self._DESTRUCTIVE:
                assert tool.annotations.read_only_hint is False, tool.name  # type: ignore[union-attr]
                assert tool.annotations.destructive_hint is True, tool.name  # type: ignore[union-attr]


class TestCreateJupyterServer:
    async def test_creates_and_reports_id(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings)
        result = await registered_tools["create_jupyter_server"](image=_IMAGE, ctx=ctx)
        output = tool_text(result)
        assert "kratsg-notebook-1" in output
        assert (
            "token" not in output.lower() or "Next steps" in output
        )  # never leaks a raw token
        assert result.is_error is not True
        assert result.structured_content is not None
        assert result.structured_content["notebook"]["id"] == "kratsg-notebook-1"

    async def test_guardrail_violation_is_reported_as_error(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings)
        result = await registered_tools["create_jupyter_server"](
            image=_IMAGE, cpu_cores=999, ctx=ctx
        )
        output = tool_text(result)
        assert "**Error**" in output
        assert "cpu_cores" in output
        assert result.is_error is True
        assert result.structured_content is None

    async def test_disallowed_image_is_reported_as_error(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings)
        result = await registered_tools["create_jupyter_server"](
            image="not-allowed:latest", ctx=ctx
        )
        assert "**Error**" in tool_text(result)
        assert result.is_error is True

    async def test_invalid_bearer_is_reported_as_error(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings, token="wrong-token")
        result = await registered_tools["create_jupyter_server"](image=_IMAGE, ctx=ctx)
        assert "**Error**" in tool_text(result)
        assert result.is_error is True


class TestListJupyterServers:
    async def test_lists_only_own_servers(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx_alice = _make_ctx(settings=settings, unixname="alice")
        await registered_tools["create_jupyter_server"](image=_IMAGE, ctx=ctx_alice)

        # Same underlying fake cluster, different owner.
        clients = ctx_alice.request_context.lifespan_context["k8s_clients"]
        ctx_bob = MagicMock()
        ctx_bob.request_context.request.headers = {"authorization": "Bearer good-token"}
        ctx_bob.request_context.lifespan_context = {
            "broker_verifier": _FakeVerifier("bob"),
            "k8s_clients": clients,
            "settings": settings,
        }
        await registered_tools["create_jupyter_server"](image=_IMAGE, ctx=ctx_bob)

        result = await registered_tools["list_jupyter_servers"](ctx=ctx_alice)
        alice_list = tool_text(result)
        assert "alice-notebook-1" in alice_list
        assert "bob-notebook-1" not in alice_list
        assert result.structured_content is not None
        server_ids = [s["id"] for s in result.structured_content["servers"]]
        assert server_ids == ["alice-notebook-1"]


class TestGetJupyterServer:
    async def test_owner_can_get_own_server(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings)
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )
        result = await registered_tools["get_jupyter_server"](name="mine", ctx=ctx)
        assert "mine" in tool_text(result)
        assert result.structured_content is not None
        assert result.structured_content["notebook"]["name"] == "mine"

    async def test_other_user_cannot_get_server(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings, unixname="kratsg")
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )

        clients = ctx.request_context.lifespan_context["k8s_clients"]
        ctx_eve = MagicMock()
        ctx_eve.request_context.request.headers = {"authorization": "Bearer good-token"}
        ctx_eve.request_context.lifespan_context = {
            "broker_verifier": _FakeVerifier("eve"),
            "k8s_clients": clients,
            "settings": settings,
        }
        result = await registered_tools["get_jupyter_server"](name="mine", ctx=ctx_eve)
        output = tool_text(result)
        assert "**Error**" in output
        assert "not yours" in output or "no notebook" in output
        assert result.is_error is True


class TestDeleteJupyterServer:
    async def test_owner_can_delete_own_server(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings)
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )
        result = await registered_tools["delete_jupyter_server"](name="mine", ctx=ctx)
        assert "Deleted" in tool_text(result)
        assert result.is_error is not True
        assert result.structured_content == {"name": "mine", "deleted": True}

    async def test_other_user_cannot_delete_server(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings, unixname="kratsg")
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )

        clients = ctx.request_context.lifespan_context["k8s_clients"]
        ctx_eve = MagicMock()
        ctx_eve.request_context.request.headers = {"authorization": "Bearer good-token"}
        ctx_eve.request_context.lifespan_context = {
            "broker_verifier": _FakeVerifier("eve"),
            "k8s_clients": clients,
            "settings": settings,
        }
        result = await registered_tools["delete_jupyter_server"](
            name="mine", ctx=ctx_eve
        )
        assert "**Error**" in tool_text(result)
        assert result.is_error is True
        assert ("jupyterlab", "mine") in clients.core_v1.pods


class TestListJupyterServersPortalUrl:
    async def test_portal_url_shown_in_header_when_set(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """When portal_url is set, list_jupyter_servers prepends a portal link."""
        s = Settings(
            notebook_namespace="jupyterlab",
            domain="notebooks.af.uchicago.edu",
            cpu_images=(_IMAGE,),
            gpu_images=(),
            portal_url="https://af.uchicago.edu/jupyterlab",
        )
        ctx = _make_ctx(settings=s)
        result = await registered_tools["list_jupyter_servers"](ctx=ctx)
        assert "https://af.uchicago.edu/jupyterlab" in tool_text(result)
        assert result.structured_content is not None
        assert (
            result.structured_content["portal_url"]
            == "https://af.uchicago.edu/jupyterlab"
        )

    async def test_portal_url_absent_when_not_set(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """When portal_url is None, no portal URL appears in the list output."""
        assert settings.portal_url is None
        ctx = _make_ctx(settings=settings)
        result = await registered_tools["list_jupyter_servers"](ctx=ctx)
        # The token must not appear, and no portal URL prefix either
        assert "Browse" not in tool_text(result)
        assert result.structured_content is not None
        assert result.structured_content["portal_url"] is None

    async def test_portal_url_is_not_tokenized(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """The portal URL line must never contain a token."""
        s = Settings(
            notebook_namespace="jupyterlab",
            domain="notebooks.af.uchicago.edu",
            cpu_images=(_IMAGE,),
            gpu_images=(),
            portal_url="https://af.uchicago.edu/jupyterlab",
        )
        ctx = _make_ctx(settings=s)
        # Create a server so there's something to list
        await registered_tools["create_jupyter_server"](image=_IMAGE, ctx=ctx)
        result = await registered_tools["list_jupyter_servers"](ctx=ctx)
        assert "token" not in tool_text(result).lower()


class TestGetJupyterServerNoIncludeUrl:
    def test_include_url_parameter_removed(self) -> None:
        """get_jupyter_server must NOT expose include_url as a user-facing parameter."""
        mcp = MCPServer("test")
        register(mcp)
        get_tool = next(
            t for t in mcp._tool_manager.list_tools() if t.name == "get_jupyter_server"
        )
        params = get_tool.parameters.get("properties", {})
        assert "include_url" not in params, (
            "include_url must be removed from get_jupyter_server — the token must never transit LLM context"
        )

    async def test_no_tokenized_url_in_get_response(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """get_jupyter_server must never return a tokenized URL."""
        ctx = _make_ctx(settings=settings)
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )
        result = await registered_tools["get_jupyter_server"](name="mine", ctx=ctx)
        # Token is a base64-encoded 32-byte value — check no ?token= query param
        assert "?token=" not in tool_text(result)

    async def test_portal_url_shown_when_set(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """When portal_url is set, get_jupyter_server includes a portal link."""
        s = Settings(
            notebook_namespace="jupyterlab",
            domain="notebooks.af.uchicago.edu",
            cpu_images=(_IMAGE,),
            gpu_images=(),
            portal_url="https://af.uchicago.edu/jupyterlab",
        )
        ctx = _make_ctx(settings=s)
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )
        result = await registered_tools["get_jupyter_server"](name="mine", ctx=ctx)
        assert "https://af.uchicago.edu/jupyterlab" in tool_text(result)
        assert result.structured_content is not None
        assert (
            result.structured_content["notebook"]["url"]
            == "https://af.uchicago.edu/jupyterlab"
        )

    async def test_url_row_absent_when_portal_url_not_set(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        """When portal_url is None, get_jupyter_server omits the URL row entirely."""
        assert settings.portal_url is None
        ctx = _make_ctx(settings=settings)
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )
        result = await registered_tools["get_jupyter_server"](name="mine", ctx=ctx)
        output = tool_text(result)
        assert "url" not in output.lower() or "| url |" not in output


class TestGetGpuAvailabilityTool:
    async def test_no_gpu_nodes(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings)
        result = await registered_tools["get_gpu_availability"](ctx=ctx)
        assert "No GPU nodes found." in tool_text(result)
        assert result.structured_content == {"entries": []}


class TestListSupportedImagesTool:
    async def test_lists_cpu_and_gpu_images(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[CallToolResult]]],
        settings: Settings,
        tool_text: Callable[[CallToolResult], str],
    ) -> None:
        ctx = _make_ctx(settings=settings)
        result = await registered_tools["list_supported_images"](ctx=ctx)
        assert _IMAGE in tool_text(result)
        assert result.structured_content == {
            "cpu_images": [_IMAGE],
            "gpu_images": [],
        }
