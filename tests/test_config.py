"""Tests for Settings, focusing on the portal_url field added for commit 1."""

from __future__ import annotations

import pytest

from af_jupyterlab_mcp.config import Settings


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


class TestImageToolOverrides:
    _ENV = "JUPYTERLAB_MCP_IMAGE_TOOL_OVERRIDES"

    def test_defaults_to_no_overrides(self) -> None:
        """No overrides means every image is assumed to support every tool."""
        assert dict(Settings().image_tool_overrides) == {}

    def test_absent_from_env_gives_no_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(self._ENV, raising=False)
        assert dict(Settings.from_env().image_tool_overrides) == {}

    def test_parses_json_object_into_frozensets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            self._ENV,
            '{"img:old": ["read_cell", "notebook_run-all-cells"], "img:ancient": []}',
        )
        overrides = Settings.from_env().image_tool_overrides
        assert overrides == {
            "img:old": frozenset({"read_cell", "notebook_run-all-cells"}),
            "img:ancient": frozenset(),
        }

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("not json", id="bad-json"),
            pytest.param('["read_cell"]', id="not-an-object"),
            pytest.param('{"img:old": "read_cell"}', id="value-not-a-list"),
            pytest.param('{"img:old": ["read_cell", 3]}', id="non-string-id"),
        ],
    )
    def test_malformed_value_raises(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(self._ENV, raw)
        with pytest.raises(ValueError, match=self._ENV):
            Settings.from_env()
