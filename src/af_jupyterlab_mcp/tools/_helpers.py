"""Shared helpers for af-jupyterlab-mcp tool implementations."""

from __future__ import annotations

from typing import Any


def format_error(
    exc: Exception,
    context: str = "",
    hints: list[str] | None = None,
) -> str:
    """Format an error with optional context and recovery hints.

    Mirrors ami-mcp's ``tools._helpers.format_error`` -- errors are always
    returned as formatted strings, never raised as bare exceptions to the LLM.
    """
    lines = [f"**Error**: {exc}"]
    if context:
        lines.append(f"\n{context}")
    if hints:
        lines.append("\n**Try:**")
        lines.extend(f"- {h}" for h in hints)
    return "\n".join(lines)


def append_next_actions(output: str, hints: list[str]) -> str:
    """Append a '## Next steps' section to tool output."""
    if not hints:
        return output
    hint_lines = "\n".join(f"- {h}" for h in hints)
    return f"{output}\n\n---\n**Next steps:**\n{hint_lines}"


_SUMMARY_FIELDS = (
    "id",
    "name",
    "owner",
    "image",
    "pod_status",
    "status",
    "node",
    "hours_remaining",
    "cpu_request",
    "cpu_limit",
    "memory_request_gb",
    "memory_limit_gb",
    "gpu_request",
    "gpu_product",
    "url",
    "log",
)


def format_notebook(info: dict[str, Any]) -> str:
    """Format one notebook's info dict as an LLM-friendly Field | Value table."""
    lines = ["| Field | Value |", "| --- | --- |"]
    lines.extend(f"| {key} | {info[key]} |" for key in _SUMMARY_FIELDS if key in info)
    return "\n".join(lines)


def format_notebook_list(infos: list[dict[str, Any]]) -> str:
    """Format a list of notebook info dicts as an LLM-friendly markdown table."""
    if not infos:
        return "No notebooks found."
    keys = ["id", "name", "pod_status", "status", "hours_remaining", "image"]
    header = "| " + " | ".join(keys) + " |"
    separator = "| " + " | ".join("---" for _ in keys) + " |"
    lines = [header, separator]
    lines.extend(
        "| " + " | ".join(str(info.get(k, "")) for k in keys) + " |" for info in infos
    )
    return "\n".join(lines)
