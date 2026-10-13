"""Subprocess runner: streams a CLI's stdout line by line, supports cancel/timeout."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from typing import Callable, Optional

from .backends.base import Backend, LogEvent, ParseState, RunResult, RunSpec
from .config import sanitized_env

try:  # ComfyUI's interrupt flag; absent when running the module standalone
    from comfy import model_management as _mm  # type: ignore

    def _interrupted() -> bool:
        return bool(_mm.processing_interrupted())
except Exception:  # pragma: no cover
    def _interrupted() -> bool:
        return False


class RunCancelled(RuntimeError):
    pass


class RunTimeout(RuntimeError):
    pass


_ACTIVE: dict[str, subprocess.Popen] = {}
_CANCEL_REQUESTED: set[str] = set()
_LOCK = threading.Lock()
_CHECK_INTERVAL = 0.25  # seconds between interrupt/timeout checks while output is flowing


def cancel(node_id: str) -> bool:
    """Kill the process associated with `node_id` (used by the HTTP cancel route)."""
    node_id = str(node_id)
    with _LOCK:
        proc = _ACTIVE.get(node_id)
        if proc is not None:
            _CANCEL_REQUESTED.add(node_id)
    if proc is None:
        return False
    _kill_tree(proc)
    return True


def active_node_ids() -> list[str]:
    with _LOCK:
        return list(_ACTIVE.keys())


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
        else:
            # the child was started in its own session (start_new_session=True), so the
            # process group id equals its pid and killpg takes the grandchildren with it
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _pump(stream, q: "queue.Queue[tuple[str, Optional[bytes]]]", tag: str) -> None:
    try:
        for raw in iter(stream.readline, b""):
            q.put((tag, raw))
    except Exception:
        pass
    finally:
        q.put((tag, None))


def run_cli(
    backend: Backend,
    exe: str,
    spec: RunSpec,
    *,
    node_id: str,
    timeout_sec: float,
    on_event: Callable[[LogEvent], None],
    strip_api_keys: bool = True,
) -> RunResult:
    node_id = str(node_id)
    argv = backend.build_command(exe, spec)
    payload = backend.stdin_payload(spec)
    env = sanitized_env(strip_api_keys)
    state = ParseState()

    on_event(LogEvent("status", f"Launching {backend.display}: {_pretty_argv(argv)}"))

    popen_kwargs: dict = dict(
        cwd=str(spec.workspace),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except FileNotFoundError as e:
        raise RuntimeError(f"Failed to start {backend.display} ({exe}): {e}") from e

    with _LOCK:
        _CANCEL_REQUESTED.discard(node_id)
        _ACTIVE[node_id] = proc

    q: "queue.Queue[tuple[str, Optional[bytes]]]" = queue.Queue()
    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=_pump, args=(stream, q, tag), daemon=True).start()

    def _feed_stdin():
        try:
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    threading.Thread(target=_feed_stdin, daemon=True).start()

    stderr_lines: list[str] = []
    open_streams = 2
    started = time.monotonic()
    last_check = started
    exited_at: Optional[float] = None
    abort_reason: Optional[str] = None

    def _check_abort() -> Optional[str]:
        if _interrupted():
            return "interrupted by ComfyUI"
        if timeout_sec and (time.monotonic() - started) > timeout_sec:
            return f"timed out after {int(timeout_sec)}s"
        return None

    try:
        while open_streams > 0:
            now = time.monotonic()
            if now - last_check >= _CHECK_INTERVAL:
                last_check = now
                abort_reason = _check_abort()
                if abort_reason:
                    break
            try:
                tag, raw = q.get(timeout=_CHECK_INTERVAL)
            except queue.Empty:
                if proc.poll() is not None:
                    # process is gone; allow the pumps a grace period to deliver the tail + sentinels
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at > 3.0:
                        break
                continue
            if raw is None:
                open_streams -= 1
                continue
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if tag == "err":
                if line.strip():
                    stderr_lines.append(line)
                    low = line.lower()
                    if "error" in low and "models cache" not in low:
                        on_event(LogEvent("warn", f"stderr: {line.strip()[:400]}"))
                continue
            try:
                events = backend.parse_line(line, state)
            except Exception as e:  # never let a parser bug kill the run
                events = [LogEvent("warn", f"parse error: {e}")]
            for ev in events:
                on_event(ev)
    finally:
        if abort_reason:
            _kill_tree(proc)
        try:
            proc.wait(timeout=15)
        except Exception:
            _kill_tree(proc)
        with _LOCK:
            _ACTIVE.pop(node_id, None)
            was_cancelled = node_id in _CANCEL_REQUESTED
            _CANCEL_REQUESTED.discard(node_id)

    if abort_reason:
        on_event(LogEvent("error", f"Run {abort_reason}"))
        if abort_reason.startswith("timed out"):
            raise RunTimeout(f"{backend.display} {abort_reason}")
        raise RunCancelled(f"{backend.display} run {abort_reason}")
    if was_cancelled:
        on_event(LogEvent("error", "Run stopped by user"))
        raise RunCancelled(f"{backend.display} run stopped by user")

    exit_code = proc.returncode if proc.returncode is not None else -1
    return backend.finalize(state, exit_code, "\n".join(stderr_lines[-20:]))


def _pretty_argv(argv: list[str]) -> str:
    shown: list[str] = []
    skip_next = False
    for i, a in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if a in ("--system-prompt", "--json-schema"):
            shown.append(a + " <...>")
            skip_next = True
            continue
        if i == 0:
            shown.append(os.path.basename(a))
            continue
        shown.append(a if len(a) < 80 else a[:77] + "...")
    return " ".join(shown)


def main(argv: list[str]) -> int:
    """Manual harness:
    python -m aipa.runner [claude_code|codex] "text" [image.png ...] [--task enhance|describe_media|chat]
                          [--resume SESSION_ID] [--no-search] [--effort low] [--model id]
    """
    from . import prompts
    from .research import run_with_skills
    from .backends import BACKENDS
    from .config import ensure_workspace, find_cli, new_run_dir

    opts = {"task": "enhance", "resume": None, "search": True, "effort": "low", "model": None}
    positional: list[str] = []
    it = iter(argv[1:])
    for a in it:
        if a == "--task":
            opts["task"] = next(it)
        elif a == "--resume":
            opts["resume"] = next(it)
        elif a == "--no-search":
            opts["search"] = False
        elif a == "--effort":
            opts["effort"] = next(it)
        elif a == "--model":
            opts["model"] = next(it)
        else:
            positional.append(a)
    bname = positional[0] if positional else "claude_code"
    text = positional[1] if len(positional) > 1 else "a cat on a windowsill"
    images = [os.path.abspath(p) for p in positional[2:]]
    b = BACKENDS[bname]
    ws = ensure_workspace()
    rd = new_run_dir()
    spec = RunSpec(
        user_message=prompts.build_user_message(text, "", opts["task"], "auto", images, [], None,
                                                resume=bool(opts["resume"])),
        system_prompt=prompts.build_system_prompt(opts["task"], "auto", False, bname, bool(images)),
        model=opts["model"] or (None if bname == "claude_code" else b.default_model),
        effort=opts["effort"], web_search=opts["search"], image_paths=images, schema=prompts.OUTPUT_SCHEMA,
        resume_id=opts["resume"], workspace=ws, run_dir=rd,
    )
    res, _ = run_with_skills(b, find_cli(b.cli_name), spec, node_id="cli", timeout_sec=300,
                  on_event=lambda e: print(f"[{e.kind}] {e.text[:400]}", flush=True))
    print("RESULT:", res)
    return 0 if not res.error else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
