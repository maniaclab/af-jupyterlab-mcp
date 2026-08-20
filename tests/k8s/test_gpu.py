"""Tests for the ported get_gpu_availability node-labels + pod-requests math."""

from __future__ import annotations

from af_jupyterlab_mcp.k8s.gpu import get_gpu_availability
from af_jupyterlab_mcp.k8s.notebooks import K8sClients

from .fakes import FakeCoreV1Api, FakeNetworkingV1Api, make_gpu_pod, make_node


def _clients_with(core: FakeCoreV1Api) -> K8sClients:
    return K8sClients(core_v1=core, networking_v1=FakeNetworkingV1Api(core=core))


class TestGetGpuAvailability:
    def test_no_gpu_nodes_returns_empty(self) -> None:
        clients = _clients_with(FakeCoreV1Api())
        assert get_gpu_availability(clients) == []

    def test_single_node_full_availability(self) -> None:
        core = FakeCoreV1Api()
        core.nodes["node-1"] = make_node(
            "node-1", product="A100", memory=40536, count=1, gpu_capacity=1
        )
        clients = _clients_with(core)
        result = get_gpu_availability(clients)
        assert len(result) == 1
        assert result[0]["product"] == "A100"
        assert result[0]["count"] == 1
        assert result[0]["available"] == 1
        assert result[0]["total_requests"] == 0

    def test_availability_reduced_by_pod_requests(self) -> None:
        core = FakeCoreV1Api()
        core.nodes["node-1"] = make_node(
            "node-1", product="A100", memory=40536, count=2, gpu_capacity=2
        )
        core.pods[("other", "user-nb-1")] = make_gpu_pod(
            name="user-nb-1", node_name="node-1", gpu_request=1
        )
        clients = _clients_with(core)
        result = get_gpu_availability(clients)
        assert result[0]["available"] == 1
        assert result[0]["total_requests"] == 1

    def test_filter_by_product(self) -> None:
        core = FakeCoreV1Api()
        core.nodes["node-1"] = make_node(
            "node-1", product="A100", memory=40536, count=1, gpu_capacity=1
        )
        core.nodes["node-2"] = make_node(
            "node-2", product="V100", memory=16384, count=1, gpu_capacity=1
        )
        clients = _clients_with(core)
        result = get_gpu_availability(clients, product="V100")
        assert [r["product"] for r in result] == ["V100"]

    def test_multiple_nodes_same_product_aggregate_count(self) -> None:
        core = FakeCoreV1Api()
        core.nodes["node-1"] = make_node(
            "node-1", product="A100", memory=40536, count=1, gpu_capacity=1
        )
        core.nodes["node-2"] = make_node(
            "node-2", product="A100", memory=40536, count=1, gpu_capacity=1
        )
        clients = _clients_with(core)
        result = get_gpu_availability(clients)
        assert len(result) == 1
        assert result[0]["count"] == 2
        assert result[0]["available"] == 2

    def test_sorted_by_memory(self) -> None:
        core = FakeCoreV1Api()
        core.nodes["big"] = make_node(
            "big", product="A100", memory=40536, count=1, gpu_capacity=1
        )
        core.nodes["small"] = make_node(
            "small", product="T4", memory=16384, count=1, gpu_capacity=1
        )
        clients = _clients_with(core)
        result = get_gpu_availability(clients)
        assert [r["product"] for r in result] == ["T4", "A100"]
