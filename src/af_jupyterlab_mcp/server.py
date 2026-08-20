"""MCP server setup for af-jupyterlab-mcp.

HTTP transport only (no stdio): this backend always runs behind the
af-mcp-platform aggregator/broker, in-cluster, as a Deployment -- there is
no local single-user CLI use case the way ami-mcp's stdio mode serves a
developer's own VOMS proxy.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import uvicorn
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse

from af_jupyterlab_mcp.auth.broker import (
    make_broker_token_verifier,
    make_broker_verifier,
)
from af_jupyterlab_mcp.config import Settings
from af_jupyterlab_mcp.k8s.notebooks import K8sClients
from af_jupyterlab_mcp.tools import jupyterlab as jupyterlab_tools

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.applications import Starlette
    from starlette.requests import Request

_INSTRUCTIONS = (
    "MCP server for per-user JupyterLab server management on the ATLAS AF "
    "Kubernetes cluster. Provides tools to create, inspect, and delete your "
    "own JupyterLab server (the same notebooks af-portal deploys), and to "
    "check GPU availability and the supported image list. Every server is "
    "scoped to your own broker identity -- there is no way to act on "
    "another user's server."
)


def _register_all(mcp: MCPServer) -> None:
    """Register every tool module on *mcp*."""
    jupyterlab_tools.register(mcp)


def _build_k8s_clients() -> K8sClients:
    """Build K8sClients from the in-cluster ServiceAccount.

    Never a kubeconfig secret (per af-mcp-platform issue #189): this backend
    runs as a Deployment in the `mcp` namespace with a cross-namespace
    RoleBinding into the notebook namespace, plus a read-only ClusterRole
    for GPU-availability parity with the portal.
    """
    k8s_config.load_incluster_config()
    api_client = k8s_client.ApiClient()
    return K8sClients(
        core_v1=k8s_client.CoreV1Api(api_client),
        networking_v1=k8s_client.NetworkingV1Api(api_client),
    )


def _make_broker_app(
    *,
    jwks_url: str,
    issuer: str,
    audience: str,
    resource_url: str,
    host: str,
    settings: Settings,
) -> Starlette:
    """Build the ASGI app for HTTP transport behind the AF credential broker."""
    token_verifier = make_broker_token_verifier(jwks_url, issuer, audience)
    broker_verifier = make_broker_verifier(jwks_url, issuer, audience)

    @asynccontextmanager
    async def _lifespan(_server: MCPServer) -> AsyncGenerator[dict[str, Any], None]:
        k8s_clients = _build_k8s_clients()
        yield {
            "broker_verifier": broker_verifier,
            "k8s_clients": k8s_clients,
            "settings": settings,
        }

    mcp = MCPServer(
        "af-jupyterlab-mcp",
        instructions=_INSTRUCTIONS,
        lifespan=_lifespan,
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(resource_url),
            # The aggregator injects the bearer itself; no OAuth discovery
            # chain to advertise on this resource.
            resource_server_url=None,
            client_registration_options=ClientRegistrationOptions(enabled=False),
            required_scopes=[],
        ),
    )
    _register_all(mcp)

    async def _healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    mcp.custom_route("/healthz", methods=["GET"])(_healthz)

    return mcp.streamable_http_app(
        streamable_http_path="/mcp", host=host, stateless_http=True
    )


def serve(
    host: str = "0.0.0.0",
    port: int = 8000,
    broker_url: str | None = None,
    broker_jwks_url: str | None = None,
    broker_issuer: str | None = None,
    audience: str = "af-jupyterlab-mcp",
    resource_url: str | None = None,
    forwarded_allow_ips: str = "127.0.0.1",
    log_level: str = "info",
) -> None:
    """Start the MCP server over HTTP transport, behind the AF credential broker."""
    if not broker_url:
        sys.stderr.write(
            "[af-jupyterlab-mcp] Error: serve requires --broker-url "
            "(or JUPYTERLAB_MCP_BROKER_URL).\n"
        )
        sys.exit(1)

    settings = Settings.from_env()
    app = _make_broker_app(
        jwks_url=broker_jwks_url or f"{broker_url.rstrip('/')}/.well-known/jwks.json",
        issuer=broker_issuer or broker_url,
        audience=audience,
        resource_url=resource_url or f"http://{host}:{port}",
        host=host,
        settings=settings,
    )

    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        log_level=log_level,
    )
