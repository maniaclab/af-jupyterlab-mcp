# af-jupyterlab-mcp

<!-- --8<-- [start:intro] -->

MCP server that lets AF users create, inspect, and delete their own per-user
JupyterLab servers on the UChicago ATLAS Analysis Facility Kubernetes cluster —
the same notebooks [af-portal](https://github.com/maniaclab/af-portal) deploys
today, exposed as tools for LLMs.
<!-- --8<-- [end:intro] -->

<!-- --8<-- [start:architecture] -->

## Architecture

```
LLM <--MCP/HTTP--> af-jupyterlab-mcp <--k8s API--> notebook namespace (Pod/Service/Secret/Ingress)
                         ^
                         | Authorization: Bearer <broker-issued JWT>
                         |
              af-mcp-platform credential broker
```

This repo ships two groups of tools: six that manage the Pod/Service/
Secret/Ingress quadruple for a notebook, ported from af-portal's
`portal/jupyterlab.py` and its four Jinja templates, and sixteen `nb_*` tools
that proxy calls into the Datalayer `jupyter-mcp-server` running inside the
notebook itself (`4ea435f`), so a session can drive code execution inside the
user's own notebook without the notebook token ever entering LLM context — see
[maniaclab/af-mcp-platform#189](https://github.com/maniaclab/af-mcp-platform/issues/189).
<!-- --8<-- [end:architecture] -->

## Project layout

```
src/af_jupyterlab_mcp/
├── cli.py               # argparse: `af-jupyterlab-mcp serve` (HTTP only)
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
│   ├── proxy.py          # call_notebook_tool -- MCP client that proxies into jupyter-mcp-server
│   └── templates/        # pod.yaml.j2, service.yaml.j2, secret.yaml.j2, ingress.yaml.j2
│                          # (ported verbatim from af-portal/portal/templates/jupyterlab/)
└── tools/
    ├── _helpers.py        # format_error(), append_next_actions(), format_notebook[_list]()
    ├── jupyterlab.py      # the six CRD-management @mcp.tool() functions
    └── nb_proxy.py        # the sixteen nb_* jupyter-mcp-server proxy @mcp.tool() functions
```

<!-- --8<-- [start:tool-surface] -->

## Tool surface

22 tools total. Every tool's MCP `annotations` declare its
read-only/mutating/destructive status (see `CLAUDE.md`'s "Tool registration
pattern"); the column below mirrors that.

### Notebook server management (`k8s/notebooks.py`, `k8s/gpu.py`)

| Tool                    | Does                                                                 | Kind        |
| ----------------------- | -------------------------------------------------------------------- | ----------- |
| `create_jupyter_server` | Create a per-user JupyterLab server (pod+service+secret+ingress)     | mutating    |
| `list_jupyter_servers`  | List the caller's own JupyterLab servers                             | read-only   |
| `get_jupyter_server`    | Get rich status for one of the caller's own JupyterLab servers       | read-only   |
| `delete_jupyter_server` | Delete one of the caller's own JupyterLab servers (all four objects) | destructive |
| `get_gpu_availability`  | Get cluster-wide GPU availability, optionally filtered by product    | read-only   |
| `list_supported_images` | List the CPU and GPU images allowed by `create_jupyter_server`       | read-only   |

The owner of every server is always `claims.unixname` from the verified broker
JWT — no tool takes an owner/username argument.

### Notebook content proxy (`k8s/proxy.py`, upstream: [jupyter-mcp-server](https://github.com/datalayer/jupyter-mcp-server))

Every `nb_*` tool takes `notebook_server_id` first, verifies the caller owns
that pod, checks it is `Ready`, and forwards the call to the notebook's own
`jupyter-mcp-server` with the notebook token injected server-side (never
returned to the caller).

| Tool                          | Does                                                    | Kind        |
| ----------------------------- | ------------------------------------------------------- | ----------- |
| `nb_list_files`               | List files on the notebook server's filesystem          | read-only   |
| `nb_list_kernels`             | List all running kernels on the notebook server         | read-only   |
| `nb_list_notebooks`           | List notebooks open on the notebook server              | read-only   |
| `nb_use_notebook`             | Connect to or create a notebook on the notebook server  | mutating    |
| `nb_unuse_notebook`           | Disconnect from a notebook on the notebook server       | mutating    |
| `nb_restart_notebook`         | Restart a notebook's kernel on the notebook server      | destructive |
| `nb_read_notebook`            | Read a notebook's cells from the notebook server        | read-only   |
| `nb_read_cell`                | Read a cell from the active notebook                    | read-only   |
| `nb_insert_cell`              | Insert a cell at a given index in the active notebook   | mutating    |
| `nb_overwrite_cell_source`    | Overwrite the source of a cell in the active notebook   | destructive |
| `nb_edit_cell_source`         | Edit part of a cell's source in the active notebook     | mutating    |
| `nb_delete_cell`              | Delete one or more cells from the active notebook       | destructive |
| `nb_move_cell`                | Move a cell to a different index in the active notebook | mutating    |
| `nb_execute_cell`             | Execute a specific cell in the active notebook          | destructive |
| `nb_insert_execute_code_cell` | Insert a code cell and immediately execute it           | destructive |
| `nb_execute_code`             | Execute arbitrary code in the notebook server's kernel  | destructive |

The three code-execution tools (`nb_execute_cell`,
`nb_insert_execute_code_cell`, `nb_execute_code`) are marked destructive even
though "execute" isn't literally a delete: they can mutate anything the kernel
can reach, which is what the annotation communicates to a client.
`nb_get_selected_cell` and `nb_run_all_cells` are intentionally absent — they
require the `jupyter-mcp-tools` JupyterLab frontend extension, not installed in
the current notebook images.
<!-- --8<-- [end:tool-surface] -->

## Build and test commands

```bash
pixi run test          # quick tests
pixi run lint          # pre-commit + pylint
pixi run helm-lint      # lint + smoke-render the Helm chart
```
