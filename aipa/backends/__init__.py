from .base import Backend, LogEvent, ParseState, RunResult, RunSpec
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend

BACKENDS: dict[str, Backend] = {
    ClaudeCodeBackend.name: ClaudeCodeBackend(),
    CodexBackend.name: CodexBackend(),
}

EFFORT_OPTIONS = ["default", "low", "medium", "high", "xhigh", "max"]


def model_options() -> list[str]:
    """Combined, prefixed model list for the node's COMBO widget."""
    out: list[str] = []
    for b in BACKENDS.values():
        out.extend(f"{b.prefix}/{m}" for m in b.models)
    return out


__all__ = [
    "Backend",
    "LogEvent",
    "ParseState",
    "RunResult",
    "RunSpec",
    "ClaudeCodeBackend",
    "CodexBackend",
    "BACKENDS",
    "EFFORT_OPTIONS",
    "model_options",
]
