# af-jupyterlab-mcp Helm chart

Deploys af-jupyterlab-mcp: per-user JupyterLab server management on the ATLAS AF
Kubernetes cluster, behind the af-mcp-platform credential broker.

## Required values

```bash
helm install af-jupyterlab-mcp charts/af-jupyterlab-mcp \
  --namespace mcp \
  --set broker.brokerUrl=https://mcp.af.uchicago.edu \
  --set notebook.namespace=jupyterlab \
  --set notebook.domain=notebooks.af.uchicago.edu
```

`broker.brokerUrl` and `notebook.namespace` are required; the chart fails the
render (`helm template`/`helm install --dry-run`) with a clear message if either
is missing.

## Platform wiring (af-mcp-platform)

This chart deploys the backend only. To route the aggregator/broker to it, add
to af-mcp-platform's `values.yaml` (camelCase, per the shipped convention in
`docs/auth.md` -- NOT the snake_case in issue #189's original body):

```yaml
broker:
  identityProviders:
    - type: broker-issued
      alias: af-jupyterlab-mcp
      displayName: "AF-native services"
      targets: ["af-jupyterlab-mcp"]
      targetOptions:
        af-jupyterlab-mcp: { audience: "af-jupyterlab-mcp", includePosix: true }

aggregator:
  backends:
    - name: af-jupyterlab-mcp
      prefix: jupyterlab
      url: "http://af-jupyterlab-mcp.mcp.svc.cluster.local:80/mcp"
      transport: http
      required_capability: manage_jupyter
      auth_type: bearer
      timeout_seconds: 120 # headroom for phase 2's nested execute calls; harmless now
```

This replaces the `jupyter-control` placeholder entry in the default
`backends.yaml` (see the top-level report for the exact diff -- not applied
here; live platform config changes need a human to apply them).

## RBAC

- A namespace `Role` + `RoleBinding` in `notebook.namespace` (NOT this release's
  namespace): pods/services/secrets get+list+create+delete, pods/log get, events
  list, ingresses get+list+create+delete. The `RoleBinding`'s subject is this
  release's `ServiceAccount`, which lives in `.Release.Namespace` (typically
  `mcp`) -- cross-namespace binding is standard Kubernetes RBAC, not a chart
  trick.
- A read-only `ClusterRole` + `ClusterRoleBinding` (`nodes` get+list, `pods`
  list across all namespaces) for GPU-availability parity with af-portal's
  cluster-wide accounting.

Disable either with `rbac.create=false` / `rbac.clusterRoleCreate=false` if you
wire them up out-of-band.

## Values of note

- `notebook.images.cpu` / `notebook.images.gpu`: the image allowlist
  `create_jupyter_server` enforces server-side. Bumping an image is a values
  change, not a release.
- `notebook.quotas.maxServersPerUser` / `maxGpusPerRequest`: unset (empty
  string) by default -- no default quota, per af-mcp-platform#189 decision 4.
  The CPU/memory/duration RANGE guardrails (1-16 cores, 1-256Gi, 1-72h) are NOT
  configurable here; they are always-enforced validation in the server itself.
- `ingress.enabled`: `false` by default. The expected caller is the aggregator,
  in-cluster, via the `Service` -- enable only if you need to reach this backend
  directly.

## Divergence from ami-mcp's chart shape

ami-mcp's chart installs ami-mcp at pod startup via `pixi install` from a
conda-forge release (it ships no OCI image). af-jupyterlab-mcp has no published
package or image yet -- `image.repository`/`image.tag` are placeholders a human
fills in once the repo exists and CI/CD is set up (see the phase-1 report on
af-mcp-platform#189 for the exact deferred items).
