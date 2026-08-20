"""Pod-name sanitization, availability checks, and default-name generation.

Ported from af-portal's ``portal/jupyterlab.py`` (``sanitize_k8s_pod_name``,
``notebook_name_available``, ``generate_notebook_name``) so mcp-created and
portal-created notebooks share one naming convention and one collision rule
-- the dual-writer safety net described in af-mcp-platform issue #189
("K8s object names are the lock").
"""

from __future__ import annotations

import re
from typing import Any

_MAX_GENERATE_ATTEMPTS = 20


def sanitize_k8s_pod_name(name: str, max_length: int = 63) -> str:
    """Sanitize a string to be a valid Kubernetes pod name.

    - Converts to lowercase.
    - Replaces invalid characters with '-'.
    - Ensures it starts and ends with an alphanumeric character.
    - Trims to the maximum allowed length (default: 63).
    """
    name = name.lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = name.strip("-")
    name = name[:max_length]
    name = name.rstrip("-")
    return name or "default-pod"


def notebook_name_available(api: Any, namespace: str, name: str) -> bool:
    """Return whether *name* is free to use for a new notebook Pod."""
    pods = api.list_namespaced_pod(
        namespace, field_selector=f"metadata.name={name.lower()}"
    )
    return len(pods.items) == 0


def generate_notebook_name(api: Any, namespace: str, owner: str) -> str | None:
    """Return a default notebook name that is available, e.g. owner-notebook-3.

    Returns None if no slot in 1..19 is free (matches the portal's own
    generate_notebook_name behavior).
    """
    for i in range(1, _MAX_GENERATE_ATTEMPTS):
        candidate = f"{owner}-notebook-{i}"
        if notebook_name_available(api, namespace, candidate):
            return candidate
    return None
