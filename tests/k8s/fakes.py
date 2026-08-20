"""A faked ``kubernetes`` client for jupyterlab-mcp's unit tests.

Real end-to-end verification against a live cluster is explicitly out of
scope for this test suite (no cluster access); this fake implements just
enough of ``CoreV1Api``/``NetworkingV1Api`` behavior -- list/read/create/
delete-with-409-on-conflict -- to exercise the dual-writer rollback,
owner-scoping, and readiness-detection logic against realistic inputs.
"""

from __future__ import annotations

import copy
import datetime
from dataclasses import dataclass, field
from typing import Any

from kubernetes.client.exceptions import ApiException


@dataclass
class FakeNotFound(Exception):
    status: int = 404


def _matches_label_selector(labels: dict[str, str], selector: str | None) -> bool:
    if not selector:
        return True
    for raw_clause in selector.split(","):
        clause = raw_clause.strip()
        if "=" not in clause:
            # Bare-key selector: "label key present", not "value == ''".
            if clause not in labels:
                return False
            continue
        key, _, value = clause.partition("=")
        if labels.get(key.strip()) != value.strip():
            return False
    return True


def _matches_field_selector(obj: Any, selector: str | None) -> bool:
    if not selector:
        return True
    for clause in selector.split(","):
        field_path, _, value = clause.partition("=")
        field_path = field_path.strip()
        if field_path == "metadata.name":
            if obj.metadata.name != value.strip():
                return False
        else:  # pragma: no cover - only metadata.name is used today
            msg = f"unsupported field selector: {field_path}"
            raise NotImplementedError(msg)
    return True


class _ItemsResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items


@dataclass
class FakeCoreV1Api:
    """In-memory stand-in for ``kubernetes.client.CoreV1Api``."""

    pods: dict[tuple[str, str], Any] = field(default_factory=dict)
    services: dict[tuple[str, str], Any] = field(default_factory=dict)
    secrets: dict[tuple[str, str], Any] = field(default_factory=dict)
    nodes: dict[str, Any] = field(default_factory=dict)
    events: dict[str, list[Any]] = field(default_factory=dict)
    pod_logs: dict[tuple[str, str], str] = field(default_factory=dict)
    # Names that should 409 on the *next* create call (dual-writer race sim).
    conflict_on_create: set[str] = field(default_factory=set)
    created_order: list[str] = field(default_factory=list)
    deleted_order: list[str] = field(default_factory=list)

    def create_namespaced_pod(self, namespace: str, body: Any) -> Any:
        name = body["metadata"]["name"]
        if name in self.conflict_on_create:
            raise ApiException(status=409, reason="AlreadyExists")
        self.pods[(namespace, name)] = _pod_from_manifest(body)
        self.created_order.append(f"pod/{name}")
        return self.pods[(namespace, name)]

    def create_namespaced_service(self, namespace: str, body: Any) -> Any:
        name = body["metadata"]["name"]
        if name in self.conflict_on_create:
            raise ApiException(status=409, reason="AlreadyExists")
        self.services[(namespace, name)] = copy.deepcopy(body)
        self.created_order.append(f"service/{name}")
        return self.services[(namespace, name)]

    def create_namespaced_secret(self, namespace: str, body: Any) -> Any:
        name = body["metadata"]["name"]
        if name in self.conflict_on_create:
            raise ApiException(status=409, reason="AlreadyExists")
        # The real client deserializes GET responses into typed, attribute-
        # access model objects (V1Secret) even though create_* accepts a
        # plain dict body -- mirror that asymmetry here.
        self.secrets[(namespace, name)] = _Obj(
            metadata=_Obj(
                name=name,
                namespace=namespace,
                labels=dict(body["metadata"].get("labels") or {}),
            ),
            data=dict(body.get("data") or {}),
            type=body.get("type"),
        )
        self.created_order.append(f"secret/{name}")
        return self.secrets[(namespace, name)]

    def read_namespaced_secret(self, name: str, namespace: str) -> Any:
        try:
            return self.secrets[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="NotFound") from None

    def read_namespaced_pod(self, name: str, namespace: str) -> Any:
        try:
            return self.pods[(namespace, name)]
        except KeyError:
            raise ApiException(status=404, reason="NotFound") from None

    def read_namespaced_pod_log(self, name: str, namespace: str) -> str:
        return self.pod_logs.get((namespace, name), "")

    def list_namespaced_pod(
        self,
        namespace: str,
        field_selector: str | None = None,
        label_selector: str | None = None,
    ) -> _ItemsResult:
        items = [
            pod
            for (ns, _name), pod in self.pods.items()
            if ns == namespace
            and _matches_field_selector(pod, field_selector)
            and _matches_label_selector(pod.metadata.labels or {}, label_selector)
        ]
        return _ItemsResult(items)

    def list_namespaced_event(
        self,
        namespace: str,  # noqa: ARG002 -- kept for interface parity with kubernetes.client.CoreV1Api
        field_selector: str | None = None,
    ) -> _ItemsResult:
        uid = ""
        if field_selector and field_selector.startswith("involvedObject.uid="):
            uid = field_selector.split("=", 1)[1]
        return _ItemsResult(self.events.get(uid, []))

    def list_node(self, label_selector: str | None = None) -> _ItemsResult:
        items = [
            node
            for node in self.nodes.values()
            if _matches_label_selector(node.metadata.labels or {}, label_selector)
        ]
        return _ItemsResult(items)

    def read_node(self, name: str) -> Any:
        return self.nodes[name]

    def list_pod_for_all_namespaces(
        self, field_selector: str | None = None
    ) -> _ItemsResult:
        node_name = None
        if field_selector:
            for clause in field_selector.split(","):
                key, _, value = clause.partition("=")
                if key == "spec.nodeName":
                    node_name = value
        items = [
            pod
            for pod in self.pods.values()
            if node_name is None or pod.spec.node_name == node_name
        ]
        return _ItemsResult(items)

    def delete_namespaced_pod(self, name: str, namespace: str) -> None:
        self.pods.pop((namespace, name), None)
        self.deleted_order.append(f"pod/{name}")

    def delete_namespaced_service(self, name: str, namespace: str) -> None:
        self.services.pop((namespace, name), None)
        self.deleted_order.append(f"service/{name}")

    def delete_namespaced_secret(self, name: str, namespace: str) -> None:
        self.secrets.pop((namespace, name), None)
        self.deleted_order.append(f"secret/{name}")


