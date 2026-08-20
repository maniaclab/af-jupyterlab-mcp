"""Command-line interface for af-jupyterlab-mcp."""

from __future__ import annotations

import argparse
import os

from af_jupyterlab_mcp.server import serve


def main() -> None:
    """Entry point for the af-jupyterlab-mcp command."""
    parser = argparse.ArgumentParser(
        prog="af-jupyterlab-mcp",
        description="MCP Server for per-user JupyterLab server management on the ATLAS AF cluster",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the MCP server (HTTP transport only)",
    )
    serve_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to serve on (default: 8000)",
    )
    serve_parser.add_argument(
        "--broker-url",
        default=os.environ.get("JUPYTERLAB_MCP_BROKER_URL"),
        help="AF credential broker base URL (env: JUPYTERLAB_MCP_BROKER_URL)",
    )
    serve_parser.add_argument(
        "--broker-jwks-url",
        default=os.environ.get("JUPYTERLAB_MCP_BROKER_JWKS_URL"),
        help=(
            "JWKS URL for verifying broker-issued JWTs "
            "(env: JUPYTERLAB_MCP_BROKER_JWKS_URL; default: BROKER_URL/.well-known/jwks.json)"
        ),
    )
    serve_parser.add_argument(
        "--broker-issuer",
        default=os.environ.get("JUPYTERLAB_MCP_BROKER_ISSUER"),
        help="Expected iss claim of broker-issued JWTs (default: BROKER_URL)",
    )
    serve_parser.add_argument(
        "--audience",
        default="af-jupyterlab-mcp",
        help="Expected aud claim of broker-issued JWTs (default: af-jupyterlab-mcp)",
    )
    serve_parser.add_argument(
        "--resource-url",
        default=os.environ.get("JUPYTERLAB_MCP_RESOURCE_URL"),
        help=(
            "Externally visible base URL of this server "
            "(env: JUPYTERLAB_MCP_RESOURCE_URL; default: http://HOST:PORT)"
        ),
    )
    serve_parser.add_argument(
        "--forwarded-allow-ips",
        default="127.0.0.1",
        help="IPs trusted for X-Forwarded-* headers (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--log-level",
        default="info",
        help="uvicorn log level (default: info)",
    )

    args = parser.parse_args()

    if args.command == "serve":
        serve(
            host=args.host,
            port=args.port,
            broker_url=args.broker_url,
            broker_jwks_url=args.broker_jwks_url,
            broker_issuer=args.broker_issuer,
            audience=args.audience,
            resource_url=args.resource_url,
            forwarded_allow_ips=args.forwarded_allow_ips,
            log_level=args.log_level,
        )
    else:
        parser.print_help()
