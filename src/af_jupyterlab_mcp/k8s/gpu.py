"""GPU availability: port of af-portal's node-labels + pod-requests math.

See ``portal/jupyterlab.py``'s ``get_gpu_availability`` docstring for the
full algorithm description; this is a straight port onto ``K8sClients``.
GPU-availability parity with the portal requires read-only cluster-scoped
reads (``nodes`` get+list, ``pods`` list across namespaces) -- see the
ClusterRole in ``charts/af-jupyterlab-mcp/templates/clusterrole.yaml``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from kubernetes.utils.quantity import parse_quantity

if TYPE_CHECKING:
    from af_jupyterlab_mcp.k8s.notebooks import K8sClients

_SUCCEEDED = "Succeeded"
_FAILED = "Failed"


def get_gpu_availability(
    clients: K8sClients, product: str | None = None, memory: int | None = None
) -> list[dict[str, Any]]:
    """Return per-GPU-product availability across the cluster.

    Args:
        clients: the notebook-namespace-scoped + cluster-read-only clients.
        product: optional GPU product name filter.
        memory: optional GPU memory (MiB) filter, used only when product is
            not given.

    Returns:
        A list of dicts (one per unique GPU product), sorted by memory.
    """
    gpus: dict[str, dict[str, Any]] = {}
    api = clients.core_v1
    if product:
        nodes = api.list_node(
            label_selector=f"gpu=true,nvidia.com/gpu.product={product}"
        )
    elif memory:
        nodes = api.list_node(label_selector=f"gpu=true,nvidia.com/gpu.memory={memory}")
    else:
        nodes = api.list_node(label_selector="nvidia.com/gpu.product")

    for node in nodes.items:
        node_product = node.metadata.labels["nvidia.com/gpu.product"]
        node_memory = int(node.metadata.labels["nvidia.com/gpu.memory"])
        count = int(node.metadata.labels["nvidia.com/gpu.count"])
        if node_product not in gpus:
            gpus[node_product] = {
                "mem_request_max": 0,
                "cpu_request_max": 0,
                "product": node_product,
                "memory": node_memory,
                "count": count,
                "total_requests": 0,
            }
        else:
            gpus[node_product]["count"] += count
        gpu = gpus[node_product]

        pods = api.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node.metadata.name},status.phase!={_SUCCEEDED},status.phase!={_FAILED}"
        ).items
        mem_request = 0
        cpu_request = 0
        gpu_request = 0
        for pod in pods:
            for container in pod.spec.containers:
                requests = container.resources.requests
                if requests:
                    gpu["total_requests"] += int(requests.get("nvidia.com/gpu", 0))
                    gpu_request += int(requests.get("nvidia.com/gpu", 0))
                    mem_request += parse_quantity(requests.get("memory", 0))
                    cpu_request += parse_quantity(requests.get("cpu", 0))

        # count in max only when there is at least 1 gpu available; the
        # limitation is this guard is only safe if the requested gpu count
        # is not more than 1 (portal parity).
        if int(node.status.capacity["nvidia.com/gpu"]) > gpu_request:
            mem_request_max = math.floor(
                (parse_quantity(node.status.capacity["memory"]) - mem_request)
                / (1024 * 1024 * 1024)
            )
            cpu_request_max = math.floor(
                parse_quantity(node.status.capacity["cpu"]) - cpu_request
            )
            gpu["mem_request_max"] = max(gpu["mem_request_max"], mem_request_max)
            gpu["cpu_request_max"] = max(gpu["cpu_request_max"], cpu_request_max)
        gpu["available"] = max(gpu["count"] - gpu["total_requests"], 0)

    return sorted(gpus.values(), key=lambda gpu: gpu["memory"])
