"""Runner tests with real (python) child processes. Run: python tests/test_runner.py"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))

from aipa import prompts, runner  # noqa: E402
from aipa.backends.base import Backend, LogEvent, RunSpec  # noqa: E402


class PyChildBackend(Backend):
    """Backend that runs an inline python child so runner behaviour can be tested offline."""

    name = "pychild"
    display = "PyChild"
    prefix = "py"
    cli_name = "python"

    def __init__(self, code: str):
        self.code = code

    def build_command(self, exe, spec):
        return [sys.executable, "-u", "-c", self.code]

    def stdin_payload(self, spec):
        return ""

    def parse_line(self, line, state):
        state.text_parts.append(line)
        return [LogEvent("text", line)]


def _spec() -> RunSpec:
    return RunSpec(
        user_message="", system_prompt="", model=None, effort=None, web_search=False, image_paths=[],
        schema=prompts.OUTPUT_SCHEMA, resume_id=None, workspace=Path(tempfile.mkdtemp()),
        run_dir=Path(tempfile.mkdtemp()),
    )


BUSY_CHILD = "\n".join([
    "import time",
    "for i in range(100000):",
    "    print(i, flush=True); time.sleep(0.01)",
])
SLEEPY_CHILD = "\n".join([
    "import time",
    "print('up', flush=True)",
    "time.sleep(30)",
    "print('never')",
])
FAILING_CHILD = "\n".join([
    "import sys",
    "sys.stderr.write('fatal: boom' + chr(10))",
    "sys.exit(3)",
])
RESULT_THEN_CRASH = "\n".join([
    "import sys",
    "print('{\"prompt\": \"kept\", \"negative_prompt\": \"\"}', flush=True)",
    "sys.exit(1)",
])


def test_timeout_fires_even_when_output_is_busy():
    b = PyChildBackend(BUSY_CHILD)
    events: list[LogEvent] = []
    t0 = time.time()
    try:
        runner.run_cli(b, sys.executable, _spec(), node_id="t-timeout", timeout_sec=1.5, on_event=events.append)
    except runner.RunTimeout:
        pass
    else:
        raise AssertionError("expected RunTimeout")
    assert time.time() - t0 < 6, "timeout must not wait for the child to finish"
    assert any(e.kind == "error" and "timed out" in e.text for e in events)
    assert runner.active_node_ids() == []


def test_cancel_kills_child():
    b = PyChildBackend(SLEEPY_CHILD)
    events: list[LogEvent] = []
    threading.Timer(0.8, lambda: runner.cancel("t-cancel")).start()
    t0 = time.time()
    try:
        runner.run_cli(b, sys.executable, _spec(), node_id="t-cancel", timeout_sec=60, on_event=events.append)
    except runner.RunCancelled:
        pass
    else:
        raise AssertionError("expected RunCancelled")
    assert time.time() - t0 < 10
    assert any(e.kind == "error" and "stopped by user" in e.text for e in events)
    assert runner.cancel("t-cancel") is False  # nothing left to cancel


def test_nonzero_exit_without_result_is_error():
    b = PyChildBackend(FAILING_CHILD)
    res = runner.run_cli(b, sys.executable, _spec(), node_id="t-exit", timeout_sec=30, on_event=lambda e: None)
    assert res.error and "code 3" in res.error and "boom" in res.error


def test_nonzero_exit_with_result_is_kept_as_warning():
    b = PyChildBackend(RESULT_THEN_CRASH)
    res = runner.run_cli(b, sys.executable, _spec(), node_id="t-keep", timeout_sec=30, on_event=lambda e: None)
    assert res.error is None
    assert res.structured == {"prompt": "kept", "negative_prompt": ""}
    assert res.warnings and "code 1" in res.warnings[0]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback

                print(f"FAIL {name}: {e}")
                traceback.print_exc()
    print("FAILED" if failed else "ALL PASSED", failed)
    sys.exit(1 if failed else 0)
