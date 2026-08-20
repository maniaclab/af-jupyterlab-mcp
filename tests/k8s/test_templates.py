"""Tests for the ported Jinja templates and the two deliberate divergences.

Divergences from af-portal's templates (issue #189):
  1. add a `created-by: af-jupyterlab-mcp` label
  2. omit `globus-id` (broker JWTs carry no Globus ID -- see issue #189
     open question 2, flagged, not silently resolved, in the final report)
"""

from __future__ import annotations

from af_jupyterlab_mcp.k8s.templates import render_manifests

_SETTINGS = {
    "notebook_id": "kratsg-notebook-1",
    "notebook_name": "kratsg-notebook-1",
    "namespace": "jupyterlab",
    "domain_name": "notebooks.af.uchicago.edu",
    "owner": "kratsg",
    "owner_uid": 12345,
    "image": "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest",
    "token": "dG9rZW4=",
    "start_script": "/usr/local/bin/SetupPrivateJupyterLab.sh",
    "cpu_request": 2,
    "cpu_limit": 4,
    "memory_request": "8Gi",
    "memory_limit": "16Gi",
    "gpu_request": 0,
    "gpu_limit": 0,
    "gpu_product": None,
    "hours_remaining": 8,
}


class TestRenderManifests:
    def test_renders_all_four_kinds(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        assert set(manifests) == {"pod", "service", "secret", "ingress"}
        assert manifests["pod"]["kind"] == "Pod"
        assert manifests["service"]["kind"] == "Service"
        assert manifests["secret"]["kind"] == "Secret"
        assert manifests["ingress"]["kind"] == "Ingress"

    def test_pod_name_and_namespace(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        pod = manifests["pod"]
        assert pod["metadata"]["name"] == "kratsg-notebook-1"
        assert pod["metadata"]["namespace"] == "jupyterlab"

    def test_created_by_label_added_to_all_four(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        for kind, manifest in manifests.items():
            assert (
                manifest["metadata"]["labels"]["created-by"] == "af-jupyterlab-mcp"
            ), f"{kind} is missing the created-by divergence label"

    def test_globus_id_label_omitted_from_pod(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        assert "globus-id" not in manifests["pod"]["metadata"]["labels"]

    def test_owner_label_on_pod_and_secret(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        assert manifests["pod"]["metadata"]["labels"]["owner"] == "kratsg"
        assert manifests["secret"]["metadata"]["labels"]["owner"] == "kratsg"

    def test_time2delete_label_matches_duration(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        assert manifests["pod"]["metadata"]["labels"]["time2delete"] == "ttl-8"

    def test_gpu_node_selector_rendered_when_gpu_requested(self) -> None:
        settings = {
            **_SETTINGS,
            "gpu_request": 1,
            "gpu_limit": 1,
            "gpu_product": "A100",
        }
        manifests = render_manifests(**settings)
        assert (
            manifests["pod"]["spec"]["nodeSelector"]["nvidia.com/gpu.product"] == "A100"
        )

    def test_cpu_only_pod_has_no_gpu_node_selector(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        assert "nodeSelector" not in manifests["pod"]["spec"]

    def test_ingress_host_uses_notebook_id_and_domain(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        assert (
            manifests["ingress"]["spec"]["rules"][0]["host"]
            == "kratsg-notebook-1.notebooks.af.uchicago.edu"
        )

    def test_secret_token_data(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        assert manifests["secret"]["data"]["token"] == "dG9rZW4="

    def test_jupyter_token_env_on_container(self) -> None:
        manifests = render_manifests(**_SETTINGS)
        env = manifests["pod"]["spec"]["containers"][0]["env"]
        token_env = next(e for e in env if e["name"] == "JUPYTER_TOKEN")
        assert token_env["value"] == "dG9rZW4="
