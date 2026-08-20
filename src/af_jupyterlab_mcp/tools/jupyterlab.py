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
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context  # noqa: TC002

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


def _lifespan(ctx: Any) -> tuple[Any, Any, Any]:
    """Return (broker_verifier, k8s_clients, settings) from the lifespan context."""
    lc = ctx.request_context.lifespan_context
    return lc["broker_verifier"], lc["k8s_clients"], lc["settings"]


def register(mcp: MCPServer) -> None:
    """Register the six af-jupyterlab-mcp tools on *mcp*."""

    @mcp.tool()
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
    ) -> str:
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
            return append_next_actions(
                output,
                [
                    f"Use `get_jupyter_server(name={info['id']!r})` to check readiness.",
                    (
                        f"Use `get_jupyter_server(name={info['id']!r}, include_url=True)` "
                        "to get the tokenized URL once it's ready."
                    ),
                ],
            )
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

    @mcp.tool()
    async def list_jupyter_servers(*, ctx: Context[Any, Any]) -> str:
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
            return format_notebook_list(infos)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)

    @mcp.tool()
    async def get_jupyter_server(
        name: str,
        include_url: bool = False,
        include_log: bool = False,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Get rich status for one of the caller's own JupyterLab servers.

        Refuses (with a not-found-or-not-yours message) for servers owned by
        another user, without revealing whether the name exists at all.
        include_url=True opts in to returning the tokenized URL (your own
        credential; kept out of the default response since it transits LLM
        context and client logs).
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
                include_url=include_url,
            )
            return format_notebook(info)
        except NotFoundOrNotYoursError as exc:
            return format_error(
                exc, hints=["Use `list_jupyter_servers` to see your own servers."]
            )
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)

    @mcp.tool()
    async def delete_jupyter_server(name: str, *, ctx: Context[Any, Any]) -> str:
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
            return f"Deleted JupyterLab server {name!r}."

    @mcp.tool()
    async def get_gpu_availability(
        gpu_product: str | None = None, *, ctx: Context[Any, Any]
    ) -> str:
        """Get cluster-wide GPU availability, optionally filtered by product."""
        try:
            _verifier, clients, _settings = _lifespan(ctx)
            results = await asyncio.to_thread(
                gpu_mod.get_gpu_availability, clients, gpu_product
            )
            if not results:
                return "No GPU nodes found."
            keys = ["product", "memory", "count", "available", "total_requests"]
            header = "| " + " | ".join(keys) + " |"
            separator = "| " + " | ".join("---" for _ in keys) + " |"
            lines = [header, separator]
            lines.extend(
                "| " + " | ".join(str(r.get(k, "")) for k in keys) + " |"
                for r in results
            )
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)

    @mcp.tool()
    async def list_supported_images(*, ctx: Context[Any, Any]) -> str:
        """List the CPU and GPU images allowed by create_jupyter_server, from chart values."""
        try:
            _verifier, _clients, settings = _lifespan(ctx)
            lines = ["**CPU images:**"]
            lines.extend(f"- {i}" for i in settings.cpu_images)
            lines.append("\n**GPU images:**")
            lines.extend(f"- {i}" for i in settings.gpu_images)
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc)
