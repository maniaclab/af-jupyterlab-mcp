"""Tests for create/get/list/delete notebook logic.

Covers the three properties called out explicitly in af-mcp-platform issue
#189 as needing tests: owner-scoping rejects cross-user access, a 409 on any
of the four creates rolls back whatever was already created (hard error, no
portal-style patch-on-409 adoption), and guardrail ranges are enforced.
"""

from __future__ import annotations

import urllib.parse

import pytest
from kubernetes.client.exceptions import ApiException

from af_jupyterlab_mcp.config import Settings
from af_jupyterlab_mcp.k8s.errors import (
    GuardrailError,
    NameConflictError,
    NotFoundOrNotYoursError,
    QuotaExceededError,
)
from af_jupyterlab_mcp.k8s.notebooks import (
    K8sClients,
    create_notebook,
    delete_notebook,
    get_notebook,
    list_notebooks,
)

from .fakes import FakeCoreV1Api, FakeNetworkingV1Api

_IMAGE = "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        notebook_namespace="jupyterlab",
        domain="notebooks.af.uchicago.edu",
        cpu_images=(_IMAGE,),
        gpu_images=(),
    )


@pytest.fixture
def clients() -> K8sClients:
    core = FakeCoreV1Api()
    networking = FakeNetworkingV1Api(core=core)
    return K8sClients(core_v1=core, networking_v1=networking)


