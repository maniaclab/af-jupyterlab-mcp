"""Async MCP proxy client for calling tools on a notebook's jupyter-mcp-server.

Each notebook pod ships `jupyter-mcp-server` at `https://<notebook_id>.<domain>/mcp`,
authenticated by the JUPYTER_TOKEN stored in the pod's env var. This module
provides `call_notebook_tool`, which constructs the endpoint URL, injects the
token server-side via the MCP client transport, calls the upstream tool, and
returns the formatted result.

The token is injected as a query parameter (`?token=<JUPYTER_TOKEN>`) in the
MCP endpoint URL, which is how JupyterLab's IdentityProvider authenticates
requests. The MCP SDK's `streamable_http_client` (which uses `httpx2`
internally) handles the protocol handshake (initialize + tools/call).

TODO(#4): confirm whether jupyter-mcp-server authenticates via `?token=`
query param or via `Authorization: Bearer <token>` header. The current
implementation uses the query param form, which matches how the JupyterLab
portal constructs notebook URLs. If bearer is needed instead, replace
`?token=` with an `httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})`
injected into `streamable_http_client`.

TODO(#4): jupyter-mcp-server uses `stateless_http=True` per the subagent
research. Each request is independent; no session persistence is needed.
"""

from __future__ import annotations

import urllib.parse

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from af_jupyterlab_mcp.tools._helpers import format_error


async def call_notebook_tool(
    *,
    notebook_url: str,
    token: str,
    tool_name: str,
    tool_args: dict,
) -> str:
    """Call a tool on the notebook's jupyter-mcp-server and return formatted output.

    The token is injected into the upstream MCP URL server-side and is never
    returned to the caller. `tool_name` is the upstream tool name (e.g.
    `"execute_code"`, `"notebook_get-selected-cell"` with hyphens preserved).

    Returns a plain string: the text content of the first tool result on
    success, or a formatted ``**Error**: ...`` string on failure (transport
    error, tool not found, upstream error).
    """
    mcp_url = f"{notebook_url}/mcp?{urllib.parse.urlencode({'token': token})}"

    try:
        async with streamable_http_client(mcp_url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, tool_args)
    except Exception as exc:  # noqa: BLE001
        return format_error(
            exc,
            hints=[
                "Check that the notebook is Ready with `get_jupyter_server`.",
                "Verify the notebook pod is reachable from the MCP server.",
            ],
        )

    # Extract text from the result content blocks.
    texts = [c.text for c in result.content if hasattr(c, "text")]
    output = "\n".join(texts) if texts else "(no output)"

    if result.isError:
        return format_error(Exception(output))

    return output
