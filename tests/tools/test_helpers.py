"""Unit tests for af_jupyterlab_mcp.tools._helpers."""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from af_jupyterlab_mcp.tools._helpers import (
    append_next_actions,
    format_error,
    format_notebook,
    format_notebook_list,
)


def _error_text(result: CallToolResult) -> str:
    """Extract the sole text block's content from an is_error CallToolResult."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


class TestFormatError:
    def test_returns_an_is_error_call_tool_result(self) -> None:
        result = format_error(ValueError("bad"))
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        assert result.structured_content is None

    def test_message_is_included(self) -> None:
        result = format_error(ValueError("bad thing happened"))
        assert "bad thing happened" in _error_text(result)

    def test_context_is_included(self) -> None:
        result = format_error(ValueError("bad"), context="extra context")
        assert "extra context" in _error_text(result)

    def test_hints_are_appended(self) -> None:
        result = format_error(ValueError("bad"), hints=["Try again."])
        assert "Try again." in _error_text(result)
        assert "**Try:**" in _error_text(result)

    def test_no_hints_omits_try_section(self) -> None:
        result = format_error(ValueError("bad"))
        assert "**Try:**" not in _error_text(result)


def test_append_next_actions_appends_bulleted_list() -> None:
    output = append_next_actions("base output", ["Do X.", "Do Y."])
    assert "base output" in output
    assert "- Do X." in output
    assert "- Do Y." in output


def test_append_next_actions_no_hints_returns_unchanged() -> None:
    assert append_next_actions("base output", []) == "base output"


class TestFormatNotebook:
    def test_only_known_summary_fields_are_shown(self) -> None:
        table = format_notebook({"id": "nb-1", "name": "mine", "unrelated_key": "x"})
        assert "nb-1" in table
        assert "mine" in table
        assert "unrelated_key" not in table


class TestFormatNotebookList:
    def test_empty_list_says_no_notebooks_found(self) -> None:
        assert format_notebook_list([]) == "No notebooks found."

    def test_lists_summary_columns(self) -> None:
        table = format_notebook_list([{"id": "nb-1", "name": "mine"}])
        assert "nb-1" in table
        assert "mine" in table