class TestCreateNotebookHappyPath:
    def test_creates_four_objects(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        info = create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=12345,
            name=None,
            image=_IMAGE,
            cpu_cores=2,
            memory_gb=8,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        assert info["id"] == "kratsg-notebook-1"
        assert info["owner"] == "kratsg"
        assert clients.core_v1.created_order == [
            "pod/kratsg-notebook-1",
            "service/kratsg-notebook-1",
            "secret/kratsg-notebook-1",
            "ingress/kratsg-notebook-1",
        ]

    def test_does_not_return_token_or_url(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        info = create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=12345,
            name=None,
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        assert "token" not in info
        assert "url" not in info

    def test_secret_token_matches_pod_env_token(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        """af-portal reads the token back via read_namespaced_secret, never
        the pod env var -- the two must agree or af-portal's browser URL
        for this notebook is built from a token that doesn't work."""
        create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=12345,
            name=None,
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        pod = clients.core_v1.pods[("jupyterlab", "kratsg-notebook-1")]
        env_map = {e.name: e.value for e in pod.spec.containers[0].env}
        secret = clients.core_v1.secrets[("jupyterlab", "kratsg-notebook-1")]
        assert secret["data"]["token"] == env_map["JUPYTER_TOKEN"]

    def test_explicit_name_is_sanitized(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        info = create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=12345,
            name="My Notebook!!",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        assert info["id"] == "my-notebook"

    def test_owner_and_owner_uid_come_from_arguments_not_manifest_injection(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=999,
            name=None,
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        pod = clients.core_v1.pods[("jupyterlab", "kratsg-notebook-1")]
        assert pod.metadata.labels["owner"] == "kratsg"


class TestCreateNotebookNameConflict:
    def test_explicit_taken_name_raises_without_creating_anything(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=1,
            name="dup",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        before = len(clients.core_v1.created_order)
        with pytest.raises(NameConflictError):
            create_notebook(
                clients,
                settings=settings,
                owner="otheruser",
                owner_uid=2,
                name="dup",
                image=_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )
        assert len(clients.core_v1.created_order) == before


class TestCreateNotebookRollbackOn409:
    """A 409 on ANY of the four creates is a hard error with rollback of
    whatever was already created -- never the portal's patch-on-409 adoption."""

    def test_409_on_service_rolls_back_pod(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        clients.core_v1.conflict_on_create.add("kratsg-notebook-1")

        # Let the pod itself succeed by racing the conflict in after pod create:
        # simulate by pre-creating a same-named object only for the service
        # step. Simpler: monkeypatch create_namespaced_service to always 409.
        def _boom(*_a: object, **_k: object) -> None:
            raise ApiException(status=409, reason="AlreadyExists")

        clients.core_v1.conflict_on_create.clear()
        clients.core_v1.create_namespaced_service = _boom

        with pytest.raises(NameConflictError):
            create_notebook(
                clients,
                settings=settings,
                owner="kratsg",
                owner_uid=1,
                name="racer",
                image=_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )
        assert ("jupyterlab", "racer") not in clients.core_v1.pods
        assert clients.core_v1.deleted_order == ["pod/racer"]

    def test_409_on_secret_rolls_back_pod_and_service(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            raise ApiException(status=409, reason="AlreadyExists")

        clients.core_v1.create_namespaced_secret = _boom

        with pytest.raises(NameConflictError):
            create_notebook(
                clients,
                settings=settings,
                owner="kratsg",
                owner_uid=1,
                name="racer2",
                image=_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )
        assert ("jupyterlab", "racer2") not in clients.core_v1.pods
        assert ("jupyterlab", "racer2") not in clients.core_v1.services
        assert clients.core_v1.deleted_order == ["service/racer2", "pod/racer2"]

    def test_409_on_ingress_rolls_back_pod_service_and_secret(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            raise ApiException(status=409, reason="AlreadyExists")

        clients.networking_v1.create_namespaced_ingress = _boom

        with pytest.raises(NameConflictError):
            create_notebook(
                clients,
                settings=settings,
                owner="kratsg",
                owner_uid=1,
                name="racer3",
                image=_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )
        assert ("jupyterlab", "racer3") not in clients.core_v1.pods
        assert ("jupyterlab", "racer3") not in clients.core_v1.services
        assert ("jupyterlab", "racer3") not in clients.core_v1.secrets
        assert clients.core_v1.deleted_order == [
            "secret/racer3",
            "service/racer3",
            "pod/racer3",
        ]

    def test_non_409_error_propagates_without_partial_rollback_confusion(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            raise ApiException(status=500, reason="InternalError")

        clients.core_v1.create_namespaced_service = _boom

        with pytest.raises(ApiException):
            create_notebook(
                clients,
                settings=settings,
                owner="kratsg",
                owner_uid=1,
                name="racer4",
                image=_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )
        # Still rolls back what was already created, regardless of status code.
        assert ("jupyterlab", "racer4") not in clients.core_v1.pods


class TestCreateNotebookGuardrails:
    def test_out_of_range_cpu_raises_and_creates_nothing(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        with pytest.raises(GuardrailError):
            create_notebook(
                clients,
                settings=settings,
                owner="kratsg",
                owner_uid=1,
                name=None,
                image=_IMAGE,
                cpu_cores=100,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )
        assert clients.core_v1.created_order == []

    def test_disallowed_image_raises_and_creates_nothing(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        with pytest.raises(GuardrailError):
            create_notebook(
                clients,
                settings=settings,
                owner="kratsg",
                owner_uid=1,
                name=None,
                image="not-allowed:latest",
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )
        assert clients.core_v1.created_order == []


class TestCreateNotebookQuota:
    def test_quota_exceeded_when_configured(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        quota_settings = Settings(
            notebook_namespace=settings.notebook_namespace,
            domain=settings.domain,
            cpu_images=settings.cpu_images,
            gpu_images=settings.gpu_images,
            max_servers_per_user=1,
        )
        create_notebook(
            clients,
            settings=quota_settings,
            owner="kratsg",
            owner_uid=1,
            name="first",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        with pytest.raises(QuotaExceededError):
            create_notebook(
                clients,
                settings=quota_settings,
                owner="kratsg",
                owner_uid=1,
                name="second",
                image=_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )

    def test_quota_unset_by_default_allows_unlimited(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        for i in range(3):
            create_notebook(
                clients,
                settings=settings,
                owner="kratsg",
                owner_uid=1,
                name=f"nb{i}",
                image=_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                gpu_product=None,
                duration_hours=8,
            )
        assert len(clients.core_v1.pods) == 3

    def test_quota_is_scoped_per_owner(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        quota_settings = Settings(
            notebook_namespace=settings.notebook_namespace,
            domain=settings.domain,
            cpu_images=settings.cpu_images,
            gpu_images=settings.gpu_images,
            max_servers_per_user=1,
        )
        create_notebook(
            clients,
            settings=quota_settings,
            owner="alice",
            owner_uid=1,
            name="alice-nb",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        # bob is unaffected by alice's quota usage
        create_notebook(
            clients,
            settings=quota_settings,
            owner="bob",
            owner_uid=2,
            name="bob-nb",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )


class TestGetNotebookOwnerScoping:
    def _create(
        self, clients: K8sClients, settings: Settings, owner: str, name: str
    ) -> None:
        create_notebook(
            clients,
            settings=settings,
            owner=owner,
            owner_uid=1,
            name=name,
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )

    def test_owner_can_read_their_own_notebook(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        self._create(clients, settings, "kratsg", "mine")
        info = get_notebook(clients, settings=settings, name="mine", owner="kratsg")
        assert info["id"] == "mine"
        assert info["owner"] == "kratsg"

    def test_other_user_cannot_read_notebook(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        self._create(clients, settings, "kratsg", "mine")
        with pytest.raises(NotFoundOrNotYoursError):
            get_notebook(clients, settings=settings, name="mine", owner="eve")

    def test_nonexistent_notebook_raises_same_error_as_not_yours(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        with pytest.raises(NotFoundOrNotYoursError):
            get_notebook(clients, settings=settings, name="ghost", owner="kratsg")

    def test_include_url_returns_tokenized_url_only_for_owner(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        self._create(clients, settings, "kratsg", "mine")
        info = get_notebook(
            clients,
            settings=settings,
            name="mine",
            owner="kratsg",
            include_url=True,
        )
        assert info["url"].startswith("https://mine.notebooks.af.uchicago.edu?token=")
        # Token is read from the pod's JUPYTER_TOKEN env var (no Secret).
        pod = clients.core_v1.pods[("jupyterlab", "mine")]
        env_map = {e.name: e.value for e in pod.spec.containers[0].env}
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(info["url"]).query)
        assert qs["token"] == [env_map["JUPYTER_TOKEN"]]

    def test_include_url_false_by_default(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        self._create(clients, settings, "kratsg", "mine")
        info = get_notebook(clients, settings=settings, name="mine", owner="kratsg")
        assert "url" not in info


class TestListNotebooksOwnerScoping:
    def test_only_returns_callers_own_notebooks(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=1,
            name="mine1",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        create_notebook(
            clients,
            settings=settings,
            owner="eve",
            owner_uid=2,
            name="eves",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        results = list_notebooks(clients, settings=settings, owner="kratsg")
        assert [r["id"] for r in results] == ["mine1"]

    def test_never_includes_tokenized_url(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=1,
            name="mine1",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        results = list_notebooks(clients, settings=settings, owner="kratsg")
        assert all("url" not in r for r in results)


class TestDeleteNotebookOwnerScoping:
    def test_owner_can_delete_their_own_notebook(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=1,
            name="mine",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        delete_notebook(clients, settings=settings, name="mine", owner="kratsg")
        assert ("jupyterlab", "mine") not in clients.core_v1.pods
        assert ("jupyterlab", "mine") not in clients.core_v1.services
        assert ("jupyterlab", "mine") not in clients.core_v1.secrets
        assert ("jupyterlab", "mine") not in clients.networking_v1.ingresses

    def test_other_user_cannot_delete_notebook(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        create_notebook(
            clients,
            settings=settings,
            owner="kratsg",
            owner_uid=1,
            name="mine",
            image=_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            gpu_product=None,
            duration_hours=8,
        )
        with pytest.raises(NotFoundOrNotYoursError):
            delete_notebook(clients, settings=settings, name="mine", owner="eve")
        # Nothing was deleted.
        assert ("jupyterlab", "mine") in clients.core_v1.pods

    def test_delete_nonexistent_raises(
        self, clients: K8sClients, settings: Settings
    ) -> None:
        with pytest.raises(NotFoundOrNotYoursError):
            delete_notebook(clients, settings=settings, name="ghost", owner="kratsg")
