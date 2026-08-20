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
LLM be expressive), this backend exposes six fixed, typed tools over Kubernetes
Pod/Service/Secret/Ingress objects. There is no raw-k8s-manifest escape hatch:
the value here is the AF-specific policy layered on top (guardrail validation,
dual-writer safety, owner-scoping), not a thin pass-through to the Kubernetes
API.

Phase 1 (this repo, today) is the six CRD-management tools only. Phase 2
(tracked in
[maniaclab/af-mcp-platform#189](https://github.com/maniaclab/af-mcp-platform/issues/189),
not built here) adds a typed proxy to the Datalayer `jupyter-mcp-server` running
inside the notebook itself, so a session can drive code execution inside the
user's own notebook without the notebook token ever entering LLM context.

## Project layout

```
src/af_jupyterlab_mcp/
├── cli.py               # argparse: `af-jupyterlab-mcp serve` (HTTP transport only)
├── config.py             # env-driven Settings + the server-side guardrail constants
├── server.py             # FastMCP setup, lifespan (k8s client + broker verifier), tool registration
├── auth/
│   └── broker.py          # extract_bearer(), get_broker_claims() -- broker-issued JWT verification
├── k8s/
│   ├── errors.py           # GuardrailError, NameConflictError, NotFoundOrNotYoursError, QuotaExceededError, ...
│   ├── guardrails.py       # CPU/memory/duration range + image allowlist validation (compute_limits)
│   ├── names.py            # sanitize_k8s_pod_name, name availability, default-name generation
│   ├── templates.py        # Jinja rendering of the four ported manifests
│   ├── notebooks.py        # create/get/list/delete notebook (ported af-portal logic + rollback/owner-scoping)
│   ├── gpu.py               # get_gpu_availability (ported af-portal logic)
│   └── templates/           # pod.yaml.j2, service.yaml.j2, secret.yaml.j2, ingress.yaml.j2
│                             # verbatim port of af-portal/portal/templates/jupyterlab/*.yaml
└── tools/
    ├── _helpers.py          # format_error(), append_next_actions(), format_notebook[_list]()
    └── jupyterlab.py         # the six @mcp.tool() functions
tests/
├── conftest.py (none needed yet -- fixtures live per-module)
├── auth/test_broker.py       # bearer extraction + claims retrieval
├── k8s/
│   ├── fakes.py                # in-memory kubernetes client stand-in (no cluster access needed)
│   ├── test_names.py
│   ├── test_guardrails.py
│   ├── test_templates.py
│   ├── test_notebooks.py       # owner-scoping, 409-rollback, quota tests
│   └── test_gpu.py
├── tools/test_jupyterlab.py   # the six tools registered + invoked end-to-end against fakes
├── test_server.py             # HTTP transport + broker-mode ASGI app (real af_credentials, no mocks of our own auth code)
└── test_cli.py
```

## Tool registration pattern

Mirrors ami-mcp: a single `register(mcp: MCPServer) -> None` in
`tools/jupyterlab.py` defines all six `@mcp.tool()` closures. `server.py` calls
`jupyterlab_tools.register(mcp)`.

Key conventions:

- The owner of every server is **always** `claims.unixname` from the verified
  broker JWT (`get_broker_claims(ctx, verifier)`) — no tool takes an
  owner/username argument, ever.
- `ctx` is keyword-only (after `*`) so optional parameters can have defaults
  before it.
- Errors are returned via `format_error(exc, hints=[...])` — never raised as
  bare exceptions to the LLM.
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

1. Add a new `@mcp.tool()` function inside `tools/jupyterlab.py`'s `register()`
   (or a new module + `server.py` registration loop entry, if it doesn't belong
   with the CRD tools).
2. Any new k8s call belongs in `k8s/notebooks.py` or a new `k8s/*.py` module
   taking `K8sClients` as its first argument — never call `kubernetes.client`
   directly from `tools/`.
3. Write unit tests using `tests/k8s/fakes.py`'s `FakeCoreV1Api` /
   `FakeNetworkingV1Api` — no cluster access is available or expected in this
   test suite.
4. Run `pixi run test` and `pixi run lint` to verify.
