# af-jupyterlab-mcp — Contributor Guide

MCP server that lets AF users create, inspect, and delete their own per-user
JupyterLab servers on the UChicago ATLAS Analysis Facility Kubernetes cluster —
the same notebooks [af-portal](https://github.com/maniaclab/af-portal) deploys
today — exposed as tools for LLMs, behind the af-mcp-platform credential broker.

## Architecture

```
LLM <--MCP/HTTP--> af-mcp-platform aggregator <--Bearer: broker JWT--> af-jupyterlab-mcp <--k8s API--> notebook namespace
```

**Design philosophy**: unlike ami-mcp (which exposes a query DSL and lets the
LLM be expressive), this backend exposes fixed, typed tools over Kubernetes
Pod/Service/Secret/Ingress objects and the notebook's own jupyter-mcp-server.
There is no raw-k8s-manifest escape hatch: the value here is the AF-specific
policy layered on top (guardrail validation, dual-writer safety, owner-scoping),
not a thin pass-through to the Kubernetes API.

Six CRD-management tools create/inspect/delete the pod+service+secret+ingress
quadruple (`tools/jupyterlab.py`). Sixteen `nb_*` tools (`tools/nb_proxy.py`,
tracked in
[maniaclab/af-mcp-platform#189](https://github.com/maniaclab/af-mcp-platform/issues/189))
proxy to the Datalayer `jupyter-mcp-server` running inside the notebook itself,
so a session can drive code execution inside the user's own notebook without the
notebook token ever entering LLM context.

## Project layout

`src/af_jupyterlab_mcp/` splits into `auth/` (broker JWT verification), `k8s/`
(all Kubernetes-facing logic: notebooks, guardrails, names, gpu, templates, and
the nb_* proxy transport), and `tools/` (the `@mcp.tool()` registrations in
`jupyterlab.py` and `nb_proxy.py`, plus shared `_helpers.py`). `tests/` mirrors
this layout one-to-one (`tests/k8s/`, `tests/tools/`, `tests/auth/`), plus
`test_config.py`, `test_server.py`, and `test_cli.py` at the top level. Run
`tree src/af_jupyterlab_mcp tests` for the current file-by-file breakdown --
deliberately not duplicated here, since a hand-maintained tree has already gone
stale twice (most recently: it didn't mention `k8s/proxy.py`,
`tools/nb_proxy.py`, or their tests after they shipped).

## Tool registration pattern

Mirrors ami-mcp/af-filesystem-mcp: a `register(mcp: MCPServer) -> None` per
module (`tools/jupyterlab.py`'s six CRD-management tools, `tools/nb_proxy.py`'s
16 `nb_*` proxy tools) defines all `@mcp.tool()` closures. `server.py`'s
`_register_all` calls both.

Every tool returns markdown _and_ structured content:
`CallToolResult(content=[...], structured_content=...)`, with the return
annotation spelled `Annotated[CallToolResult, ResultModel]`. This is the escape
hatch the mcp SDK's `func_metadata()` provides specifically for this case (see
`mcp/server/mcpserver/utilities/func_metadata.py`): annotating a tool
`-> ResultModel` directly gets you `outputSchema` + `structuredContent`, but the
SDK then renders the text block as `pydantic_core.to_json(result, indent=2)`,
destroying the curated markdown. `Annotated[CallToolResult, ResultModel]`
publishes `outputSchema` from `ResultModel`, validates `structured_content`
against it at runtime, and returns the `CallToolResult` — markdown text block
and all — unchanged.

```python
# tools/mymodule.py
from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer  # noqa: TC002 (needed at runtime for eval_str signature introspection)
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from af_jupyterlab_mcp.tools._helpers import append_next_actions, format_error


class MyToolResult(BaseModel):
    """Structured result of my_tool."""

    id: str
    # ... the rest of the fields the underlying call actually returns


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="My tool",
            read_only_hint=True,
            open_world_hint=True,
        )
    )
    async def my_tool(
        name: str,
        *,
        ctx: Context[Any, Any],
    ) -> Annotated[CallToolResult, MyToolResult]:
        """Tool description -- shown to the LLM as the tool's purpose."""
        try:
            result = await do_the_thing(ctx, name)
        except Exception as exc:  # noqa: BLE001
            return format_error(exc, hints=["..."])
        text = append_next_actions(str(result), ["..."])
        payload = MyToolResult(id=result["id"])
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload.model_dump(mode="json"),
        )
```

Key conventions:

- The owner of every server is **always** `claims.unixname` from the verified
  broker JWT (`get_broker_claims(ctx, verifier)`) — no tool takes an
  owner/username argument, ever.
- `ctx` is keyword-only (after `*`) so optional parameters can have defaults
  before it.
- `Context`/`MCPServer`/`CallToolResult`/`TextContent`/`ToolAnnotations` and
  every result model used in a return annotation must be imported as **real,
  non-`TYPE_CHECKING`** imports (with a `# noqa: TC002` on the
  `mcp.server.mcpserver` import to satisfy ruff's type-checking-import lint) —
  the mcp SDK's `func_metadata()` calls
  `inspect.signature(func, eval_str=True)`, which needs every name in the
  signature to actually resolve in the function's module globals at _runtime_,
  not just for static type checking. Getting this wrong raises
  `InvalidSignature: Unable to evaluate type annotations` the moment the tool is
  registered.
- Annotations follow the read-only/mutating/destructive split: read-only tools
  get `read_only_hint=True` (`open_world_hint=True` for everything in this repo
  — every tool reaches external k8s or jupyter-mcp-server state); mutating
  non-destructive tools get `read_only_hint=False` with `destructive_hint` left
  unset; destructive tools (including the three arbitrary-code-execution `nb_*`
  tools — "execute" isn't literally a delete, but they can mutate anything the
  kernel can reach) get `read_only_hint=False, destructive_hint=True`.
  `idempotent_hint` stays unset everywhere — the spec says these hints are only
  meaningful when `read_only_hint` is false.
- Errors are returned via `format_error(exc, hints=[...])`, which itself returns
  a `CallToolResult(is_error=True)` — never raised, never a bare
  `f"Error: {exc}"` string, and never a plain error `CallToolResult` built by
  hand at a tool's own call site.
- `tools/nb_proxy.py`'s 16 tools proxy to jupyter-mcp-server, which returns
  markdown/plain text for every tool, not structured data — there is nothing
  further to extract without coupling to its undocumented text format, so all 16
  share one minimal wrapper model, `NbProxyResult({"result": <str>})`, via the
  shared `_call_upstream` helper. `_get_ready_pod_and_token` and
  `call_notebook_tool` raise typed exceptions
  (`NotFoundOrNotYoursError`/`NotebookNotReadyError`/
  `NotebookToolTransportError`/`NotebookToolUpstreamError`) on failure — never
  return a plain error string a caller has to `isinstance`-sniff.
- All `kubernetes` client calls are blocking (the SDK has no asyncio support),
  so every k8s-layer call from a tool goes through `asyncio.to_thread(...)` to
  keep the MCP event loop responsive.
- The k8s layer (`k8s/notebooks.py`, `k8s/gpu.py`) takes a `K8sClients` bundle
  (`core_v1` + `networking_v1`) as its first argument, never constructs one
  itself — this is what makes it testable against `tests/k8s/fakes.py` with no
  cluster access.

## Build and test commands

```bash
pixi run test          # quick tests (no cluster needed -- fully faked k8s client)
pixi run lint           # pre-commit + pylint
pixi run helm-lint      # lint + smoke-render the Helm chart (missing brokerUrl/notebook.namespace fails loudly)
pixi run build          # build sdist + wheel
```

## Auth: broker-issued JWTs only

Unlike ami-mcp (which supports both a shared-secret mode and a broker mode),
af-jupyterlab-mcp is AF-native and broker-only: `af-credentials` is a **hard**
dependency, not an optional extra, and there is no non-broker way to run this
server. See `src/af_jupyterlab_mcp/auth/broker.py`'s module docstring for the
two-verification-per-request shape (the SDK's `token_verifier=` drops POSIX
claims; `get_broker_claims` re-verifies via the same
`BrokerTokenVerifier.verify(token)` to recover `unixname`/`uid`).

**Do not** import `af_credentials.verifier`/`af_credentials.mcp` anywhere except
`auth/broker.py` and `server.py` — keep the token-verification surface in one
place.

## Dual-writer safety (af-portal AND af-jupyterlab-mcp both create notebooks)

- K8s object names are the lock: `sanitize_k8s_pod_name` +
  `notebook_name_available` (ported from af-portal, kept name-for-name identical
  so `<owner>-notebook-N` collisions are detected the same way by both writers).
- A 409 on **any** of the four creates (pod/service/secret/ingress) is a hard
  error — `k8s/notebooks.py`'s `_rollback` deletes whatever was already created
  in that call, in reverse order. Never replicate af-portal's patch-on-409
  fallback (`deploy_notebook`'s
  `except ApiException: if e.status == 409: api.patch_...`) — that silently
  adopts an existing same-named object, which is fine for the portal's own
  re-deploys but wrong for a second, independent writer.

## Template porting rules

The four templates in `k8s/templates/*.yaml.j2` are a verbatim port of
af-portal's `portal/templates/jupyterlab/{pod,service,secret,ingress}.yaml`,
with exactly two deliberate divergences (nothing else):

1. every object gets a `created-by: af-jupyterlab-mcp` label (audit only; the
   portal ignores labels it does not recognize).
2. the pod's `globus-id` label is **omitted** — broker JWTs carry no Globus ID.
   This is af-mcp-platform issue #189's open question 2, deliberately left open,
   not resolved here — check the issue before "fixing" this.

Do not add, remove, or rename any other field without checking whether
af-portal's reaper thread (`start_notebook_maintenance`, which selects on
`k8s-app=jupyterlab` and reads `time2delete`) still recognizes the object —
af-jupyterlab-mcp deliberately runs no reaper of its own (decision 2 in issue
#189): af-portal's Flask reaper thread is the sole TTL reaper for both writers'
pods.

## Server-side guardrails

CPU (1-16 cores), memory (1-256Gi), and duration (1-72h, default 8h) are
**validation**, not quota — always enforced, in `k8s/guardrails.py`, regardless
of whether the optional quota knobs (`Settings.max_servers_per_user` /
`max_gpus_per_request`, both `None` / unset by default) are configured via Helm
values. Do not move these ranges behind a config flag — af-mcp-platform issue
#189 is explicit that the portal's own 72h cap is enforced client-side only (a
known gap, filed separately), and this backend must not repeat that mistake.

## RBAC

Two distinct grants, both templated in `charts/af-jupyterlab-mcp/templates/`:

- A namespace `Role` + cross-namespace `RoleBinding` in the notebook namespace
  (pods/services/secrets get+list+create+delete, pods/log get, events list,
  ingresses get+list+create+delete). RBAC cannot scope by label selector, so the
  `owner=` check is enforced entirely at the application layer
  (`k8s/notebooks.py`), not by Kubernetes.
- A read-only, cluster-scoped `ClusterRole` + `ClusterRoleBinding` (`nodes`
  get+list, `pods` list across all namespaces) for GPU-availability parity with
  af-portal's cluster-wide accounting — a namespace-local approximation would
  undercount whenever non-notebook workloads share a GPU node.

## Adding a new tool

1. Add a new `@mcp.tool(annotations=ToolAnnotations(...))` function inside
   `tools/jupyterlab.py`'s or `tools/nb_proxy.py`'s `register()` (or a new
   module + `server.py`'s `_register_all` entry, if it doesn't belong with
   either existing group).
2. Define a pydantic `BaseModel` for the tool's structured result and spell the
   return annotation `Annotated[CallToolResult, ResultModel]`; return
   `CallToolResult(content=[TextContent(...)], structured_content=...)` per the
   "Tool registration pattern" section above. Assign `ToolAnnotations` per the
   read-only/mutating/destructive split.
3. Any new k8s call belongs in `k8s/notebooks.py` or a new `k8s/*.py` module
   taking `K8sClients` as its first argument — never call `kubernetes.client`
   directly from `tools/`.
4. Write unit tests using `tests/k8s/fakes.py`'s `FakeCoreV1Api` /
   `FakeNetworkingV1Api` — no cluster access is available or expected in this
   test suite. Use the `tool_text` fixture (`tests/conftest.py`) to unwrap
   `CallToolResult.content[0].text` for markdown substring assertions, and
   assert on `result.structured_content`/`result.is_error` directly for the
   structured/error paths.
5. Add the new tool's name to the appropriate bucket in
   `TestAnnotationsAndOutputSchema`/`TestNbProxyAnnotationsAndOutputSchema`
   (per-module) and confirm `tests/test_server.py`'s
   `TestEveryToolDeclaresAnnotationsAndOutputSchema` tool-count assertion is
   updated to match.
6. Run `pixi run test` and `pixi run lint` to verify.
