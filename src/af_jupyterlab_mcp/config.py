"""Environment-driven configuration for af-jupyterlab-mcp.

Every knob here has a chart-values-driven env var so an operator can change
notebook limits, the image allowlist, or quotas without a code release (see
``charts/af-jupyterlab-mcp/values.yaml`` -> ``templates/deployment.yaml``).
Quota knobs default to ``None`` (unset / unenforced) per decision 4 in
af-mcp-platform issue #189: no default per-user quota, but the service must
support turning one on via configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Server-side guardrail bounds (issue #189 "Server-side guardrails"). These
# are validation, not quota, and are ALWAYS enforced regardless of the
# optional quota knobs below.
CPU_CORES_MIN = 1
CPU_CORES_MAX = 16
MEMORY_GB_MIN = 1
MEMORY_GB_MAX = 256
DURATION_HOURS_MIN = 1
DURATION_HOURS_MAX = 72
DURATION_HOURS_DEFAULT = 8

# Resource limit = 2x the request (portal `validate_notebook` parity).
LIMIT_MULTIPLIER = 2


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _env_int_or_none(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, normally built once in the server lifespan."""

    # The Kubernetes namespace holding notebook Pod/Service/Secret/Ingress
    # objects. This is the portal's own notebook namespace -- see open
    # question 1 in issue #189 (must be dedicated to notebooks; the `secrets`
    # RBAC grant is only safe if nothing else lives here).
    notebook_namespace: str = "jupyterlab"
    # DNS domain notebooks are exposed under: <notebook-id>.<domain>.
    domain: str = "notebooks.af.uchicago.edu"

    # Image allowlist, chart-values-driven (decision: "image must be on an
    # allowlist from chart values" -- see issue #189 guardrails).
    cpu_images: tuple[str, ...] = field(default_factory=tuple)
    gpu_images: tuple[str, ...] = field(default_factory=tuple)

    # Optional quota knobs (helm values), OFF by default per decision 4.
    max_servers_per_user: int | None = None
    max_gpus_per_request: int | None = None

    # Portal browse URL surfaced by list_jupyter_servers.
    # Set via JUPYTERLAB_MCP_PORTAL_URL. No default -- left None if unset,
    # in which case list_jupyter_servers omits the portal link.
    portal_url: str | None = None

    @property
    def all_images(self) -> tuple[str, ...]:
        """Return the deduplicated union of the CPU and GPU image allowlists."""
        seen: dict[str, None] = {}
        for image in (*self.cpu_images, *self.gpu_images):
            seen.setdefault(image, None)
        return tuple(seen)

    @classmethod
    def from_env(cls) -> Settings:
        """Build Settings from the JUPYTERLAB_MCP_* environment variables."""
        kwargs: dict[str, object] = {}
        if namespace := os.environ.get("JUPYTERLAB_MCP_NAMESPACE"):
            kwargs["notebook_namespace"] = namespace
        if domain := os.environ.get("JUPYTERLAB_MCP_DOMAIN"):
            kwargs["domain"] = domain
        if cpu_images := os.environ.get("JUPYTERLAB_MCP_CPU_IMAGES"):
            kwargs["cpu_images"] = _split_csv(cpu_images)
        if gpu_images := os.environ.get("JUPYTERLAB_MCP_GPU_IMAGES"):
            kwargs["gpu_images"] = _split_csv(gpu_images)
        max_servers = _env_int_or_none("JUPYTERLAB_MCP_MAX_SERVERS_PER_USER")
        if max_servers is not None:
            kwargs["max_servers_per_user"] = max_servers
        max_gpus = _env_int_or_none("JUPYTERLAB_MCP_MAX_GPUS_PER_REQUEST")
        if max_gpus is not None:
            kwargs["max_gpus_per_request"] = max_gpus
        if portal_url := os.environ.get("JUPYTERLAB_MCP_PORTAL_URL"):
            kwargs["portal_url"] = portal_url
        return cls(**kwargs)  # type: ignore[arg-type]
