"""Server-side validation guardrails for create_jupyter_server.

Per af-mcp-platform issue #189: CPU/memory/duration ranges and the image
allowlist are validation, not quota, and are ALWAYS enforced regardless of
whether the optional quota knobs (``Settings.max_servers_per_user`` /
``max_gpus_per_request``) are configured. The portal enforces the 72h
duration cap only in its HTML form (client-side); jupyterlab-mcp enforces it
here, server-side, on every call.
"""

from __future__ import annotations

from jupyterlab_mcp.config import (
    CPU_CORES_MAX,
    CPU_CORES_MIN,
    DURATION_HOURS_MAX,
    DURATION_HOURS_MIN,
    LIMIT_MULTIPLIER,
    MEMORY_GB_MAX,
    MEMORY_GB_MIN,
    Settings,
)
from jupyterlab_mcp.k8s.errors import GuardrailError, ImageNotAllowedError


def validate_create_request(
    *,
    image: str,
    cpu_cores: int,
    memory_gb: int,
    gpus: int,
    duration_hours: int,
    settings: Settings,
) -> None:
    """Validate a create_jupyter_server request against server-side guardrails.

    Raises:
        GuardrailError: A range or quota guardrail was violated.
        ImageNotAllowedError: The image is not on the configured allowlist.
    """
    if not CPU_CORES_MIN <= cpu_cores <= CPU_CORES_MAX:
        msg = (
            f"cpu_cores={cpu_cores} is out of range [{CPU_CORES_MIN}, {CPU_CORES_MAX}]"
        )
        raise GuardrailError(msg)

    if not MEMORY_GB_MIN <= memory_gb <= MEMORY_GB_MAX:
        msg = (
            f"memory_gb={memory_gb} is out of range [{MEMORY_GB_MIN}, {MEMORY_GB_MAX}]"
        )
        raise GuardrailError(msg)

    if not DURATION_HOURS_MIN <= duration_hours <= DURATION_HOURS_MAX:
        msg = (
            f"duration_hours={duration_hours} is out of range "
            f"[{DURATION_HOURS_MIN}, {DURATION_HOURS_MAX}]"
        )
        raise GuardrailError(msg)

    if gpus < 0:
        msg = f"gpus={gpus} must be >= 0"
        raise GuardrailError(msg)

    if (
        settings.max_gpus_per_request is not None
        and gpus > settings.max_gpus_per_request
    ):
        msg = (
            f"gpus={gpus} exceeds the configured max_gpus_per_request="
            f"{settings.max_gpus_per_request}"
        )
        raise GuardrailError(msg)

    if image not in settings.all_images:
        msg = f"image {image!r} is not on the supported-images allowlist"
        raise ImageNotAllowedError(msg)


def compute_limits(*, cpu_cores: int, memory_gb: int) -> tuple[int, int]:
    """Return (cpu_limit, memory_limit_gb) as 2x the requested cpu/memory.

    Portal ``validate_notebook`` parity: limit = 2x request.
    """
    return cpu_cores * LIMIT_MULTIPLIER, memory_gb * LIMIT_MULTIPLIER
