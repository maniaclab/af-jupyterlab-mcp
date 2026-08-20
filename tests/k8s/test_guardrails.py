"""Tests for server-side guardrail validation (issue #189 range enforcement)."""

from __future__ import annotations

import pytest

from jupyterlab_mcp.config import Settings
from jupyterlab_mcp.k8s.errors import GuardrailError, ImageNotAllowedError
from jupyterlab_mcp.k8s.guardrails import compute_limits, validate_create_request

_ALLOWED_IMAGE = "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest"


@pytest.fixture
def settings() -> Settings:
    return Settings(cpu_images=(_ALLOWED_IMAGE,), gpu_images=())


class TestCpuRange:
    @pytest.mark.parametrize("cpu_cores", [1, 8, 16])
    def test_in_range_ok(self, settings: Settings, cpu_cores: int) -> None:
        validate_create_request(
            image=_ALLOWED_IMAGE,
            cpu_cores=cpu_cores,
            memory_gb=1,
            gpus=0,
            duration_hours=8,
            settings=settings,
        )

    @pytest.mark.parametrize("cpu_cores", [0, -1, 17, 100])
    def test_out_of_range_rejected(self, settings: Settings, cpu_cores: int) -> None:
        with pytest.raises(GuardrailError, match="cpu_cores"):
            validate_create_request(
                image=_ALLOWED_IMAGE,
                cpu_cores=cpu_cores,
                memory_gb=1,
                gpus=0,
                duration_hours=8,
                settings=settings,
            )


class TestMemoryRange:
    @pytest.mark.parametrize("memory_gb", [1, 128, 256])
    def test_in_range_ok(self, settings: Settings, memory_gb: int) -> None:
        validate_create_request(
            image=_ALLOWED_IMAGE,
            cpu_cores=1,
            memory_gb=memory_gb,
            gpus=0,
            duration_hours=8,
            settings=settings,
        )

    @pytest.mark.parametrize("memory_gb", [0, -1, 257, 1000])
    def test_out_of_range_rejected(self, settings: Settings, memory_gb: int) -> None:
        with pytest.raises(GuardrailError, match="memory_gb"):
            validate_create_request(
                image=_ALLOWED_IMAGE,
                cpu_cores=1,
                memory_gb=memory_gb,
                gpus=0,
                duration_hours=8,
                settings=settings,
            )


class TestDurationRange:
    @pytest.mark.parametrize("duration_hours", [1, 36, 72])
    def test_in_range_ok(self, settings: Settings, duration_hours: int) -> None:
        validate_create_request(
            image=_ALLOWED_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            duration_hours=duration_hours,
            settings=settings,
        )

    @pytest.mark.parametrize("duration_hours", [0, -1, 73, 1000])
    def test_out_of_range_rejected(
        self, settings: Settings, duration_hours: int
    ) -> None:
        with pytest.raises(GuardrailError, match="duration_hours"):
            validate_create_request(
                image=_ALLOWED_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                duration_hours=duration_hours,
                settings=settings,
            )

    def test_hand_crafted_73h_is_rejected_server_side(
        self, settings: Settings
    ) -> None:
        """Regression guard for the portal's own gap: the HTML form caps at
        72h client-side only, so a hand-crafted POST can exceed it. jupyterlab-mcp
        must reject server-side regardless of what a client claims."""
        with pytest.raises(GuardrailError):
            validate_create_request(
                image=_ALLOWED_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                duration_hours=73,
                settings=settings,
            )


class TestImageAllowlist:
    def test_allowed_image_ok(self, settings: Settings) -> None:
        validate_create_request(
            image=_ALLOWED_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=0,
            duration_hours=8,
            settings=settings,
        )

    def test_disallowed_image_rejected(self, settings: Settings) -> None:
        with pytest.raises(ImageNotAllowedError, match="not-on-allowlist"):
            validate_create_request(
                image="not-on-allowlist:latest",
                cpu_cores=1,
                memory_gb=1,
                gpus=0,
                duration_hours=8,
                settings=settings,
            )

    def test_gpu_image_from_gpu_allowlist_ok(self) -> None:
        gpu_image = "hub.opensciencegrid.org/usatlas/ml-platform-gpu:latest"
        settings = Settings(cpu_images=(), gpu_images=(gpu_image,))
        validate_create_request(
            image=gpu_image,
            cpu_cores=1,
            memory_gb=1,
            gpus=1,
            duration_hours=8,
            settings=settings,
        )


class TestGpuCountGuardrail:
    def test_negative_gpus_rejected(self, settings: Settings) -> None:
        with pytest.raises(GuardrailError, match="gpus"):
            validate_create_request(
                image=_ALLOWED_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=-1,
                duration_hours=8,
                settings=settings,
            )

    def test_quota_max_gpus_per_request_enforced_when_configured(self) -> None:
        settings = Settings(
            cpu_images=(_ALLOWED_IMAGE,), gpu_images=(), max_gpus_per_request=2
        )
        with pytest.raises(GuardrailError, match="max_gpus_per_request"):
            validate_create_request(
                image=_ALLOWED_IMAGE,
                cpu_cores=1,
                memory_gb=1,
                gpus=3,
                duration_hours=8,
                settings=settings,
            )

    def test_quota_unset_by_default_allows_any_gpu_count(
        self, settings: Settings
    ) -> None:
        # decision 4: no default max-GPU cap beyond the portal's ranges.
        validate_create_request(
            image=_ALLOWED_IMAGE,
            cpu_cores=1,
            memory_gb=1,
            gpus=8,
            duration_hours=8,
            settings=settings,
        )


class TestComputeLimits:
    def test_limit_is_2x_request(self) -> None:
        cpu_limit, memory_limit_gb = compute_limits(cpu_cores=2, memory_gb=8)
        assert cpu_limit == 4
        assert memory_limit_gb == 16
