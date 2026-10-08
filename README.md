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

This repo ships three groups of tools: six that manage the Pod/Service/
Secret/Ingress quadruple for a notebook, ported from af-portal's
`portal/jupyterlab.py` and its four Jinja templates; seventeen `nb_*` tools that
proxy calls into the Datalayer `jupyter-mcp-server` running inside the notebook
itself (`4ea435f`), so a session can drive code execution inside the user's own
notebook without the notebook token ever entering LLM context — see
[maniaclab/af-mcp-platform#189](https://github.com/maniaclab/af-mcp-platform/issues/189);
and 52 `nb_ui_*` tools that drive the user's open JupyterLab tab through the
`jupyter-mcp-tools` extension.
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
    ├── nb_proxy.py        # the seventeen nb_* jupyter-mcp-server proxy @mcp.tool() functions
    └── nb_ui.py           # the 52 nb_ui_* JupyterLab frontend-command proxy tools
```

<!-- --8<-- [start:tool-surface] -->

## Tool surface

75 tools total. Every tool's MCP `annotations` declare its
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
| `nb_clear_cell_output`        | Clear a code cell's outputs, keeping the cell           | destructive |

The three code-execution tools (`nb_execute_cell`,
`nb_insert_execute_code_cell`, `nb_execute_code`) are marked destructive even
though "execute" isn't literally a delete: they can mutate anything the kernel
can reach, which is what the annotation communicates to a client.
Cell-addressing tools accept either a positional index or the cell's stable
`cell_id` (given both, the id wins); prefer ids, since an index goes stale as
soon as a cell is inserted above it.

### JupyterLab UI proxy (`tools/nb_ui.py`, upstream: [jupyter-mcp-tools](https://github.com/datalayer/jupyter-mcp-tools))

Every `nb_ui_*` tool proxies one JupyterLab command through the same
ownership/readiness/token path as the `nb_*` tools, but the command runs
**inside the user's open JupyterLab browser tab** (relayed over a websocket by
the `jupyter-mcp-tools` extension) and acts on the active notebook, console, or
selection there. With no tab open, the call fails. The notebook image must
allowlist each command
([maniaclab/ml_platform#14](https://github.com/maniaclab/ml_platform/issues/14)).

Tool names are `nb_ui_` + the upstream id with `-` → `_` (upstream ids are
JupyterLab command ids with `:` → `_`, e.g. `notebook:run-all-cells` →
`notebook_run-all-cells` → `nb_ui_notebook_run_all_cells`). Tools that take
arguments beyond `notebook_server_id` are noted.

| Area         | Tools (`nb_ui_` prefix omitted)                                                                                                                                                                                                                                                                              | Kind        |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------- |
| Notebook     | `notebook_get_selected_cell`, `notebook_move_cursor_down`, `notebook_move_cursor_up`, `notebook_extend_marked_cells_below`, `notebook_extend_marked_cells_above`, `notebook_copy_cell`                                                                                                                       | read-only   |
| Notebook     | `notebook_insert_cell_below`, `notebook_insert_cell_above`, `notebook_paste_cell_below`, `notebook_paste_cell_above`, `notebook_move_cell_up`, `notebook_move_cell_down`, `notebook_split_cell_at_cursor`, `notebook_change_cell_to_code`, `notebook_change_cell_to_markdown`, `notebook_change_cell_to_raw` | mutating    |
| Notebook     | `notebook_delete_cell`, `notebook_cut_cell`, `notebook_merge_cell_above`, `notebook_merge_cell_below`, `notebook_run_all_cells`, `notebook_run_cell`, `notebook_run_cell_and_select_next`, `notebook_run_cell_and_insert_below`, `notebook_append_execute` (`source`, `cell_type`)                           | destructive |
| Console      | `console_create` (`path`, `insert_mode`, `activate`)                                                                                                                                                                                                                                                         | mutating    |
| Console      | `console_clear`, `console_interrupt_kernel`, `console_inject` (`code`, `path`, `activate`)                                                                                                                                                                                                                   | destructive |
| Documents    | `docmanager_open` (`path`, `factory`), `docmanager_new_untitled` (`content_type`, `path`, `ext`), `docmanager_save`, `docmanager_duplicate`                                                                                                                                                                  | mutating    |
| File browser | `filebrowser_go_to_path` (`path`), `filebrowser_refresh`, `filebrowser_toggle_hidden_files`                                                                                                                                                                                                                  | read-only   |
| File browser | `filebrowser_create_new_directory`                                                                                                                                                                                                                                                                           | mutating    |
| Kernel       | `kernelmenu_reconnect_to_kernel`                                                                                                                                                                                                                                                                             | mutating    |
| Kernel       | `kernelmenu_interrupt`, `kernelmenu_shutdown`                                                                                                                                                                                                                                                                | destructive |
| UI           | `application_toggle_left_area`, `application_toggle_right_area`, `application_toggle_presentation_mode`, `apputils_change_theme` (`theme`), `editmenu_open`, `filemenu_open`, `helpmenu_open`                                                                                                                | read-only   |
| Search       | `documentsearch_start` (`search_text`), `documentsearch_highlightNext`, `documentsearch_highlightPrevious`                                                                                                                                                                                                   | read-only   |
| Terminal     | `terminal_create_new`, `terminal_refresh`                                                                                                                                                                                                                                                                    | mutating    |

"Read-only" here includes commands that only change view state (cursor,
selection, layout, search, theme). Commands that execute code are destructive,
like the `nb_execute_*` tools.

Deliberately **not** proxied (and not allowlisted in the image):

- `filebrowser_upload` / `filebrowser_download` — they open a file picker or
  save into the human's browser; no bytes ever reach the MCP client.
- `docmanager_delete` / `docmanager_rename` / `docmanager_save-as`,
  `kernelmenu_change`, `kernelmenu_restart`, `console_restart-kernel` — they
  block on a modal dialog that needs a human click. Use `nb_restart_notebook` to
  restart a kernel without the UI.

<!-- --8<-- [end:tool-surface] -->

## Build and test commands

```bash
pixi run test          # quick tests
pixi run lint          # pre-commit + pylint
pixi run helm-lint      # lint + smoke-render the Helm chart
```
