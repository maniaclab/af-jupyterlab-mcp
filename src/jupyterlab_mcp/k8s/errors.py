"""Exceptions raised by the jupyterlab-mcp Kubernetes layer."""

from __future__ import annotations


class GuardrailError(ValueError):
    """A create request violates a server-side validation guardrail."""


class ImageNotAllowedError(GuardrailError):
    """The requested image is not on the chart-values-driven allowlist."""


class QuotaExceededError(RuntimeError):
    """A configured (opt-in) quota knob was exceeded."""


class GPUCapacityError(RuntimeError):
    """The requested GPU count does not fit in currently available capacity."""


class NameConflictError(RuntimeError):
    """A dual-writer race lost: the object name was taken between check and create."""


class NotFoundOrNotYoursError(LookupError):
    """A pod exists but is not owned by the caller, or does not exist at all.

    Deliberately conflates "not found" and "not yours" in the message so a
    caller cannot distinguish the two and enumerate other users' server names.
    """
