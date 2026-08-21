"""Proxy tools that forward jupyter-mcp-server tool calls through jupyterlab-mcp.

Each tool here mirrors one tool from jupyter-mcp-server
(https://github.com/datalayer/jupyter-mcp-server). The tool:

1. Takes ``notebook_server_id: str`` as its first required argument.
2. Verifies ownership: the caller's broker identity must own the notebook pod.
3. Checks pod readiness (Ready=True); returns an error with a hint if not.
4. Reads JUPYTER_TOKEN from the pod env — never returned to the caller.
5. Calls the upstream jupyter-mcp-server tool via ``call_notebook_tool``,
   which injects the token server-side.

Hyphens in upstream tool names (e.g. ``notebook_get-selected-cell``) are
normalised to underscores in the Python/MCP tool name (``nb_get_selected_cell``)
but the *original* hyphenated name is passed to ``call_notebook_tool`` so the
upstream server receives the name it expects.

Tool signatures are copied from jupyter-mcp-server v1.x source; see
https://github.com/datalayer/jupyter-mcp-server/tree/main/jupyter_mcp_server/tools

TODO(#4): ``notebook_get-selected-cell`` and ``notebook_run-all-cells`` are
served by the ``jupyter-mcp-tools`` JupyterLab extension (not the core
jupyter-mcp-server package). Their parameter schemas are only known at runtime
from the running JupyterLab instance. The proxy tools below stub them with no
extra parameters and forward whatever args are supplied.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002

from af_jupyterlab_mcp.auth.broker import get_broker_claims
from af_jupyterlab_mcp.k8s.errors import NotFoundOrNotYoursError
from af_jupyterlab_mcp.k8s.notebooks import (
    K8sClients,
    _read_pod_or_none,
    get_notebook_token,
)
from af_jupyterlab_mcp.k8s.proxy import call_notebook_tool
from af_jupyterlab_mcp.tools._helpers import format_error

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from af_jupyterlab_mcp.config import Settings


def _lifespan(ctx: Any) -> tuple[Any, K8sClients, Settings]:
    lc = ctx.request_context.lifespan_context
    return lc["broker_verifier"], lc["k8s_clients"], lc["settings"]


async def _get_ready_pod_and_token(
    ctx: Any,
    notebook_server_id: str,
) -> tuple[Any, str] | str:
    """Resolve the pod for *notebook_server_id*, check ownership and readiness.

    Returns ``(pod, token)`` on success, or a formatted ``**Error**: ...``
    string on any failure (not found, not yours, not ready, no token).
    """
    verifier, clients, settings = _lifespan(ctx)
    claims = await get_broker_claims(ctx, verifier)
    unixname = claims.unixname

    pod = await asyncio.to_thread(
        _read_pod_or_none,
        clients.core_v1,
        settings.notebook_namespace,
        notebook_server_id.lower(),
    )
    if pod is None or pod.metadata.labels.get("owner") != unixname:
        return format_error(
            NotFoundOrNotYoursError(
                f"no notebook named {notebook_server_id!r} (or it is not yours)"
            ),
            hints=["Use `list_jupyter_servers` to see your own servers."],
        )

    ready = any(
        c.type == "Ready" and c.status == "True" for c in pod.status.conditions
    )
    if not ready:
        return format_error(
            Exception(f"Notebook {notebook_server_id!r} is not yet Ready"),
            hints=[
                f"Use `get_jupyter_server(name={notebook_server_id!r})` to check readiness.",
                "Wait for the pod to become Ready before calling notebook tools.",
            ],
        )

    try:
        token = get_notebook_token(pod)
    except ValueError as exc:
        return format_error(exc)

    return pod, token


def register(mcp: MCPServer) -> None:
    """Register the 18 nb_* proxy tools on *mcp*."""

    # ------------------------------------------------------------------
    # Filesystem
    # ------------------------------------------------------------------

    @mcp.tool()
    async def nb_list_files(
        notebook_server_id: str,
        path: str = "",
        max_depth: int = 1,
        start_index: int = 0,
        limit: int = 25,
        pattern: str = "",
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """List files on the notebook server's filesystem."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="list_files",
            tool_args={
                "path": path,
                "max_depth": max_depth,
                "start_index": start_index,
                "limit": limit,
                "pattern": pattern,
            },
        )

    # ------------------------------------------------------------------
    # Kernel management
    # ------------------------------------------------------------------

    @mcp.tool()
    async def nb_list_kernels(
        notebook_server_id: str,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """List all running kernels on the notebook server."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="list_kernels",
            tool_args={},
        )

    # ------------------------------------------------------------------
    # Notebook management
    # ------------------------------------------------------------------

    @mcp.tool()
    async def nb_list_notebooks(
        notebook_server_id: str,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """List notebooks open on the notebook server."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="list_notebooks",
            tool_args={},
        )

    @mcp.tool()
    async def nb_use_notebook(
        notebook_server_id: str,
        notebook_name: str,
        notebook_path: str,
        mode: Literal["connect", "create"] = "connect",
        kernel_id: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Connect to or create a notebook on the notebook server."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        args: dict[str, Any] = {
            "notebook_name": notebook_name,
            "notebook_path": notebook_path,
            "mode": mode,
        }
        if kernel_id is not None:
            args["kernel_id"] = kernel_id
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="use_notebook",
            tool_args=args,
        )

    @mcp.tool()
    async def nb_unuse_notebook(
        notebook_server_id: str,
        notebook_name: str,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Disconnect from a notebook on the notebook server."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="unuse_notebook",
            tool_args={"notebook_name": notebook_name},
        )

    @mcp.tool()
    async def nb_restart_notebook(
        notebook_server_id: str,
        notebook_name: str,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Restart a notebook's kernel on the notebook server."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="restart_notebook",
            tool_args={"notebook_name": notebook_name},
        )

    @mcp.tool()
    async def nb_read_notebook(
        notebook_server_id: str,
        notebook_name: str,
        response_format: Literal["brief", "detailed"] = "brief",
        start_index: int = 0,
        limit: int = 20,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Read a notebook's cells from the notebook server."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="read_notebook",
            tool_args={
                "notebook_name": notebook_name,
                "response_format": response_format,
                "start_index": start_index,
                "limit": limit,
            },
        )

    # ------------------------------------------------------------------
    # Cell reading
    # ------------------------------------------------------------------

    @mcp.tool()
    async def nb_read_cell(
        notebook_server_id: str,
        cell_index: int,
        include_outputs: bool = True,
        notebook_name: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Read a cell from the active notebook on the notebook server."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        args: dict[str, Any] = {
            "cell_index": cell_index,
            "include_outputs": include_outputs,
        }
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="read_cell",
            tool_args=args,
        )

    # ------------------------------------------------------------------
    # Cell writing
    # ------------------------------------------------------------------

    @mcp.tool()
    async def nb_insert_cell(
        notebook_server_id: str,
        cell_index: int,
        cell_type: Literal["code", "markdown"],
        cell_source: str,
        notebook_name: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Insert a cell at a given index in the active notebook."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        args: dict[str, Any] = {
            "cell_index": cell_index,
            "cell_type": cell_type,
            "cell_source": cell_source,
        }
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="insert_cell",
            tool_args=args,
        )

    @mcp.tool()
    async def nb_overwrite_cell_source(
        notebook_server_id: str,
        cell_index: int,
        cell_source: str,
        notebook_name: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Overwrite the source of a cell in the active notebook."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        args: dict[str, Any] = {
            "cell_index": cell_index,
            "cell_source": cell_source,
        }
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="overwrite_cell_source",
            tool_args=args,
        )

    @mcp.tool()
    async def nb_edit_cell_source(
        notebook_server_id: str,
        cell_index: int,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        notebook_name: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Edit part of a cell's source in the active notebook."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        args: dict[str, Any] = {
            "cell_index": cell_index,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        }
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="edit_cell_source",
            tool_args=args,
        )

    @mcp.tool()
    async def nb_delete_cell(
        notebook_server_id: str,
        cell_indices: list[int],
        include_source: bool = True,
        notebook_name: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Delete one or more cells from the active notebook."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        args: dict[str, Any] = {
            "cell_indices": cell_indices,
            "include_source": include_source,
        }
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="delete_cell",
            tool_args=args,
        )

    @mcp.tool()
    async def nb_move_cell(
        notebook_server_id: str,
        source_index: int,
        target_index: int,
        notebook_name: str | None = None,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Move a cell to a different index in the active notebook."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        args: dict[str, Any] = {
            "source_index": source_index,
            "target_index": target_index,
        }
        if notebook_name is not None:
            args["notebook_name"] = notebook_name
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="move_cell",
            tool_args=args,
        )

    # ------------------------------------------------------------------
    # Cell execution
    # ------------------------------------------------------------------

    @mcp.tool()
    async def nb_execute_cell(
        notebook_server_id: str,
        cell_index: int,
        timeout: int = 0,
        stream: bool = True,
        progress_interval: int = 5,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Execute a specific cell in the active notebook."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="execute_cell",
            tool_args={
                "cell_index": cell_index,
                "timeout": timeout,
                "stream": stream,
                "progress_interval": progress_interval,
            },
        )

    @mcp.tool()
    async def nb_insert_execute_code_cell(
        notebook_server_id: str,
        cell_index: int,
        cell_source: str,
        timeout: int = 0,
        stream: bool = True,
        progress_interval: int = 5,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Insert a code cell and immediately execute it in the active notebook."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="insert_execute_code_cell",
            tool_args={
                "cell_index": cell_index,
                "cell_source": cell_source,
                "timeout": timeout,
                "stream": stream,
                "progress_interval": progress_interval,
            },
        )

    # ------------------------------------------------------------------
    # Other tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def nb_execute_code(
        notebook_server_id: str,
        code: str,
        timeout: int = 30,
        kernel_id: str | None = None,
        progress_interval: int = 5,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Execute arbitrary code in the notebook server's kernel."""
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        args: dict[str, Any] = {
            "code": code,
            "timeout": timeout,
            "progress_interval": progress_interval,
        }
        if kernel_id is not None:
            args["kernel_id"] = kernel_id
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="execute_code",
            tool_args=args,
        )

    # ------------------------------------------------------------------
    # JupyterLab extension tools (jupyter-mcp-tools)
    # Parameters below are stubbed; exact schemas are only known at runtime.
    # TODO(#4): fetch live tool schemas from the running JupyterLab instance
    # once jupyter-mcp-tools publishes a stable schema.
    # ------------------------------------------------------------------

    @mcp.tool()
    async def nb_get_selected_cell(
        notebook_server_id: str,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Get the currently selected cell in the active JupyterLab notebook.

        Served by the jupyter-mcp-tools JupyterLab extension. Parameters are
        resolved at runtime from the live instance.
        """
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="notebook_get-selected-cell",
            tool_args={},
        )

    @mcp.tool()
    async def nb_run_all_cells(
        notebook_server_id: str,
        *,
        ctx: Context[Any, Any],
    ) -> str:
        """Run all cells in the active JupyterLab notebook.

        Served by the jupyter-mcp-tools JupyterLab extension. Parameters are
        resolved at runtime from the live instance.
        """
        result = await _get_ready_pod_and_token(ctx, notebook_server_id)
        if isinstance(result, str):
            return result
        _pod, token = result
        _, _, settings = _lifespan(ctx)
        return await call_notebook_tool(
            notebook_url=f"https://{notebook_server_id}.{settings.domain}",
            token=token,
            tool_name="notebook_run-all-cells",
            tool_args={},
        )
