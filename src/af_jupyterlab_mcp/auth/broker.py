"""Broker-issued identity JWT verification (af-mcp-platform issue #189).

af-jupyterlab-mcp is an AF-native backend (``type: broker-issued``, ``auth_type:
bearer``, ``audience: af-jupyterlab-mcp``, ``include_posix: true``): the broker
is authoritative, there is no external identity to federate. Every request
carries a broker-issued RS256 JWT as its bearer; ``af_credentials``'s
``BrokerTokenVerifier`` checks it against the broker's JWKS (cached, no
per-request network round trip).

Two verifications of the same bearer happen per request, same pattern as
ami-mcp's ``auth.broker`` (``extract_bearer``/``BrokerProxyClientFactory``):

1. The mcp SDK's own ``token_verifier=`` (``af_credentials.mcp.
   mcp_token_verifier``) gates transport-level access, but the AccessToken
   it hands back to tool code carries only ``sub`` -- POSIX claims are
   dropped by that adapter.
2. Tool code that needs ``unixname``/``uid`` (i.e. every owner-scoped tool)
   calls ``get_broker_claims(ctx, verifier)``, which re-runs
   ``BrokerTokenVerifier.verify(token)`` directly to get the full
   ``BrokerClaims`` (cached JWKS, local signature check, no network).

Requires the ``af-credentials`` package (a hard dependency of this backend,
unlike ami-mcp where broker mode is an optional extra alongside a
shared-secret mode -- af-jupyterlab-mcp has no non-broker auth mode).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from af_credentials.mcp import mcp_token_verifier
    from af_credentials.verifier import BrokerTokenVerifier

    HAS_AF_CREDENTIALS = True
except ImportError:  # pragma: no cover - exercised only without the extra
    HAS_AF_CREDENTIALS = False

if TYPE_CHECKING:
    from mcp.server.auth.provider import TokenVerifier

MISSING_AF_CREDENTIALS_MSG = "af-jupyterlab-mcp requires the af-credentials package: pip install af-credentials[mcp]"


def extract_bearer(ctx: Any) -> str:
    """Return the bearer token from the current request's Authorization header.

    The token has already been verified by the server's TokenVerifier before
    any tool runs; this re-reads it so ``get_broker_claims`` can pull the
    full claim set (uid/gid/unixname) that the SDK-level verifier drops.

    Raises:
        PermissionError: If the header is missing or not a Bearer scheme.
    """
    auth = ctx.request_context.request.headers.get("authorization", "") or ""
    if not auth.lower().startswith("bearer "):
        msg = "Missing Bearer token in Authorization header"
        raise PermissionError(msg)
    return auth[7:].strip()


async def get_broker_claims(ctx: Any, verifier: Any) -> Any:
    """Return the verified broker claims for the current request.

    Args:
        ctx: The MCP request Context.
        verifier: The ``BrokerTokenVerifier`` instance built in the server
            lifespan (stored in ``ctx.request_context.lifespan_context
            ["broker_verifier"]``; passed explicitly here for testability).

    Returns:
        The ``af_credentials.verifier.BrokerClaims`` for this request,
        carrying at least ``sub`` and, because this backend is registered
        with ``include_posix: true``, ``uid``/``gid``/``unixname``.

    Raises:
        PermissionError: missing/non-Bearer header, an invalid or expired
            token, or (fail-closed) a token that verified but is missing
            ``unixname`` -- every tool's owner-scoping depends on it.
    """
    token = extract_bearer(ctx)
    claims = await verifier.verify(token)
    if claims is None:
        msg = "invalid or expired broker token"
        raise PermissionError(msg)
    if not getattr(claims, "unixname", None):
        msg = (
            "broker token verified but carries no unixname -- this principal "
            "has no POSIX identity in the AF directory (see af-mcp-platform "
            "issue #189 open question 3)"
        )
        raise PermissionError(msg)
    return claims


def make_broker_token_verifier(
    jwks_url: str, issuer: str, audience: str
) -> TokenVerifier:
    """Build the mcp SDK TokenVerifier that gates transport-level access."""
    if not HAS_AF_CREDENTIALS:
        raise ImportError(MISSING_AF_CREDENTIALS_MSG)
    verifier: TokenVerifier = mcp_token_verifier(
        BrokerTokenVerifier(jwks_url, issuer, audience)
    )
    return verifier


def make_broker_verifier(jwks_url: str, issuer: str, audience: str) -> Any:
    """Build the raw ``BrokerTokenVerifier`` used directly by ``get_broker_claims``.

    Distinct from ``make_broker_token_verifier``: that one is wrapped for
    the SDK and drops POSIX claims; this one is stored in the lifespan
    context and called again per-tool to recover them.
    """
    if not HAS_AF_CREDENTIALS:
        raise ImportError(MISSING_AF_CREDENTIALS_MSG)
    return BrokerTokenVerifier(jwks_url, issuer, audience)
