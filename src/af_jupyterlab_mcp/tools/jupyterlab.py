"""The six phase-1 tools: create/list/get/delete notebooks, GPU + image info.

Owner scoping is strict: the owner of every server is always
``claims.unixname`` from the verified broker JWT (``get_broker_claims``) --
no tool here takes an owner/username argument. All ``kubernetes`` client
calls are blocking, so every k8s-layer call is offloaded via
``asyncio.to_thread`` to keep the MCP event loop responsive (mirrors
ami-mcp's ``run_ami_sync``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any

from mcp.server.mcpserver import Context  # noqa: TC002
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from af_jupyterlab_mcp.auth.broker import get_broker_claims
from af_jupyterlab_mcp.config import DURATION_HOURS_DEFAULT
from af_jupyterlab_mcp.k8s import gpu as gpu_mod
from af_jupyterlab_mcp.k8s import notebooks as notebooks_mod
from af_jupyterlab_mcp.k8s.errors import (
    GuardrailError,
    NameConflictError,
    NotFoundOrNotYoursError,
    QuotaExceededError,
)
from af_jupyterlab_mcp.tools._helpers import (
    append_next_actions,
    format_error,
    format_notebook,
    format_notebook_list,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


class NotebookSummary(BaseModel):
    """The curated notebook fields shown in the Field | Value markdown table.

    Mirrors exactly the field set ``_helpers.format_notebook`` selects via
    ``_SUMMARY_FIELDS`` -- structured content should never promise more than
    the markdown already does. Every field but ``id`` is optional because
    the underlying info dict's shape varies by tool (e.g. ``create_notebook``
    never sets ``pod_status``/``status``/``url``; the raw k8s-internal
    fields it does return, like ``namespace`` or ``duration_hours``, are not
    part of this curated view and are dropped by pydantic's default
    "ignore extra fields" behavior).
    """

    id: str
    name: str | None = None
    owner: str | None = None
    image: str | None = None
    pod_status: str | None = None
    status: str | None = None
    node: str | None = None
    hours_remaining: int | None = None
    cpu_request: int | None = None
    cpu_limit: int | None = None
    memory_request_gb: int | None = None
    memory_limit_gb: int | None = None
    gpu_request: int | None = None
    gpu_product: str | None = None
    url: str | None = None
    log: str | None = None


class CreateJupyterServerResult(BaseModel):
    """Structured result of create_jupyter_server."""

    notebook: NotebookSummary


class GetJupyterServerResult(BaseModel):
    """Structured result of get_jupyter_server."""

    notebook: NotebookSummary


class ListJupyterServersResult(BaseModel):
    """Structured result of list_jupyter_servers."""

    servers: list[NotebookSummary]
    portal_url: str | None = None


class DeleteJupyterServerResult(BaseModel):
    """Structured result of delete_jupyter_server."""

    name: str
    deleted: bool = True


class GpuAvailabilityEntry(BaseModel):
    """One GPU product's cluster-wide availability -- see k8s/gpu.py."""

    product: str
    memory: int
    count: int
    available: int
    total_requests: int


class GetGpuAvailabilityResult(BaseModel):
    """Structured result of get_gpu_availability."""

    entries: list[GpuAvailabilityEntry]


class ListSupportedImagesResult(BaseModel):
    """Structured result of list_supported_images."""

    cpu_images: list[str]
    gpu_images: list[str]


def _lifespan(ctx: Any) -> tuple[Any, Any, Any]:
    """Return (broker_verifier, k8s_clients, settings) from the lifespan context."""
    lc = ctx.request_context.lifespan_context
    return lc["broker_verifier"], lc["k8s_clients"], lc["settings"]


