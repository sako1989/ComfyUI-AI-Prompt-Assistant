"""Claude Code CLI adapter: `claude -p --output-format stream-json`."""

from __future__ import annotations

import json

from .base import Backend, LogEvent, ParseState, RunSpec, compact


class ClaudeCodeBackend(Backend):
    name = "claude_code"
    display = "Claude Code"
    prefix = "claude"
    cli_name = "claude"
    models = [
        "default",
        "fable",
        "opus",
        "sonnet",
        "haiku",
        "claude-fable-5-1",
        "claude-opus-5",
        "claude-sonnet-5",
    ]
    default_model = None  # let the CLI pick the user's configured model

    def build_command(self, exe: str, spec: RunSpec) -> list[str]:
        tools = ["Read"]
        # Read is deliberately not pre-approved: a blanket allow rule lets it open any file,
        # while dontAsk without one only permits reads inside the cwd (workspace/) and --add-dir.
        allowed: list[str] = []
        if spec.web_search:
            tools += ["WebSearch", "WebFetch"]
            allowed += ["WebSearch", "WebFetch"]
        argv = [
            exe,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--safe-mode",
            "--setting-sources", "",
            "--tools", ",".join(tools),
            "--add-dir", str(spec.run_dir),
        ]
        if allowed:
            argv += ["--allowedTools", ",".join(allowed)]
        if spec.resume_id:
            argv += ["--resume", spec.resume_id]
        else:
            argv += ["--system-prompt", spec.system_prompt]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if spec.schema:
            argv += ["--json-schema", json.dumps(spec.schema, separators=(",", ":"))]
        argv += list(spec.extra_args or [])
        return argv

    def stdin_payload(self, spec: RunSpec) -> str:
        return spec.user_message

    # ------------------------------------------------------------------ parsing
    def parse_line(self, line: str, state: ParseState) -> list[LogEvent]:
        line = line.strip()
        if not line:
            return []
        try:
            ev = json.loads(line)
        except Exception:
            return [LogEvent("text", line)] if not line.startswith("{") else []
        if not isinstance(ev, dict):
            return []
        t = ev.get("type")
        out: list[LogEvent] = []

        if t == "system":
            sub = ev.get("subtype")
            if sub == "init":
                state.session_id = ev.get("session_id") or state.session_id
                model = ev.get("model") or "?"
                tools = ev.get("tools") or []
                out.append(LogEvent("status", f"Claude Code session started | model {model} | tools {', '.join(tools) if tools else 'none'}"))
            elif sub in ("thinking_tokens",):
                pass
            elif sub:
                msg = ev.get("message") or ev.get("content") or ""
                if msg:
                    out.append(LogEvent("status", f"{sub}: {compact(msg)}"))
            return out

        if t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            status = info.get("status")
            if status and status != "allowed":
                out.append(LogEvent("warn", f"Rate limit status: {status} ({compact(info)})"))
            return out

        if t == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                return out
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "thinking":
                    th = (block.get("thinking") or "").strip()
                    if th:
                        out.append(LogEvent("thinking", th))
                elif bt == "text":
                    tx = (block.get("text") or "").strip()
                    if tx:
                        state.text_parts.append(tx)
                        out.append(LogEvent("text", tx))
                elif bt == "tool_use":
                    name = block.get("name") or "tool"
                    inp = block.get("input") or {}
                    if name == "StructuredOutput":
                        out.append(LogEvent("status", "Formatting final answer..."))
                        if isinstance(inp, dict) and "prompt" in inp:
                            state.structured = state.structured or inp
                    elif name == "WebSearch":
                        if block.get("id"):
                            state.pending_searches.add(block["id"])
                        out.append(LogEvent("search", f"Web search: {inp.get('query', compact(inp))}"))
                    elif name == "WebFetch":
                        out.append(LogEvent("search", f"Fetch: {inp.get('url', compact(inp))}"))
                    elif name == "Read":
                        out.append(LogEvent("tool", f"Read {inp.get('file_path', compact(inp))}"))
                    else:
                        out.append(LogEvent("tool", f"{name} {compact(inp)}"))
            return out

        if t == "user":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        c = block.get("content")
                        tool_id = block.get("tool_use_id")
                        if tool_id in state.pending_searches:
                            state.pending_searches.discard(tool_id)
                            if not block.get("is_error") and c:
                                state.completed_searches += 1
                        if isinstance(c, list):
                            texts = [str(x.get("text", "")) for x in c if isinstance(x, dict) and x.get("type") == "text"]
                            has_img = any(isinstance(x, dict) and x.get("type") == "image" for x in c)
                            preview = compact(" ".join(texts), 200)
                            if has_img:
                                preview = ("[image] " + preview).strip()
                        else:
                            preview = compact(c, 200)
                        if block.get("is_error"):
                            out.append(LogEvent("warn", f"Tool error: {preview}"))
                        elif preview:
                            out.append(LogEvent("tool_result", preview))
            return out

        if t == "result":
            state.session_id = ev.get("session_id") or state.session_id
            state.is_error = bool(ev.get("is_error"))
            res = ev.get("result")
            if isinstance(res, str) and res.strip():
                state.final_text = res
            so = ev.get("structured_output")
            if isinstance(so, dict) and so.get("prompt") is not None:
                state.structured = so
            errs = ev.get("errors")
            if state.is_error:
                detail = compact(errs) if errs else (state.final_text or ev.get("subtype") or "unknown error")
                state.error = f"Claude Code error: {detail}"
                out.append(LogEvent("error", state.error))
            elif errs:
                out.append(LogEvent("warn", compact(errs)))
            denials = ev.get("permission_denials") or []
            if denials:
                out.append(LogEvent("warn", f"{len(denials)} tool call(s) denied by permission policy: " + compact([d.get('tool_name') for d in denials if isinstance(d, dict)])))
            usage = ev.get("usage") or {}
            state.usage = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read": usage.get("cache_read_input_tokens"),
                "cache_creation": usage.get("cache_creation_input_tokens"),
                "num_turns": ev.get("num_turns"),
                "duration_ms": ev.get("duration_ms"),
                "cost_usd": ev.get("total_cost_usd"),
            }
            dur = ev.get("duration_ms")
            secs = f"{dur / 1000:.1f}s" if isinstance(dur, (int, float)) else "?"
            out.append(LogEvent("status", f"Finished in {secs} | {ev.get('num_turns', '?')} turn(s) | out {usage.get('output_tokens', '?')} tok"))
            return out

        # stream_event (partial messages) and anything else: ignore quietly
        return out
