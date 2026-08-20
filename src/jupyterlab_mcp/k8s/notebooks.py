"""Create/get/list/delete notebook logic, ported from af-portal's jupyterlab.py.

Ports ``deploy_notebook``/``get_notebook``/``get_notebooks``/``remove_notebook``
from af-portal's ``portal/jupyterlab.py``, ``kubernetes==...`` client. Two
behaviors are intentionally NOT ported, per af-mcp-platform issue #189:

- No patch-on-409 fallback. The portal silently adopts a same-named
  service/secret/ingress on a 409, which is fine for the portal's own
  re-deploys but wrong for a second, independent writer: here a 409 on ANY
  of the four creates is a hard error, and whatever was already created in
  that call is rolled back (``_rollback``).
- Owner-scoping is enforced everywhere a pod is looked up by name
  (``get_notebook``, ``delete_notebook``): a caller who is not the pod's
  ``owner`` label gets the same error as a nonexistent pod
  (``NotFoundOrNotYoursError``), never a distinguishable 403.
"""

from __future__ import annotations

import base64
import datetime
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kubernetes.client.exceptions import ApiException

from jupyterlab_mcp.k8s.errors import (
    NameConflictError,
    NotFoundOrNotYoursError,
    QuotaExceededError,
)
from jupyterlab_mcp.k8s.guardrails import compute_limits, validate_create_request
from jupyterlab_mcp.k8s.names import (
    generate_notebook_name,
    notebook_name_available,
    sanitize_k8s_pod_name,
)
from jupyterlab_mcp.k8s.templates import render_manifests

if TYPE_CHECKING:
    from jupyterlab_mcp.config import Settings

_START_SCRIPT = "/usr/local/bin/SetupPrivateJupyterLab.sh"
_CONDITION_ORDER = {
    "PodScheduled": 0,
    "Initialized": 1,
    "PodReadyToStartContainers": 2,
    "ContainersReady": 3,
    "Ready": 4,
}


@dataclass
class K8sClients:
    """The two API clients notebook management needs.

    Production code builds this from an in-cluster ServiceAccount
    (``config.load_incluster_config()``); tests inject fakes (see
    ``tests/k8s/fakes.py``).
    """

    core_v1: Any
    networking_v1: Any


def _count_owned_pods(clients: K8sClients, namespace: str, owner: str) -> int:
    pods = clients.core_v1.list_namespaced_pod(
        namespace, label_selector=f"k8s-app=jupyterlab,owner={owner}"
    )
    return len(pods.items)


