---
icon: lucide/code
---

# Contributing

## Architecture

```
LLM <--MCP/HTTP--> af-mcp-platform aggregator <--Bearer: broker JWT--> af-jupyterlab-mcp <--k8s API--> notebook namespace
```

`af-jupyterlab-mcp` exposes six fixed, typed tools over Kubernetes
Pod/Service/Secret/Ingress objects for per-user JupyterLab servers on the
UChicago ATLAS Analysis Facility cluster — the same notebooks
[af-portal](https://github.com/maniaclab/af-portal) deploys today:

- `create_jupyter_server`
- `list_jupyter_servers`
- `get_jupyter_server`
- `delete_jupyter_server`
- `get_gpu_availability`
- `list_supported_images`

Unlike ami-mcp (which exposes a query DSL and lets the LLM be expressive), there
is no raw-k8s-manifest escape hatch here: the value is the AF-specific policy
layered on top (guardrail validation, dual-writer safety with af-portal,
owner-scoping via the broker-issued JWT's `unixname`), not a thin pass-through
to the Kubernetes API.

The Helm chart (`charts/af-jupyterlab-mcp/`) runs the generic
`ghcr.io/prefix-dev/pixi` image rather than a published af-jupyterlab-mcp image:
an initContainer installs the pinned `jupyterlabMcp.version` from conda-forge
into `/workspace` via `pixi install --manifest-path /workspace/pixi.toml`
against a ConfigMap-rendered `pixi.toml` (and, when
`jupyterlabMcp.pixiLockContent` is set, a frozen `pixi.lock` for reproducible
installs), and the main container activates that environment with
`pixi shell-hook` before starting the server — the same pattern `ami-mcp` and
`af-filesystem-mcp` use.

## Development setup

```bash
git clone https://github.com/maniaclab/af-jupyterlab-mcp
cd af-jupyterlab-mcp
pixi install
pixi run pre-commit-install
```

## Build and test commands

```bash
pixi run test          # quick tests (no cluster needed -- fully faked k8s client)
pixi run lint           # pre-commit + pylint
pixi run helm-lint      # lint + smoke-render the Helm chart
pixi run build          # build sdist + wheel
pixi run docs-serve     # build and serve docs locally
```