def register(mcp: MCPServer) -> None:
    """Register the six af-jupyterlab-mcp tools on *mcp*."""

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Create JupyterLab server",
            read_only_hint=False,
        )
    )
    async def create_jupyter_server(
        image: str,
        name: str | None = None,
        cpu_cores: int = 2,
        memory_gb: int = 8,
        gpus: int = 0,
        gpu_product: str | None = None,
        duration_hours: int = DURATION_HOURS_DEFAULT,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, CreateJupyterServerResult]:
        """Create a per-user JupyterLab server (pod+service+secret+ingress).

        The server is always owned by the caller (from their verified broker
        identity) -- there is no owner argument. Does not return the
        notebook token or URL; use get_jupyter_server(include_url=True) to
        opt in to that (it is your own credential).
        """
        try:
            verifier, clients, settings = _lifespan(ctx)
            claims = await get_broker_claims(ctx, verifier)
            info = await asyncio.to_thread(
                notebooks_mod.create_notebook,
                clients,
                settings=settings,
                owner=claims.unixname,
                owner_uid=claims.uid,
                name=name,
                image=image,
                cpu_cores=cpu_cores,
                memory_gb=memory_gb,
                gpus=gpus,
                gpu_product=gpu_product,
                duration_hours=duration_hours,
            )
            output = format_notebook(info)
            hints = [
                f"Use `get_jupyter_server(name={info['id']!r})` to check readiness."
            ]
            if settings.portal_url:
                hints.append(
                    f"Visit {settings.portal_url} in your browser to access the notebook."
                )
            text = append_next_actions(output, hints)
        except (GuardrailError, QuotaExceededError, NameConflictError) as exc:
            return format_error(
                exc,
                hints=[
                    "Use `list_supported_images` for the allowed images.",
                    "Use `get_gpu_availability` before requesting GPUs.",
                ],
            )
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        payload = CreateJupyterServerResult(notebook=NotebookSummary(**info))
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload.model_dump(mode="json"),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List your JupyterLab servers",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def list_jupyter_servers(
        *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, ListJupyterServersResult]:
        """List the caller's own JupyterLab servers. Never includes tokenized URLs."""
        try:
            verifier, clients, settings = _lifespan(ctx)
            claims = await get_broker_claims(ctx, verifier)
            infos = await asyncio.to_thread(
                notebooks_mod.list_notebooks,
                clients,
                settings=settings,
                owner=claims.unixname,
            )
            listing = format_notebook_list(infos)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        text = (
            f"Browse your servers: {settings.portal_url}\n\n{listing}"
            if settings.portal_url
            else listing
        )
        payload = ListJupyterServersResult(
            servers=[NotebookSummary(**info) for info in infos],
            portal_url=settings.portal_url,
        )
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload.model_dump(mode="json"),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get JupyterLab server status",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def get_jupyter_server(
        name: str,
        include_log: bool = False,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, GetJupyterServerResult]:
        """Get rich status for one of the caller's own JupyterLab servers.

        Refuses (with a not-found-or-not-yours message) for servers owned by
        another user, without revealing whether the name exists at all. The
        tokenized notebook URL is never returned; use the portal to access the
        notebook in a browser.
        """
        try:
            verifier, clients, settings = _lifespan(ctx)
            claims = await get_broker_claims(ctx, verifier)
            info = await asyncio.to_thread(
                notebooks_mod.get_notebook,
                clients,
                settings=settings,
                name=name,
                owner=claims.unixname,
                include_log=include_log,
                include_url=False,
            )
            # Surface the portal link (no token) if the operator has configured one.
            if settings.portal_url:
                info["url"] = settings.portal_url
            text = format_notebook(info)
        except NotFoundOrNotYoursError as exc:
            return format_error(
                exc, hints=["Use `list_jupyter_servers` to see your own servers."]
            )
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        payload = GetJupyterServerResult(notebook=NotebookSummary(**info))
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload.model_dump(mode="json"),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Delete JupyterLab server",
            read_only_hint=False,
            destructive_hint=True,
        )
    )
    async def delete_jupyter_server(
        name: str, *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, DeleteJupyterServerResult]:
        """Delete one of the caller's own JupyterLab servers (all four objects).

        The try/except/else split (rather than a trailing return inside the
        try block) is deliberate -- ruff's TRY300 flags a return-inside-try
        as it can silently mask an exception raised by the return expression
        itself; pylint's no-else-return disagrees, so it is disabled here.
        """
        try:  # pylint: disable=no-else-return
            verifier, clients, settings = _lifespan(ctx)
            claims = await get_broker_claims(ctx, verifier)
            await asyncio.to_thread(
                notebooks_mod.delete_notebook,
                clients,
                settings=settings,
                name=name,
                owner=claims.unixname,
            )
        except NotFoundOrNotYoursError as exc:
            return format_error(
                exc, hints=["Use `list_jupyter_servers` to see your own servers."]
            )
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        else:
            payload = DeleteJupyterServerResult(name=name)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", text=f"Deleted JupyterLab server {name!r}."
                    )
                ],
                structured_content=payload.model_dump(mode="json"),
            )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get GPU availability",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def get_gpu_availability(
        gpu_product: str | None = None, *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, GetGpuAvailabilityResult]:
        """Get cluster-wide GPU availability, optionally filtered by product."""
        try:
            _verifier, clients, _settings = _lifespan(ctx)
            results = await asyncio.to_thread(
                gpu_mod.get_gpu_availability, clients, gpu_product
            )
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        if not results:
            text = "No GPU nodes found."
        else:
            keys = ["product", "memory", "count", "available", "total_requests"]
            header = "| " + " | ".join(keys) + " |"
            separator = "| " + " | ".join("---" for _ in keys) + " |"
            lines = [header, separator]
            lines.extend(
                "| " + " | ".join(str(r.get(k, "")) for k in keys) + " |"
                for r in results
            )
            text = "\n".join(lines)
        payload = GetGpuAvailabilityResult(
            entries=[
                GpuAvailabilityEntry(
                    product=r["product"],
                    memory=r["memory"],
                    count=r["count"],
                    available=r["available"],
                    total_requests=r["total_requests"],
                )
                for r in results
            ]
        )
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload.model_dump(mode="json"),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List supported images",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def list_supported_images(
        *, ctx: Context[Any, Any]
    ) -> Annotated[CallToolResult, ListSupportedImagesResult]:
        """List the CPU and GPU images allowed by create_jupyter_server, from chart values."""
        try:
            _verifier, _clients, settings = _lifespan(ctx)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
        lines = ["**CPU images:**"]
        lines.extend(f"- {i}" for i in settings.cpu_images)
        lines.append("\n**GPU images:**")
        lines.extend(f"- {i}" for i in settings.gpu_images)
        text = "\n".join(lines)
        payload = ListSupportedImagesResult(
            cpu_images=list(settings.cpu_images),
            gpu_images=list(settings.gpu_images),
        )
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload.model_dump(mode="json"),
        )