@dataclass
class FakeNetworkingV1Api:
    """In-memory stand-in for ``kubernetes.client.NetworkingV1Api``."""

    core: FakeCoreV1Api
    ingresses: dict[tuple[str, str], Any] = field(default_factory=dict)

    def create_namespaced_ingress(self, namespace: str, body: Any) -> Any:
        name = body["metadata"]["name"]
        if name in self.core.conflict_on_create:
            raise ApiException(status=409, reason="AlreadyExists")
        self.ingresses[(namespace, name)] = copy.deepcopy(body)
        self.core.created_order.append(f"ingress/{name}")
        return self.ingresses[(namespace, name)]

    def delete_namespaced_ingress(self, name: str, namespace: str) -> None:
        self.ingresses.pop((namespace, name), None)
        self.core.deleted_order.append(f"ingress/{name}")


class _Obj:
    """Attribute-access dict, mimicking generated k8s client model objects."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _pod_from_manifest(manifest: dict[str, Any]) -> Any:
    """Build a pod object with the attribute shape jupyterlab_mcp.k8s.notebooks expects."""
    md = manifest["metadata"]
    spec = manifest["spec"]
    container = spec["containers"][0]
    return _Obj(
        metadata=_Obj(
            name=md["name"],
            namespace=md["namespace"],
            labels=dict(md.get("labels") or {}),
            creation_timestamp=datetime.datetime.now(tz=datetime.timezone.utc),
            uid=f"uid-{md['name']}",
            deletion_timestamp=None,
        ),
        spec=_Obj(
            containers=[
                _Obj(
                    image=container["image"],
                    resources=_Obj(
                        requests=container["resources"]["requests"],
                        limits=container["resources"]["limits"],
                    ),
                )
            ],
            node_name=None,
            node_selector=spec.get("nodeSelector"),
        ),
        status=_Obj(
            phase="Pending",
            conditions=[],
        ),
    )


def make_node(
    name: str, *, product: str, memory: int, count: int, gpu_capacity: int
) -> Any:
    return _Obj(
        metadata=_Obj(
            name=name,
            labels={
                "gpu": "true",
                "nvidia.com/gpu.product": product,
                "nvidia.com/gpu.memory": str(memory),
                "nvidia.com/gpu.count": str(count),
            },
        ),
        status=_Obj(
            capacity={
                "nvidia.com/gpu": str(gpu_capacity),
                "memory": "512Gi",
                "cpu": "64",
            }
        ),
    )


def make_gpu_pod(*, name: str, node_name: str, gpu_request: int) -> Any:
    return _Obj(
        metadata=_Obj(name=name, namespace="other", uid=f"uid-{name}"),
        spec=_Obj(
            node_name=node_name,
            containers=[
                _Obj(
                    resources=_Obj(
                        requests={
                            "nvidia.com/gpu": str(gpu_request),
                            "memory": "4Gi",
                            "cpu": "2",
                        }
                    )
                )
            ],
        ),
        status=_Obj(phase="Running"),
    )
