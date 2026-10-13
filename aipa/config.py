"""Paths, CLI discovery and small config helpers (no ComfyUI imports)."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

PACK_DIR = Path(__file__).resolve().parents[1]  # aipa/ -> pack root
WORKSPACE_DIR = PACK_DIR / "workspace"
RUNS_DIR = WORKSPACE_DIR / "runs"
SESSIONS_PATH = WORKSPACE_DIR / "sessions.json"
CONFIG_PATH = PACK_DIR / "config.json"

EVENT_NAME = "ai_prompt_assistant.log"

# Environment variables that would silently switch a CLI from subscription auth
# to API-key billing, or that mark "we are already inside Claude Code".
STRIP_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)


class CLINotFound(RuntimeError):
    pass


def load_config() -> dict:
    """Optional user overrides: {"claude_path": "...", "codex_path": "...", "keep_runs": 20,
    "strip_api_keys": true, "extra_claude_args": [], "extra_codex_args": []}"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _candidate_paths(name: str) -> list[Path]:
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    if name == "claude":
        return [
            home / ".local" / "bin" / "claude.exe",
            home / ".local" / "bin" / "claude",
            roaming / "npm" / "claude.cmd",
            roaming / "npm" / "claude",
            local / "Programs" / "claude" / "claude.exe",
            Path("/usr/local/bin/claude"),
            home / ".npm-global" / "bin" / "claude",
        ]
    if name == "codex":
        return [
            local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe",
            roaming / "npm" / "codex.cmd",
            roaming / "npm" / "codex",
            home / ".local" / "bin" / "codex.exe",
            home / ".local" / "bin" / "codex",
            Path("/usr/local/bin/codex"),
            home / ".npm-global" / "bin" / "codex",
        ]
    return []


def find_cli(name: str, config: dict | None = None) -> str:
    """Return an executable path for `claude` or `codex`, or raise CLINotFound."""
    config = config if config is not None else load_config()
    override = config.get(f"{name}_path")
    searched: list[str] = []
    if override:
        p = Path(str(override)).expanduser()
        if p.is_file():
            return str(p)
        searched.append(f"{p} (config.json override)")
    found = shutil.which(name)
    if found:
        return found
    searched.append(f"{name} on PATH")
    for cand in _candidate_paths(name):
        if cand.is_file():
            return str(cand)
        searched.append(str(cand))
    raise CLINotFound(
        f"Could not find the `{name}` CLI. Searched: " + "; ".join(searched)
        + f". Install it, or set \"{name}_path\" in {CONFIG_PATH}."
    )


def ensure_workspace() -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_DIR


def new_run_dir() -> Path:
    ensure_workspace()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = RUNS_DIR / f"{stamp}-{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def cleanup_runs(keep: int = 20, exclude: Path | None = None) -> None:
    """Delete old run directories, keeping the newest `keep` (at least 1) and never `exclude`."""
    try:
        keep = max(1, int(keep))
    except Exception:
        keep = 20
    try:
        dirs = sorted((p for p in RUNS_DIR.iterdir() if p.is_dir()), key=lambda p: p.name)
    except FileNotFoundError:
        return
    excl = Path(exclude).resolve() if exclude else None
    for old in dirs[:-keep]:
        if excl is not None and old.resolve() == excl:
            continue
        shutil.rmtree(old, ignore_errors=True)


def sanitized_env(strip_api_keys: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    if strip_api_keys:
        for k in STRIP_ENV_VARS:
            env.pop(k, None)
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env
