"""Render the four ported Jinja templates into Kubernetes manifest dicts.

The templates in ``af_jupyterlab_mcp/k8s/templates/*.yaml.j2`` are a verbatim
port of af-portal's ``portal/templates/jupyterlab/{pod,service,secret,
ingress}.yaml`` (see cross-pointing comment in that repo), with three
deliberate divergences (the first two called for by af-mcp-platform issue
#189):

  1. every object gets a ``created-by: af-jupyterlab-mcp`` label (audit; the
     portal ignores labels it does not recognize, so this is safe for its
     reaper and its own re-deploy logic).
  2. the pod's ``globus-id`` label is omitted -- a broker-issued JWT carries
     no Globus ID (see issue #189 open question 2, deliberately left open,
     not silently resolved here).
  3. the pod's ``cpu`` and ``nvidia.com/gpu`` resource quantities are quoted
     so int template vars still parse as YAML strings -- the kubernetes
     client rejects non-string quantities in ``create_namespaced_pod``
     bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_KIND_TO_FILENAME = {
    "pod": "pod.yaml.j2",
    "service": "service.yaml.j2",
    "secret": "secret.yaml.j2",
    "ingress": "ingress.yaml.j2",
}


def render_manifests(**template_vars: Any) -> dict[str, dict[str, Any]]:
    """Render the pod/service/secret/ingress manifests for one notebook.

    Args:
        **template_vars: notebook_id, notebook_name, namespace, domain_name,
            owner, owner_uid, image, token, start_script, cpu_request,
            cpu_limit, memory_request, memory_limit, gpu_request, gpu_limit,
            gpu_product, hours_remaining.

    Returns:
        A dict with keys "pod", "service", "secret", "ingress", each mapping
        to the parsed manifest (a plain dict, ready for the kubernetes client).
    """
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))
    manifests: dict[str, dict[str, Any]] = {}
    for kind, filename in _KIND_TO_FILENAME.items():
        template = env.get_template(filename)
        rendered = template.render(**template_vars)
        manifests[kind] = yaml.safe_load(rendered)
    return manifests
