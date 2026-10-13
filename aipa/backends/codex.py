"""OpenAI Codex CLI adapter: `codex [--search] exec --json` / `codex exec resume`."""

from __future__ import annotations

import json
from pathlib import Path

from .base import Backend, LogEvent, ParseState, RunSpec, compact


class CodexBackend(Backend):
    name = "codex"
    display = "Codex"
    prefix = "codex"
    cli_name = "codex"
    models = [
        "gpt-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6-astra",
    ]
    # The user's config.toml default may be newer than the installed CLI accepts,
    # so always pass an explicit model.
    default_model = "gpt-5.5"

    def _schema_file(self, spec: RunSpec) -> str | None:
        if not spec.schema:
            return None
        p = Path(spec.run_dir) / "output_schema.json"
        p.write_text(json.dumps(spec.schema, indent=2), encoding="utf-8")
        return str(p)

    def build_command(self, exe: str, spec: RunSpec) -> list[str]:
        argv = [exe]
        if spec.web_search:
            argv.append("--search")
        argv += ["exec"]
        if spec.resume_id:
            argv += ["resume"]
        argv += [
            "--json",
            "--skip-git-repo-check",
            "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="read-only"',
            "-c", "mcp_servers={}",
        ]
        model = spec.model or self.default_model
        if model:
            argv += ["-m", model]
        if spec.effort:
            argv += ["-c", f'model_reasoning_effort="{spec.effort}"']
        for img in spec.image_paths:
            argv += ["-i", img]
        schema_file = self._schema_file(spec)
        if schema_file:
            argv += ["--output-schema", schema_file]
        argv += list(spec.extra_args or [])
        # Set this even when off: otherwise the user's global config may enable cached search.
        argv += ["-c", 'web_search="live"' if spec.web_search else 'web_search="disabled"']
        if spec.resume_id:
            argv += [spec.resume_id]
        argv += ["-"]  # read the prompt from stdin
        return argv

    def stdin_payload(self, spec: RunSpec) -> str:
        # Codex has no system-prompt flag in exec mode; prepend the instructions.
        if spec.resume_id:
            return spec.user_message
        return f"<instructions>\n{spec.system_prompt}\n</instructions>\n\n{spec.user_message}"

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
        t = ev.get("type") or ""
        out: list[LogEvent] = []

        if t == "thread.started":
            state.session_id = ev.get("thread_id") or state.session_id
            out.append(LogEvent("status", f"Codex thread started | {state.session_id}"))
            return out
        if t == "turn.started":
            out.append(LogEvent("status", "Codex is working..."))
            return out
        if t == "turn.completed":
            usage = ev.get("usage") or {}
            state.usage = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read": usage.get("cached_input_tokens"),
                "reasoning_tokens": usage.get("reasoning_output_tokens"),
            }
            out.append(LogEvent("status", f"Turn completed | in {usage.get('input_tokens', '?')} / out {usage.get('output_tokens', '?')} tok"))
            return out
        if t == "turn.failed":
            err = ev.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            state.error = f"Codex error: {compact(_unwrap_error(msg), 600)}"
            state.is_error = True
            out.append(LogEvent("error", state.error))
            return out
        if t == "error":
            msg = ev.get("message") or compact(ev)
            state.error = f"Codex error: {compact(_unwrap_error(msg), 600)}"
            state.is_error = True
            out.append(LogEvent("error", state.error))
            return out

        if t.startswith("item."):
            item = ev.get("item") or {}
            it = item.get("type") or ""
            item_id = item.get("id")
            completed = t == "item.completed"
            if it == "agent_message":
                text = (item.get("text") or "").strip()
                if completed and text:
                    state.final_text = text
                    state.text_parts.append(text)
                    out.append(LogEvent("text", text))
            elif it == "reasoning":
                text = (item.get("text") or item.get("summary") or "").strip()
                if completed and text:
                    out.append(LogEvent("thinking", text))
            elif it == "web_search":
                action = item.get("action") or {}
                # CLI 0.144 emits item.completed without a status member. Newer
                # streams may include one; explicit failures never count.
                is_search = isinstance(action, dict) and (
                    action.get("type") == "search" or (not action and bool(item.get("query"))))
                if (completed and item.get("status", "completed") == "completed" and not item.get("error")
                        and is_search
                        and (item_id or line) not in state.pending_searches):
                    state.pending_searches.add(item_id or line)
                    state.completed_searches += 1
                q = _search_query(item)
                key = item_id or f"ws-{len(state.seen_ids)}"
                if t == "item.started":
                    if q and key not in state.seen_ids:
                        state.seen_ids.add(key)
                        out.append(LogEvent("search", f"Web search: {q}"))
                elif completed and key not in state.seen_ids:
                    state.seen_ids.add(key)
                    out.append(LogEvent("search", f"Web search: {q or '(query not reported by Codex)'}"))
            elif it == "command_execution":
                cmd = item.get("command") or ""
                if t == "item.started" or (completed and item_id not in state.seen_ids):
                    if item_id:
                        state.seen_ids.add(item_id)
                    out.append(LogEvent("tool", f"Command: {compact(cmd, 200)}"))
                if completed:
                    output = item.get("aggregated_output") or ""
                    if output:
                        out.append(LogEvent("tool_result", compact(output, 240)))
            elif it == "error":
                msg = item.get("message") or compact(item)
                out.append(LogEvent("warn", compact(msg, 400)))
            elif it in ("mcp_tool_call", "file_change", "todo_list", "plan"):
                if completed:
                    out.append(LogEvent("tool", f"{it}: {compact(item, 200)}"))
            return out

        return out


def _search_query(item: dict) -> str:
    q = item.get("query")
    if isinstance(q, str) and q.strip():
        return q.strip()
    action = item.get("action") or {}
    if isinstance(action, dict):
        aq = action.get("query")
        if isinstance(aq, str) and aq.strip():
            return aq.strip()
        qs = action.get("queries")
        if isinstance(qs, list) and qs:
            return "; ".join(str(x) for x in qs if x)
        url = action.get("url")
        if isinstance(url, str) and url:
            return f"open {url}"
    return ""


def _unwrap_error(msg) -> str:
    """Codex sometimes nests a JSON error body inside the message string."""
    if not isinstance(msg, str):
        return compact(msg)
    s = msg.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            inner = obj.get("error") if isinstance(obj, dict) else None
            if isinstance(inner, dict) and inner.get("message"):
                return str(inner["message"])
            if isinstance(obj, dict) and obj.get("message"):
                return str(obj["message"])
        except Exception:
            pass
    return s
