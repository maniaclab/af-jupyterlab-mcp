"""Tests for broker-issued JWT bearer extraction and claims retrieval.

jupyterlab-mcp's owner-scoping is only as strong as this module: every tool
gets ``claims.unixname`` from here, never from a caller-supplied argument.
These tests exercise ``extract_bearer`` and ``get_broker_claims`` against a
duck-typed fake verifier matching the real
``af_credentials.verifier.BrokerTokenVerifier.verify(token) -> BrokerClaims |
None`` shape (an async method returning a frozen dataclass with
``sub``/``jti``/``exp`` always present and ``uid``/``gid``/``unixname``
``None`` unless the issuing broker included POSIX claims) -- confirmed
directly against the installed ``af-credentials`` package (a hard
dependency here, unlike ami-mcp where broker mode is an optional extra);
see ``tests/test_server.py``'s ``TestBrokerModeApp`` for the same contract
exercised through the real ``BrokerTokenVerifier`` class end-to-end.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jupyterlab_mcp.auth.broker import extract_bearer, get_broker_claims


def _make_ctx(headers: dict[str, str]) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.request.headers = headers
    return ctx


class _FakeClaims:
    def __init__(
        self, *, sub: str, unixname: str | None, uid: int | None, gid: int | None
    ) -> None:
        self.sub = sub
        self.unixname = unixname
        self.uid = uid
        self.gid = gid


class _FakeVerifier:
    """Duck-typed stand-in for af_credentials.verifier.BrokerTokenVerifier."""

    def __init__(self, claims_by_token: dict[str, _FakeClaims]) -> None:
        self._claims_by_token = claims_by_token

    async def verify(self, token: str) -> _FakeClaims | None:
        return self._claims_by_token.get(token)


class TestExtractBearer:
    def test_returns_token(self) -> None:
        ctx = _make_ctx({"authorization": "Bearer abc123"})
        assert extract_bearer(ctx) == "abc123"

    def test_case_insensitive_scheme(self) -> None:
        ctx = _make_ctx({"authorization": "bearer abc123"})
        assert extract_bearer(ctx) == "abc123"

    def test_missing_header_raises(self) -> None:
        ctx = _make_ctx({})
        with pytest.raises(PermissionError, match="Bearer"):
            extract_bearer(ctx)

    def test_non_bearer_scheme_raises(self) -> None:
        ctx = _make_ctx({"authorization": "Basic dXNlcjpwYXNz"})
        with pytest.raises(PermissionError, match="Bearer"):
            extract_bearer(ctx)


class TestGetBrokerClaims:
    async def test_returns_claims_for_valid_token(self) -> None:
        verifier = _FakeVerifier(
            {"good": _FakeClaims(sub="kratsg", unixname="kratsg", uid=1000, gid=1000)}
        )
        ctx = _make_ctx({"authorization": "Bearer good"})
        claims = await get_broker_claims(ctx, verifier)
        assert claims.unixname == "kratsg"
        assert claims.uid == 1000

    async def test_invalid_token_raises_permission_error(self) -> None:
        verifier = _FakeVerifier({})
        ctx = _make_ctx({"authorization": "Bearer bad"})
        with pytest.raises(PermissionError, match="invalid or expired"):
            await get_broker_claims(ctx, verifier)

    async def test_missing_bearer_raises_before_verify(self) -> None:
        verifier = _FakeVerifier(
            {"good": _FakeClaims(sub="x", unixname="x", uid=1, gid=1)}
        )
        ctx = _make_ctx({})
        with pytest.raises(PermissionError, match="Bearer"):
            await get_broker_claims(ctx, verifier)

    async def test_claims_missing_unixname_raises(self) -> None:
        """include_posix=true should always deliver unixname for this
        backend's audience; if it is somehow absent, fail closed rather than
        silently scoping ownership to None."""
        verifier = _FakeVerifier(
            {"good": _FakeClaims(sub="kratsg", unixname=None, uid=None, gid=None)}
        )
        ctx = _make_ctx({"authorization": "Bearer good"})
        with pytest.raises(PermissionError, match="unixname"):
            await get_broker_claims(ctx, verifier)