def _read_pod_or_none(core_v1: Any, namespace: str, name: str) -> Any | None:
    try:
        return core_v1.read_namespaced_pod(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _rollback(
    clients: K8sClients, namespace: str, name: str, created: list[str]
) -> None:
    """Delete, in reverse order, whatever objects were already created.

    Best-effort: if an object is somehow already gone, that's fine -- the
    goal is "no orphaned objects", not "every delete must succeed".
    """
    for kind in reversed(created):
        try:
            if kind == "pod":
                clients.core_v1.delete_namespaced_pod(name, namespace)
            elif kind == "service":
                clients.core_v1.delete_namespaced_service(name, namespace)
            elif kind == "secret":
                clients.core_v1.delete_namespaced_secret(name, namespace)
            elif kind == "ingress":
                clients.networking_v1.delete_namespaced_ingress(name, namespace)
        except ApiException:  # noqa: PERF203 -- best-effort rollback; a delete failing (e.g. already gone) must not mask the original 409
            pass


def create_notebook(
    clients: K8sClients,
    *,
    settings: Settings,
    owner: str,
    owner_uid: int,
    name: str | None,
    image: str,
    cpu_cores: int,
    memory_gb: int,
    gpus: int,
    gpu_product: str | None,
    duration_hours: int,
) -> dict[str, Any]:
    """Create the pod+service+secret+ingress quadruple for one notebook.

    ``owner``/``owner_uid`` must come from verified broker JWT claims, never
    from caller-supplied arguments (enforced by the tool layer). Returns a
    dict describing the created notebook -- deliberately never the token or
    a tokenized URL (see ``get_jupyter_server(include_url=True)`` for that).

    Raises:
        GuardrailError / ImageNotAllowedError: a server-side guardrail failed.
        QuotaExceededError: the configured (opt-in) per-owner quota was hit.
        NameConflictError: the name was taken, including a 409 lost to a
            concurrent create -- in which case anything already created in
            this call is rolled back first.
    """
    validate_create_request(
        image=image,
        cpu_cores=cpu_cores,
        memory_gb=memory_gb,
        gpus=gpus,
        duration_hours=duration_hours,
        settings=settings,
    )

    if settings.max_servers_per_user is not None:
        owned = _count_owned_pods(clients, settings.notebook_namespace, owner)
        if owned >= settings.max_servers_per_user:
            msg = (
                f"owner {owner!r} already has {owned} notebook(s), at the "
                f"configured max_servers_per_user={settings.max_servers_per_user}"
            )
            raise QuotaExceededError(msg)

    if name:
        notebook_id = sanitize_k8s_pod_name(name)
    else:
        generated = generate_notebook_name(
            clients.core_v1, settings.notebook_namespace, owner
        )
        if generated is None:
            msg = f"no available default notebook name slot for owner {owner!r}"
            raise NameConflictError(msg)
        notebook_id = generated

    if not notebook_name_available(
        clients.core_v1, settings.notebook_namespace, notebook_id
    ):
        msg = f"notebook name {notebook_id!r} is already taken"
        raise NameConflictError(msg)

    cpu_limit, memory_limit_gb = compute_limits(
        cpu_cores=cpu_cores, memory_gb=memory_gb
    )
    token = base64.b64encode(os.urandom(32)).decode()
    manifests = render_manifests(
        notebook_id=notebook_id,
        notebook_name=name or notebook_id,
        namespace=settings.notebook_namespace,
        domain_name=settings.domain,
        owner=owner,
        owner_uid=owner_uid,
        image=image,
        token=token,
        start_script=_START_SCRIPT,
        cpu_request=cpu_cores,
        cpu_limit=cpu_limit,
        memory_request=f"{memory_gb}Gi",
        memory_limit=f"{memory_limit_gb}Gi",
        gpu_request=gpus,
        gpu_limit=gpus,
        gpu_product=gpu_product,
        hours_remaining=duration_hours,
    )

    created: list[str] = []
    try:
        clients.core_v1.create_namespaced_pod(
            namespace=settings.notebook_namespace, body=manifests["pod"]
        )
        created.append("pod")
        clients.core_v1.create_namespaced_service(
            namespace=settings.notebook_namespace, body=manifests["service"]
        )
        created.append("service")
        clients.core_v1.create_namespaced_secret(
            namespace=settings.notebook_namespace, body=manifests["secret"]
        )
        created.append("secret")
        clients.networking_v1.create_namespaced_ingress(
            namespace=settings.notebook_namespace, body=manifests["ingress"]
        )
        created.append("ingress")
    except ApiException as exc:
        _rollback(clients, settings.notebook_namespace, notebook_id, created)
        if exc.status == 409:
            msg = (
                f"notebook name {notebook_id!r} was taken by a concurrent "
                "create (dual-writer race) -- rolled back"
            )
            raise NameConflictError(msg) from exc
        raise

    return {
        "id": notebook_id,
        "name": name or notebook_id,
        "namespace": settings.notebook_namespace,
        "owner": owner,
        "image": image,
        "cpu_request": cpu_cores,
        "cpu_limit": cpu_limit,
        "memory_request_gb": memory_gb,
        "memory_limit_gb": memory_limit_gb,
        "gpu_request": gpus,
        "gpu_product": gpu_product,
        "duration_hours": duration_hours,
    }


def get_expiration_date(pod: Any) -> datetime.datetime | None:
    """Return the pod's expiration date, derived from its time2delete label."""
    pattern = re.compile(r"ttl-\d+")
    label = pod.metadata.labels.get("time2delete", "")
    if pattern.match(label):
        hours = int(label.split("-")[1])
        expiration: datetime.datetime = (
            pod.metadata.creation_timestamp + datetime.timedelta(hours=hours)
        )
        return expiration
    return None


def _build_notebook_info(
    clients: K8sClients,
    pod: Any,
    *,
    settings: Settings,
    include_log: bool = False,
    include_url: bool = False,
) -> dict[str, Any]:
    """Port of the portal's ``get_notebook`` dict-assembly logic."""
    api = clients.core_v1
    notebook: dict[str, Any] = {
        "id": pod.metadata.name,
        "name": pod.metadata.labels.get("notebook-name"),
        "namespace": settings.notebook_namespace,
        "owner": pod.metadata.labels.get("owner"),
        "image": pod.spec.containers[0].image,
        "node": pod.spec.node_name,
        "node_selector": pod.spec.node_selector,
        "pod_status": pod.status.phase,
        "creation_date": pod.metadata.creation_timestamp.isoformat(),
        "requests": pod.spec.containers[0].resources.requests,
        "limits": pod.spec.containers[0].resources.limits,
    }

    expiration_date = get_expiration_date(pod)
    if expiration_date is not None:
        time_remaining = expiration_date - datetime.datetime.now(
            tz=datetime.timezone.utc
        )
        notebook["expiration_date"] = expiration_date.isoformat()
        notebook["hours_remaining"] = int(time_remaining.total_seconds() / 3600)

    conditions = [
        {
            "type": c.type,
            "status": c.status,
            "timestamp": c.last_transition_time.isoformat(),
        }
        for c in pod.status.conditions
    ]
    conditions.sort(
        key=lambda cond: _CONDITION_ORDER.get(cond["type"], len(_CONDITION_ORDER))
    )
    notebook["conditions"] = conditions

    events = api.list_namespaced_event(
        namespace=settings.notebook_namespace,
        field_selector=f"involvedObject.uid={pod.metadata.uid}",
    ).items
    notebook["events"] = [
        {
            "message": e.message,
            "timestamp": e.last_timestamp.isoformat() if e.last_timestamp else None,
        }
        for e in events
    ]

    if pod.spec.node_name:
        node = api.read_node(pod.spec.node_name)
        if node.metadata.labels.get("gpu") == "true":
            notebook["gpu"] = {
                "product": node.metadata.labels["nvidia.com/gpu.product"],
                "memory": node.metadata.labels["nvidia.com/gpu.memory"] + "Mi",
            }

    log: str | None = None
    if pod.metadata.deletion_timestamp is None:
        ready = any(
            c.type == "Ready" and c.status == "True" for c in pod.status.conditions
        )
        if ready:
            log = api.read_namespaced_pod_log(
                pod.metadata.name, namespace=settings.notebook_namespace
            )
            notebook["status"] = (
                "Ready"
                if re.search("Jupyter.*is running at", log)
                else "Starting notebook..."
            )
        else:
            notebook["status"] = "Pending"
    else:
        notebook["status"] = "Removing notebook..."

    if include_log and log is not None:
        notebook["log"] = log

    if include_url and pod.metadata.deletion_timestamp is None:
        secret = api.read_namespaced_secret(
            pod.metadata.name, settings.notebook_namespace
        )
        token = secret.data["token"]
        query = urllib.parse.urlencode({"token": token})
        notebook["url"] = f"https://{pod.metadata.name}.{settings.domain}?{query}"

    return notebook


def get_notebook(
    clients: K8sClients,
    *,
    settings: Settings,
    name: str,
    owner: str,
    include_log: bool = False,
    include_url: bool = False,
) -> dict[str, Any]:
    """Return rich status for one notebook, refusing access to non-owners.

    Raises:
        NotFoundOrNotYoursError: the pod does not exist, or exists but is
            not owned by *owner* -- deliberately indistinguishable, so a
            caller cannot enumerate other users' server names.
    """
    pod = _read_pod_or_none(clients.core_v1, settings.notebook_namespace, name.lower())
    if pod is None or pod.metadata.labels.get("owner") != owner:
        msg = f"no notebook named {name!r} (or it is not yours)"
        raise NotFoundOrNotYoursError(msg)
    return _build_notebook_info(
        clients,
        pod,
        settings=settings,
        include_log=include_log,
        include_url=include_url,
    )


def list_notebooks(
    clients: K8sClients, *, settings: Settings, owner: str
) -> list[dict[str, Any]]:
    """List the caller's own notebooks. Other users' pods are never fetched."""
    pods = clients.core_v1.list_namespaced_pod(
        settings.notebook_namespace, label_selector=f"k8s-app=jupyterlab,owner={owner}"
    ).items
    results: list[dict[str, Any]] = []
    for pod in pods:
        try:
            results.append(
                _build_notebook_info(
                    clients,
                    pod,
                    settings=settings,
                    include_log=False,
                    include_url=False,
                )
            )
        except Exception:  # noqa: BLE001, PERF203, S112 -- one bad pod (portal parity) must not fail the whole listing
            continue
    return results


def delete_notebook(
    clients: K8sClients, *, settings: Settings, name: str, owner: str
) -> None:
    """Delete the pod+service+secret+ingress quadruple, refusing non-owners.

    Raises:
        NotFoundOrNotYoursError: same not-found-or-not-yours refusal as
            ``get_notebook`` -- checked BEFORE anything is deleted.
    """
    pod = _read_pod_or_none(clients.core_v1, settings.notebook_namespace, name.lower())
    if pod is None or pod.metadata.labels.get("owner") != owner:
        msg = f"no notebook named {name!r} (or it is not yours)"
        raise NotFoundOrNotYoursError(msg)

    notebook_id = pod.metadata.name
    namespace = settings.notebook_namespace
    clients.core_v1.delete_namespaced_pod(notebook_id, namespace)
    clients.core_v1.delete_namespaced_service(notebook_id, namespace)
    clients.core_v1.delete_namespaced_secret(notebook_id, namespace)
    clients.networking_v1.delete_namespaced_ingress(notebook_id, namespace)
