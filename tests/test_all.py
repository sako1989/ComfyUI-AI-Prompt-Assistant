"""Unit tests (no ComfyUI needed). Run: python tests/test_all.py  or  python -m pytest tests"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))

import numpy as np  # noqa: E402

from aipa import config, media, prompts  # noqa: E402
from aipa.backends import BACKENDS, model_options  # noqa: E402
from aipa.backends.base import ParseState, RunSpec, extract_json_object  # noqa: E402
from aipa.sessions import SessionStore  # noqa: E402


def _spec(**over) -> RunSpec:
    base = dict(
        user_message="hello", system_prompt="SYS", model=None, effort="high", web_search=True,
        image_paths=[], schema=prompts.OUTPUT_SCHEMA, resume_id=None,
        workspace=Path("C:/ws"), run_dir=Path(tempfile.mkdtemp()),
    )
    base.update(over)
    return RunSpec(**base)


# ------------------------------------------------------------------ backends: commands
def test_claude_command_new_session():
    b = BACKENDS["claude_code"]
    argv = b.build_command("claude.exe", _spec(model="opus", web_search=True))
    assert argv[:2] == ["claude.exe", "-p"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--tools") + 1] == "Read,WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "Read,WebSearch,WebFetch"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"
    assert "--system-prompt" in argv and "--resume" not in argv
    assert "--strict-mcp-config" in argv and "--safe-mode" in argv
    assert "--json-schema" in argv
    assert b.stdin_payload(_spec()) == "hello"


def test_claude_command_resume_and_no_search():
    b = BACKENDS["claude_code"]
    argv = b.build_command("claude", _spec(web_search=False, resume_id="abc-123"))
    assert argv[argv.index("--tools") + 1] == "Read"
    assert argv[argv.index("--resume") + 1] == "abc-123"
    assert "--system-prompt" not in argv
    assert "--model" not in argv and "--effort" in argv


def test_codex_command():
    b = BACKENDS["codex"]
    spec = _spec(model=None, image_paths=["C:/x/a.png", "C:/x/b.png"], effort="xhigh")
    argv = b.build_command("codex.exe", spec)
    assert argv[0] == "codex.exe" and argv[1] == "--search" and argv[2] == "exec"
    assert argv[-1] == "-"
    assert argv[argv.index("-m") + 1] == "gpt-5.5"  # explicit default, never the CLI default
    assert argv.count("-i") == 2
    assert '-c' in argv and 'model_reasoning_effort="xhigh"' in argv
    assert "--output-schema" in argv and Path(argv[argv.index("--output-schema") + 1]).exists()
    payload = b.stdin_payload(spec)
    assert payload.startswith("<instructions>\nSYS") and payload.endswith("hello")


def test_codex_command_resume_no_search():
    b = BACKENDS["codex"]
    argv = b.build_command("codex", _spec(web_search=False, resume_id="thread-1", schema=None))
    assert "--search" not in argv
    assert argv[1:3] == ["exec", "resume"]
    assert argv[-2:] == ["thread-1", "-"]
    assert "--output-schema" not in argv
    assert b.stdin_payload(_spec(resume_id="thread-1")) == "hello"


def test_resolve_model():
    c, x = BACKENDS["claude_code"], BACKENDS["codex"]
    assert c.resolve_model("claude/default", "") is None
    assert c.resolve_model("claude/opus", "") == "opus"
    assert c.resolve_model("codex/gpt-5.5", "") is None       # wrong prefix → backend default
    assert c.resolve_model("claude/opus", " my-model ") == "my-model"
    assert x.resolve_model("claude/opus", "") == "gpt-5.5"     # wrong prefix → codex default
    assert x.resolve_model("codex/gpt-5.6-sol", "") == "gpt-5.6-sol"
    opts = model_options()
    assert "claude/default" in opts and "codex/gpt-5.5" in opts


# ------------------------------------------------------------------ backends: parsers
CLAUDE_LINES = [
    '{"type":"system","subtype":"init","cwd":"C:\\\\ws","session_id":"sess-1","tools":["Read","WebSearch"],"model":"claude-opus-5"}',
    '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}',
    '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"Let me look at the image."}]}}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"C:/ws/runs/r/image_00.png"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result","content":[{"type":"image","source":{}}]}]}}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"WebSearch","input":{"query":"Wan 2.2 prompt guide"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result","content":[{"type":"text","text":"Web search results for query: ..."}]}]}}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"StructuredOutput","input":{"prompt":"A cat","negative_prompt":"blurry","notes":"n"}}]}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"{\\"prompt\\":\\"A cat\\",\\"negative_prompt\\":\\"blurry\\",\\"notes\\":\\"n\\"}","structured_output":{"prompt":"A cat","negative_prompt":"blurry","notes":"n"},"session_id":"sess-1","num_turns":4,"duration_ms":5300,"total_cost_usd":0.01,"usage":{"input_tokens":10,"output_tokens":50}}',
]


def test_claude_parser():
    b = BACKENDS["claude_code"]
    st = ParseState()
    kinds = []
    for line in CLAUDE_LINES:
        for ev in b.parse_line(line, st):
            kinds.append(ev.kind)
    assert st.session_id == "sess-1"
    assert kinds[0] == "status"
    assert "thinking" in kinds and "tool" in kinds and "search" in kinds and "tool_result" in kinds
    res = b.finalize(st, 0, "")
    assert res.error is None
    assert res.structured["prompt"] == "A cat" and res.structured["negative_prompt"] == "blurry"
    assert res.usage["num_turns"] == 4


def test_claude_parser_error_result():
    b = BACKENDS["claude_code"]
    st = ParseState()
    b.parse_line('{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Not logged in","session_id":"s","errors":["auth"]}', st)
    res = b.finalize(st, 1, "")
    assert res.error and "Claude Code error" in res.error


CODEX_LINES = [
    '{"type":"thread.started","thread_id":"01a0b174-e79f"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Exceeded skills context budget of 2%."}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"reasoning","text":"Thinking about lighting"}}',
    '{"type":"item.started","item":{"id":"item_2","type":"web_search","query":"LTX video prompt tips"}}',
    '{"type":"item.completed","item":{"id":"item_2","type":"web_search","query":"LTX video prompt tips"}}',
    '{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"{\\"prompt\\":\\"A dog\\",\\"negative_prompt\\":\\"\\",\\"notes\\":\\"ok\\"}"}}',
    '{"type":"turn.completed","usage":{"input_tokens":23176,"cached_input_tokens":1408,"output_tokens":5}}',
]


def test_codex_parser():
    b = BACKENDS["codex"]
    st = ParseState()
    kinds = []
    for line in CODEX_LINES:
        for ev in b.parse_line(line, st):
            kinds.append(ev.kind)
    assert st.session_id == "01a0b174-e79f"
    assert kinds.count("search") == 1  # started + completed with same id → one line
    assert "warn" in kinds and "thinking" in kinds and "text" in kinds
    res = b.finalize(st, 0, "")
    assert res.error is None
    assert res.structured == {"prompt": "A dog", "negative_prompt": "", "notes": "ok"}


def test_codex_parser_turn_failed():
    b = BACKENDS["codex"]
    st = ParseState()
    b.parse_line('{"type":"turn.failed","error":{"message":"{\\"type\\":\\"error\\",\\"status\\":400,\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"message\\":\\"The model requires a newer version of Codex.\\"}}"}}', st)
    res = b.finalize(st, 1, "")
    assert res.error == "Codex error: The model requires a newer version of Codex."


def test_finalize_keeps_result_on_nonzero_exit():
    b = BACKENDS["claude_code"]
    st = ParseState()
    for line in CLAUDE_LINES:
        b.parse_line(line, st)
    res = b.finalize(st, 1, "ERR_STREAM_WRITE_AFTER_END")
    assert res.error is None and res.structured["prompt"] == "A cat"
    assert res.warnings and "exited with code 1" in res.warnings[0]
    # but a genuine model-reported error still wins
    st2 = ParseState()
    b.parse_line('{"type":"result","subtype":"error","is_error":true,"result":"boom","session_id":"s"}', st2)
    assert b.finalize(st2, 1, "").error


def test_claude_parser_string_content_and_empty_structured():
    b = BACKENDS["claude_code"]
    st = ParseState()
    evs = b.parse_line('{"type":"assistant","message":{"content":"plain string answer"}}', st)
    assert [e.kind for e in evs] == ["text"] and st.text_parts == ["plain string answer"]
    b.parse_line('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"StructuredOutput","input":{"prompt":"P","negative_prompt":"N"}}]}}', st)
    b.parse_line('{"type":"result","subtype":"success","is_error":false,"result":"","structured_output":{},"session_id":"s"}', st)
    assert st.structured == {"prompt": "P", "negative_prompt": "N"}  # empty {} must not clobber it
    assert b.parse_line('{"type":"assistant","message":{"content":[null, 5, {"type":"text","text":"ok"}]}}', st)


def test_extract_json_object():
    assert extract_json_object('Here you go:\n```json\n{"prompt": "x", "negative_prompt": ""}\n```') == {"prompt": "x", "negative_prompt": ""}
    assert extract_json_object('noise {"a": 1} more {"prompt": "y"} tail')["prompt"] == "y"
    assert extract_json_object("plain text") is None
    assert extract_json_object('{"prompt": "with \\"quotes\\" and {braces}"}')["prompt"] == 'with "quotes" and {braces}'


def test_finalize_fallback_to_text():
    b = BACKENDS["codex"]
    st = ParseState()
    b.parse_line('{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"just a prompt, no json"}}', st)
    res = b.finalize(st, 0, "")
    assert res.structured is None and res.text == "just a prompt, no json"


# ------------------------------------------------------------------ media
def test_sample_indices():
    assert media.sample_indices(0, 5) == []
    assert media.sample_indices(3, 5) == [0, 1, 2]
    idx = media.sample_indices(100, 4)
    assert idx[0] == 0 and idx[-1] == 99 and len(idx) == 4


def test_image_batch_to_pngs_resizes():
    import torch

    batch = torch.rand(3, 300, 600, 3)
    d = Path(tempfile.mkdtemp())
    paths = media.image_batch_to_pngs(batch, d, max_frames=2, max_side=200)
    assert len(paths) == 2 and all(p.exists() for p in paths)
    from PIL import Image

    with Image.open(paths[0]) as im:
        assert im.size == (200, 100) and im.mode == "RGB"


def test_tensor_to_pil_channel_cases_and_alpha_flatten():
    import torch

    assert media.tensor_to_pil(torch.rand(8, 8, 1)).mode == "RGB"
    assert media.tensor_to_pil(torch.rand(8, 8, 2)).mode == "RGBA"
    assert media.tensor_to_pil(torch.rand(8, 8, 3)).mode == "RGB"
    assert media.tensor_to_pil(torch.rand(8, 8, 4)).mode == "RGBA"
    assert media.tensor_to_pil(torch.rand(8, 8, 6)).mode == "RGB"
    # fully transparent red must not survive as opaque red
    t = torch.zeros(4, 4, 4)
    t[..., 0] = 1.0  # red, alpha 0
    flat = media.flatten_alpha(media.tensor_to_pil(t))
    assert flat.mode == "RGB" and flat.getpixel((0, 0)) == (255, 255, 255)


def test_cleanup_runs_never_deletes_current_or_last():
    import shutil

    saved = config.RUNS_DIR
    tmp = Path(tempfile.mkdtemp()) / "runs"
    config.RUNS_DIR = tmp
    try:
        dirs = [tmp / f"2026-{i:02d}" for i in range(4)]
        for d in dirs:
            d.mkdir(parents=True)
        config.cleanup_runs(0, exclude=dirs[1])
        assert dirs[1].exists() and dirs[3].exists() and not dirs[0].exists() and not dirs[2].exists()
        config.cleanup_runs("garbage")  # bad config value -> default keep, nothing deleted
        assert dirs[1].exists() and dirs[3].exists()
    finally:
        config.RUNS_DIR = saved
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_video_stream_sampling_with_real_mp4():
    """File-backed video: frames are decoded lazily via av, never as a full tensor."""
    import av
    from fractions import Fraction

    d = Path(tempfile.mkdtemp())
    mp4 = d / "clip.mp4"
    with av.open(str(mp4), mode="w") as out:
        stream = out.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for i in range(30):
            arr = np.full((48, 64, 3), i * 8, dtype=np.uint8)  # brightness ramps with frame index
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for pkt in stream.encode(frame):
                out.mux(pkt)
        for pkt in stream.encode():
            out.mux(pkt)

    class FakeVideoFromFile:
        def get_stream_source(self):
            return str(mp4)

        def get_frame_count(self):
            return 30

        def get_frame_rate(self):
            return Fraction(10, 1)

        def get_dimensions(self):
            return (64, 48)

        def get_active_trim_window(self):
            return (0.0, 0.0)

        def get_components(self):
            raise AssertionError("must not materialize the whole video")

    paths, meta = media.video_to_frame_pngs(FakeVideoFromFile(), d, max_frames=4, max_side=512)
    assert len(paths) == 4 and meta["frame_count"] == 30 and meta["fps"] == 10.0
    assert meta["sampled_indices"] == [0, 10, 19, 29]
    from PIL import Image

    first = Image.open(paths[0]).getpixel((5, 5))[0]
    last = Image.open(paths[-1]).getpixel((5, 5))[0]
    assert last > first + 100  # later frames are brighter, so ordering/selection is right


def test_video_to_frames_with_fake_video():
    import torch
    from fractions import Fraction

    class Comps:
        images = torch.rand(24, 64, 96, 3)
        frame_rate = Fraction(24, 1)
        audio = None

    class FakeVideo:
        def get_components(self):
            return Comps()

    d = Path(tempfile.mkdtemp())
    paths, meta = media.video_to_frame_pngs(FakeVideo(), d, max_frames=4, max_side=512)
    assert len(paths) == 4
    assert meta["frame_count"] == 24 and meta["fps"] == 24.0 and meta["duration_sec"] == 1.0
    assert meta["sampled_times_sec"][-1] == 0.96


# ------------------------------------------------------------------ prompts / sessions / config
def test_prompts():
    sp = prompts.build_system_prompt("enhance", "wan_video", True, "claude_code", True)
    assert "Wan 2.1 / 2.2" in sp and "Read tool" in sp and "WEB SEARCH: enabled" in sp
    sp2 = prompts.build_system_prompt("chat", "auto", False, "codex", False)
    assert "attached to this message" in sp2 and "WEB SEARCH: disabled" in sp2
    um = prompts.build_user_message("a cat", "moody", "enhance", "flux", ["C:/a.png"], ["C:/f0.png"],
                                    {"frame_count": 10, "fps": 5, "duration_sec": 2, "width": 1, "height": 1, "sampled_times_sec": [0, 1.8]},
                                    resume=True)
    assert "Follow-up" in um and "C:/a.png" in um and "C:/f0.png" in um and "moody" in um
    assert set(prompts.TARGET_OPTIONS) >= {"auto", "flux", "sdxl", "wan_video", "custom"}


def test_session_store_roundtrip():
    p = Path(tempfile.mkdtemp()) / "sessions.json"
    s = SessionStore(p)
    assert s.get("7", "claude_code") is None
    s.set("7", "claude_code", "abc")
    s.set("7", "codex", "def")
    assert SessionStore(p).get("7", "claude_code") == "abc"
    s.clear("7", "codex")
    assert s.get("7", "codex") is None and s.get("7", "claude_code") == "abc"


def test_sanitized_env_strips_keys():
    os.environ["ANTHROPIC_API_KEY"] = "sk-test"
    os.environ["CLAUDECODE"] = "1"
    env = config.sanitized_env(True)
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDECODE" not in env and env["NO_COLOR"] == "1"
    assert "ANTHROPIC_API_KEY" in config.sanitized_env(False)


def test_find_cli_reports_search_paths():
    try:
        config.find_cli("nonexistent-cli-xyz", {})
    except config.CLINotFound as e:
        assert "config.json" in str(e)
    else:
        raise AssertionError("expected CLINotFound")



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
