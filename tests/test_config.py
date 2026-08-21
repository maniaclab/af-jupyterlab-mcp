"""Tests for Settings, focusing on the portal_url field added for commit 1."""

from __future__ import annotations

from typing import TYPE_CHECKING

from af_jupyterlab_mcp.config import Settings

if TYPE_CHECKING:
    import pytest


class TestPortalUrl:
    def test_portal_url_defaults_to_none(self) -> None:
        """portal_url is absent from the default Settings."""
        s = Settings()
        assert s.portal_url is None

    def test_portal_url_set_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JUPYTERLAB_MCP_PORTAL_URL populates portal_url."""
        monkeypatch.setenv(
            "JUPYTERLAB_MCP_PORTAL_URL", "https://af.uchicago.edu/jupyterlab"
        )
        s = Settings.from_env()
        assert s.portal_url == "https://af.uchicago.edu/jupyterlab"

    def test_portal_url_absent_from_env_gives_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When JUPYTERLAB_MCP_PORTAL_URL is not set, portal_url is None."""
        monkeypatch.delenv("JUPYTERLAB_MCP_PORTAL_URL", raising=False)
        s = Settings.from_env()
        assert s.portal_url is None
