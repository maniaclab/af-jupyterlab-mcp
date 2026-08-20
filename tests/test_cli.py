"""Tests for the jupyterlab-mcp CLI argument wiring."""

from __future__ import annotations

from unittest.mock import patch

from jupyterlab_mcp.cli import main


class TestCliServe:
    def test_serve_forwards_broker_url(self) -> None:
        with (
            patch("jupyterlab_mcp.cli.serve") as mock_serve,
            patch(
                "sys.argv",
                ["jupyterlab-mcp", "serve", "--broker-url", "https://broker.invalid"],
            ),
        ):
            main()
        _args, kwargs = mock_serve.call_args
        assert kwargs["broker_url"] == "https://broker.invalid"
        assert kwargs["audience"] == "jupyterlab-mcp"
        assert kwargs["host"] == "0.0.0.0"
        assert kwargs["port"] == 8000

    def test_serve_forwards_all_broker_overrides(self) -> None:
        with (
            patch("jupyterlab_mcp.cli.serve") as mock_serve,
            patch(
                "sys.argv",
                [
                    "jupyterlab-mcp",
                    "serve",
                    "--broker-url",
                    "https://broker.invalid",
                    "--broker-jwks-url",
                    "https://broker.invalid/jwks",
                    "--broker-issuer",
                    "https://issuer.invalid",
                    "--audience",
                    "custom-aud",
                    "--resource-url",
                    "https://jupyterlab-mcp.invalid",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9000",
                ],
            ),
        ):
            main()
        _args, kwargs = mock_serve.call_args
        assert kwargs["broker_jwks_url"] == "https://broker.invalid/jwks"
        assert kwargs["broker_issuer"] == "https://issuer.invalid"
        assert kwargs["audience"] == "custom-aud"
        assert kwargs["resource_url"] == "https://jupyterlab-mcp.invalid"
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 9000

    def test_no_command_prints_help(self) -> None:
        with patch("sys.argv", ["jupyterlab-mcp"]):
            main()
