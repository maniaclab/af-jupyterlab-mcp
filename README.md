# jupyterlab-mcp

MCP server that lets AF users create, inspect, and delete their own per-user
JupyterLab servers on the UChicago ATLAS Analysis Facility Kubernetes cluster —
the same notebooks [af-portal](https://github.com/maniaclab/af-portal) deploys
today, exposed as tools for LLMs.

## Architecture

```
LLM <--MCP/HTTP--> jupyterlab-mcp <--k8s API--> notebook namespace (Pod/Service/Secret/Ingress)
                         ^
                         | Authorization: Bearer <broker-issued JWT>
                         |
              af-mcp-platform credential broker
```

Phase 1 (this repo, today) ships six tools that manage the Pod/Service/
Secret/Ingress quadruple for a notebook, ported from af-portal's
`portal/jupyterlab.py` and its four Jinja templates. Phase 2 (tracked, not yet
built) adds a typed proxy to the Datalayer `jupyter-mcp-server` running inside
the notebook itself — see
[maniaclab/af-mcp-platform#189](https://github.com/maniaclab/af-mcp-platform/issues/189).

## Project layout

```
src/jupyterlab_mcp/
├── cli.py               # argparse: `jupyterlab-mcp serve` (HTTP only)
├── config.py            # env-driven Settings: namespace, domain, image allowlist, quotas
├── server.py            # FastMCP setup, lifespan (k8s client + broker verifier), tool registration
├── auth/
│   └── broker.py        # extract_bearer(), get_broker_claims() -- broker-issued JWT verification
├── k8s/
│   ├── errors.py         # GuardrailError, NameConflictError, NotFoundOrNotYoursError, ...
│   ├── guardrails.py     # CPU/memory/duration range + image allowlist validation
│   ├── names.py          # sanitize_k8s_pod_name, name availability, name generation
│   ├── templates.py      # Jinja rendering of the four ported manifests
│   ├── notebooks.py      # create/get/list/delete notebook (ported portal logic)
│   ├── gpu.py            # get_gpu_availability (ported portal logic)
│   └── templates/        # pod.yaml.j2, service.yaml.j2, secret.yaml.j2, ingress.yaml.j2
│                          # (ported verbatim from af-portal/portal/templates/jupyterlab/)
└── tools/
    └── jupyterlab.py     # the six @mcp.tool() functions
```

## Tool surface

- `create_jupyter_server`
- `list_jupyter_servers`
- `get_jupyter_server`
- `delete_jupyter_server`
- `get_gpu_availability`
- `list_supported_images`

The owner of every server is always `claims.unixname` from the verified broker
JWT — no tool takes an owner/username argument.

## Build and test commands

```bash
pixi run test          # quick tests
pixi run lint          # pre-commit + pylint
pixi run helm-lint      # lint + smoke-render the Helm chart
```
