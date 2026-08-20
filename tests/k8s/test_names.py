"""Tests for pod-name sanitization, availability checks, and default naming."""

from __future__ import annotations

from af_jupyterlab_mcp.k8s.names import (
    generate_notebook_name,
    notebook_name_available,
    sanitize_k8s_pod_name,
)

from .fakes import FakeCoreV1Api, _pod_from_manifest


class TestSanitizeK8sPodName:
    def test_lowercases(self) -> None:
        assert sanitize_k8s_pod_name("MyNotebook") == "mynotebook"

    def test_replaces_invalid_characters(self) -> None:
        assert sanitize_k8s_pod_name("my_notebook!!") == "my-notebook"

    def test_strips_leading_trailing_hyphens(self) -> None:
        assert sanitize_k8s_pod_name("--my-notebook--") == "my-notebook"

    def test_truncates_to_max_length(self) -> None:
        long_name = "a" * 100
        result = sanitize_k8s_pod_name(long_name)
        assert len(result) == 63

    def test_truncation_does_not_leave_trailing_hyphen(self) -> None:
        name = "a" * 62 + "-" + "b"
        result = sanitize_k8s_pod_name(name, max_length=63)
        assert not result.endswith("-")

    def test_empty_after_sanitization_defaults(self) -> None:
        assert sanitize_k8s_pod_name("!!!") == "default-pod"


class TestNotebookNameAvailable:
    def test_available_when_no_pod_exists(self) -> None:
        api = FakeCoreV1Api()
        assert notebook_name_available(api, "jupyterlab", "kratsg-notebook-1") is True

    def test_unavailable_when_pod_exists(self) -> None:
        api = FakeCoreV1Api()
        api.pods[("jupyterlab", "kratsg-notebook-1")] = _pod_from_manifest(
            {
                "metadata": {"name": "kratsg-notebook-1", "namespace": "jupyterlab"},
                "spec": {
                    "containers": [
                        {
                            "image": "x",
                            "resources": {"requests": {}, "limits": {}},
                        }
                    ]
                },
            }
        )
        assert notebook_name_available(api, "jupyterlab", "kratsg-notebook-1") is False


class TestGenerateNotebookName:
    def test_generates_first_available_slot(self) -> None:
        api = FakeCoreV1Api()
        name = generate_notebook_name(api, "jupyterlab", "kratsg")
        assert name == "kratsg-notebook-1"

    def test_skips_taken_names(self) -> None:
        api = FakeCoreV1Api()
        for i in (1, 2):
            api.pods[("jupyterlab", f"kratsg-notebook-{i}")] = _pod_from_manifest(
                {
                    "metadata": {
                        "name": f"kratsg-notebook-{i}",
                        "namespace": "jupyterlab",
                    },
                    "spec": {
                        "containers": [
                            {"image": "x", "resources": {"requests": {}, "limits": {}}}
                        ]
                    },
                }
            )
        name = generate_notebook_name(api, "jupyterlab", "kratsg")
        assert name == "kratsg-notebook-3"

    def test_returns_none_when_exhausted(self) -> None:
        api = FakeCoreV1Api()
        for i in range(1, 20):
            api.pods[("jupyterlab", f"kratsg-notebook-{i}")] = _pod_from_manifest(
                {
                    "metadata": {
                        "name": f"kratsg-notebook-{i}",
                        "namespace": "jupyterlab",
                    },
                    "spec": {
                        "containers": [
                            {"image": "x", "resources": {"requests": {}, "limits": {}}}
                        ]
                    },
                }
            )
        assert generate_notebook_name(api, "jupyterlab", "kratsg") is None
