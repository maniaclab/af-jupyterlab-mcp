"""Tests for the six registered af-jupyterlab-mcp tools end-to-end (fake k8s + fake broker claims)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver import MCPServer

from af_jupyterlab_mcp.config import Settings
from af_jupyterlab_mcp.k8s.notebooks import K8sClients
from af_jupyterlab_mcp.tools.jupyterlab import register
from tests.k8s.fakes import FakeCoreV1Api, FakeNetworkingV1Api

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
def registered_tools() -> dict[str, Callable[..., Awaitable[str]]]:
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


class TestCreateJupyterServer:
    async def test_creates_and_reports_id(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        ctx = _make_ctx(settings=settings)
        output = await registered_tools["create_jupyter_server"](image=_IMAGE, ctx=ctx)
        assert "kratsg-notebook-1" in output
        assert (
            "token" not in output.lower() or "Next steps" in output
        )  # never leaks a raw token

    async def test_guardrail_violation_is_reported_as_error(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        ctx = _make_ctx(settings=settings)
        output = await registered_tools["create_jupyter_server"](
            image=_IMAGE, cpu_cores=999, ctx=ctx
        )
        assert "**Error**" in output
        assert "cpu_cores" in output

    async def test_disallowed_image_is_reported_as_error(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        ctx = _make_ctx(settings=settings)
        output = await registered_tools["create_jupyter_server"](
            image="not-allowed:latest", ctx=ctx
        )
        assert "**Error**" in output

    async def test_invalid_bearer_is_reported_as_error(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        ctx = _make_ctx(settings=settings, token="wrong-token")
        output = await registered_tools["create_jupyter_server"](image=_IMAGE, ctx=ctx)
        assert "**Error**" in output


class TestListJupyterServers:
    async def test_lists_only_own_servers(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
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

        alice_list = await registered_tools["list_jupyter_servers"](ctx=ctx_alice)
        assert "alice-notebook-1" in alice_list
        assert "bob-notebook-1" not in alice_list


class TestGetJupyterServer:
    async def test_owner_can_get_own_server(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        ctx = _make_ctx(settings=settings)
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )
        output = await registered_tools["get_jupyter_server"](name="mine", ctx=ctx)
        assert "mine" in output

    async def test_other_user_cannot_get_server(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
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
        output = await registered_tools["get_jupyter_server"](name="mine", ctx=ctx_eve)
        assert "**Error**" in output
        assert "not yours" in output or "no notebook" in output


class TestDeleteJupyterServer:
    async def test_owner_can_delete_own_server(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        ctx = _make_ctx(settings=settings)
        await registered_tools["create_jupyter_server"](
            image=_IMAGE, name="mine", ctx=ctx
        )
        output = await registered_tools["delete_jupyter_server"](name="mine", ctx=ctx)
        assert "Deleted" in output

    async def test_other_user_cannot_delete_server(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
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
        output = await registered_tools["delete_jupyter_server"](
            name="mine", ctx=ctx_eve
        )
        assert "**Error**" in output
        assert ("jupyterlab", "mine") in clients.core_v1.pods


class TestGetGpuAvailabilityTool:
    async def test_no_gpu_nodes(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        ctx = _make_ctx(settings=settings)
        output = await registered_tools["get_gpu_availability"](ctx=ctx)
        assert "No GPU nodes found." in output


class TestListSupportedImagesTool:
    async def test_lists_cpu_and_gpu_images(
        self,
        registered_tools: dict[str, Callable[..., Awaitable[str]]],
        settings: Settings,
    ) -> None:
        ctx = _make_ctx(settings=settings)
        output = await registered_tools["list_supported_images"](ctx=ctx)
        assert _IMAGE in output
