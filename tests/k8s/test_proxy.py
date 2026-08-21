"""Tests for proxy infrastructure: get_notebook_token and call_notebook_tool.

All upstream MCP calls are mocked -- no real notebook is contacted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from af_jupyterlab_mcp.k8s.notebooks import get_notebook_token
from af_jupyterlab_mcp.k8s.proxy import call_notebook_tool

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _Obj:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _make_pod(token: str = "test-token-abc") -> Any:
    """Return a minimal pod-like object with JUPYTER_TOKEN in env."""
    return _Obj(
        metadata=_Obj(name="nb-alice-1"),
        spec=_Obj(
            containers=[
                _Obj(
                    env=[
                        _Obj(name="JUPYTER_TOKEN", value=token),
                        _Obj(name="OTHER_VAR", value="other"),
                    ]
                )
            ]
        ),
    )


def _make_pod_no_token() -> Any:
    """Return a pod-like object with no JUPYTER_TOKEN in env."""
    return _Obj(
        metadata=_Obj(name="nb-alice-1"),
        spec=_Obj(containers=[_Obj(env=[_Obj(name="OTHER_VAR", value="other")])]),
    )


def _make_tool_result(text: str, is_error: bool = False) -> MagicMock:
    content = MagicMock()
    content.type = "text"
    content.text = text
    result = MagicMock()
    result.content = [content]
    result.isError = is_error
    return result


# ---------------------------------------------------------------------------
# Tests: get_notebook_token
# ---------------------------------------------------------------------------


class TestGetNotebookToken:
    def test_reads_jupyter_token_from_pod_env(self) -> None:
        pod = _make_pod("secret-token-xyz")
        assert get_notebook_token(pod) == "secret-token-xyz"

    def test_raises_value_error_when_token_missing(self) -> None:
        pod = _make_pod_no_token()
        with pytest.raises(ValueError, match="JUPYTER_TOKEN not found"):
            get_notebook_token(pod)

    def test_raises_value_error_when_token_is_empty_string(self) -> None:
        """An empty-string JUPYTER_TOKEN is treated as missing."""
        pod = _make_pod(token="")
        pod.spec.containers[0].env[0] = _Obj(name="JUPYTER_TOKEN", value="")
        with pytest.raises(ValueError, match="JUPYTER_TOKEN not found"):
            get_notebook_token(pod)


# ---------------------------------------------------------------------------
# Tests: call_notebook_tool
# ---------------------------------------------------------------------------


class TestCallNotebookTool:
    async def test_returns_formatted_result_on_success(self) -> None:
        """call_notebook_tool calls the upstream tool and returns the text content."""
        mock_result = _make_tool_result("execution output")

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def fake_transport(_url: Any, **_kwargs: Any):
            yield (AsyncMock(), AsyncMock())

        @asynccontextmanager
        async def fake_session_ctx(*_args: Any, **_kwargs: Any):
            yield mock_session

        with (
            patch("af_jupyterlab_mcp.k8s.proxy.streamable_http_client", fake_transport),
            patch(
                "af_jupyterlab_mcp.k8s.proxy.ClientSession",
                return_value=fake_session_ctx(),
            ),
        ):
            result = await call_notebook_tool(
                notebook_url="https://nb-alice-1.notebooks.af.uchicago.edu",
                token="secret-token",
                tool_name="execute_code",
                tool_args={"code": "print('hello')"},
            )

        assert "execution output" in result

    async def test_includes_token_in_mcp_url(self) -> None:
        """The token is injected into the upstream MCP URL; never returned to caller."""
        captured_url: list[str] = []
        mock_result = _make_tool_result("ok")

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def fake_transport(url: str, **_kwargs: Any):
            captured_url.append(url)
            yield (AsyncMock(), AsyncMock())

        @asynccontextmanager
        async def fake_session_ctx(*_args: Any, **_kwargs: Any):
            yield mock_session

        with (
            patch("af_jupyterlab_mcp.k8s.proxy.streamable_http_client", fake_transport),
            patch(
                "af_jupyterlab_mcp.k8s.proxy.ClientSession",
                return_value=fake_session_ctx(),
            ),
        ):
            await call_notebook_tool(
                notebook_url="https://nb-alice-1.notebooks.af.uchicago.edu",
                token="secret-token",
                tool_name="execute_code",
                tool_args={},
            )

        assert len(captured_url) == 1
        assert "secret-token" in captured_url[0]
        assert "/mcp" in captured_url[0]

    async def test_passes_correct_tool_name_and_args(self) -> None:
        """The upstream tool_name and tool_args are forwarded verbatim."""
        captured_calls: list[tuple[str, dict[str, object]]] = []
        mock_result = _make_tool_result("result")

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()

        async def fake_call_tool(
            name: str, arguments: dict[str, object] | None = None, **_kwargs: Any
        ) -> Any:
            captured_calls.append((name, arguments or {}))
            return mock_result

        mock_session.call_tool = fake_call_tool

        @asynccontextmanager
        async def fake_transport(_url: str, **_kwargs: Any):
            yield (AsyncMock(), AsyncMock())

        @asynccontextmanager
        async def fake_session_ctx(*_args: Any, **_kwargs: Any):
            yield mock_session

        with (
            patch("af_jupyterlab_mcp.k8s.proxy.streamable_http_client", fake_transport),
            patch(
                "af_jupyterlab_mcp.k8s.proxy.ClientSession",
                return_value=fake_session_ctx(),
            ),
        ):
            await call_notebook_tool(
                notebook_url="https://nb-alice-1.notebooks.af.uchicago.edu",
                token="tok",
                tool_name="notebook_get-selected-cell",
                tool_args={"extra": "arg"},
            )

        assert len(captured_calls) == 1
        name, args = captured_calls[0]
        assert name == "notebook_get-selected-cell"
        assert args == {"extra": "arg"}

    async def test_returns_error_string_when_upstream_fails(self) -> None:
        """Transport errors are caught and returned as error strings, not raised."""
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock(side_effect=ConnectionError("unreachable"))

        @asynccontextmanager
        async def fake_transport(_url: str, **_kwargs: Any):
            yield (AsyncMock(), AsyncMock())

        @asynccontextmanager
        async def fake_session_ctx(*_args: Any, **_kwargs: Any):
            yield mock_session

        with (
            patch("af_jupyterlab_mcp.k8s.proxy.streamable_http_client", fake_transport),
            patch(
                "af_jupyterlab_mcp.k8s.proxy.ClientSession",
                return_value=fake_session_ctx(),
            ),
        ):
            result = await call_notebook_tool(
                notebook_url="https://nb-alice-1.notebooks.af.uchicago.edu",
                token="tok",
                tool_name="execute_code",
                tool_args={},
            )

        assert "Error" in result or "error" in result.lower()

    async def test_token_not_returned_in_result(self) -> None:
        """The injected token must never appear in the return value."""
        mock_result = _make_tool_result("execution ok")

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def fake_transport(_url: str, **_kwargs: Any):
            yield (AsyncMock(), AsyncMock())

        @asynccontextmanager
        async def fake_session_ctx(*_args: Any, **_kwargs: Any):
            yield mock_session

        secret = "super-secret-token-xyz"
        with (
            patch("af_jupyterlab_mcp.k8s.proxy.streamable_http_client", fake_transport),
            patch(
                "af_jupyterlab_mcp.k8s.proxy.ClientSession",
                return_value=fake_session_ctx(),
            ),
        ):
            result = await call_notebook_tool(
                notebook_url="https://nb-alice-1.notebooks.af.uchicago.edu",
                token=secret,
                tool_name="execute_code",
                tool_args={},
            )

        assert secret not in result
