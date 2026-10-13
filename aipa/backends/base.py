"""Backend protocol shared by the Claude Code and Codex adapters."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class LogEvent:
    kind: str  # status | thinking | text | tool | tool_result | search | warn | error | result | done
    text: str


@dataclass
class RunSpec:
    user_message: str
    system_prompt: str
    model: Optional[str]           # bare model id or None for the CLI default
    effort: Optional[str]          # low|medium|high|xhigh|max or None
    web_search: bool
    image_paths: list[str]
    schema: Optional[dict]
    resume_id: Optional[str]
    workspace: Path
    run_dir: Path
    extra_args: list[str] = field(default_factory=list)


@dataclass
class ParseState:
    session_id: Optional[str] = None
    final_text: str = ""
    structured: Optional[dict] = None
    error: Optional[str] = None
    is_error: bool = False
    text_parts: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    seen_ids: set = field(default_factory=set)
    pending_searches: set = field(default_factory=set)
    completed_searches: int = 0


@dataclass
class RunResult:
    text: str
    structured: Optional[dict]
    session_id: Optional[str]
    error: Optional[str]
    usage: dict
    warnings: list[str] = field(default_factory=list)
    completed_searches: int = 0


class Backend(ABC):
    name: str = ""       # combo value, e.g. "claude_code"
    display: str = ""    # human label
    prefix: str = ""     # model prefix in the combined combo, e.g. "claude"
    models: list[str] = []
    default_model: Optional[str] = None
    cli_name: str = ""   # executable base name

    def resolve_model(self, combo_value: str, custom_model: str) -> Optional[str]:
        """Turn the combined combo value (+ optional override) into a bare model id."""
        custom = (custom_model or "").strip()
        if custom:
            return custom
        if combo_value and combo_value.startswith(self.prefix + "/"):
            bare = combo_value.split("/", 1)[1].strip()
            return None if bare in ("", "default") else bare
        return self.default_model

    @abstractmethod
    def build_command(self, exe: str, spec: RunSpec) -> list[str]:
        ...

    @abstractmethod
    def stdin_payload(self, spec: RunSpec) -> str:
        """Text fed to the CLI's stdin (the prompt)."""
        ...

    @abstractmethod
    def parse_line(self, line: str, state: ParseState) -> list[LogEvent]:
        ...

    def finalize(self, state: ParseState, exit_code: int, stderr_tail: str) -> RunResult:
        text = state.final_text or "\n".join(p for p in state.text_parts if p).strip()
        structured = state.structured
        if structured is None and text:
            structured = extract_json_object(text)
        error = state.error
        warnings: list[str] = []
        if state.is_error and not error:
            error = text or f"{self.display} reported an error"
        have_result = bool(structured) or bool(text.strip())
        if exit_code != 0 and not error:
            detail = f"{self.display} exited with code {exit_code}" + (
                f": {stderr_tail.strip()[-800:]}" if stderr_tail.strip() else "")
            if have_result:
                # the answer arrived before the CLI died on shutdown; keep it, but say so
                warnings.append(detail)
            else:
                error = detail
        return RunResult(text=text, structured=structured, session_id=state.session_id, error=error,
                         usage=state.usage, warnings=warnings, completed_searches=state.completed_searches)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_object(text: str) -> Optional[dict]:
    """Best-effort: find a JSON object with a `prompt` key inside free text."""
    if not text:
        return None
    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    candidates.extend(m.group(1) for m in _JSON_FENCE.finditer(text))
    # brace-matching scan for the first top-level object
    start = text.find("{")
    while start != -1 and len(candidates) < 6:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
        start = text.find("{", start + 1)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and "prompt" in obj:
            return obj
    return None


def compact(value, limit: int = 240) -> str:
    """One-line preview of arbitrary JSON-ish data for the log."""
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False)
        except Exception:
            s = str(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "..."
