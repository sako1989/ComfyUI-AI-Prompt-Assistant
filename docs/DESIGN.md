# AI Prompt Assistant — design spec (2026-09-18)

## Research and skills update (2026-09-23)

`aipa/research.py` now orchestrates a text-only subject-identification call, exact local
skill lookup, isolated web-research calls on misses, and final generation with web tools
disabled. Research acceptance requires parser-observed successful search completion plus
verified structured output and source URLs. There is one retry and one total timeout budget.
`generation_model` explicitly adds a mandatory subject independent of AI extraction.

`aipa/skills.py` loads manual Markdown files and atomically saves researched `SKILL.md`
files under the pack's `skills/` directory. Both backends receive the same bounded text
context. Manual skills can provide exact aliases; generic family names never cover new
version numbers. Only final generation sessions are persisted; prior subjects are kept
for follow-up extraction. Current policy and skills are reinjected on resumed turns.

The appended inputs are `generation_model`, `skill`, `skill_action`, `skill_topic`.
`GET /ai_prompt_assistant/skills` and the Refresh skills list button update dropdown
options without restarting. Content fingerprints invalidate ComfyUI caches after edits.
Generated skills never expire automatically; explicit refresh avoids unexpected repeat
research. Manual files are never overwritten. See `skills/README.md` for the format.

The original architecture and environment observations below describe the initial version.

## Goal

A ComfyUI node that acts as a prompt-engineering assistant. It takes the user's rough idea or
existing prompt plus optional reference media (IMAGE, VIDEO) and returns a polished generation
prompt. The reasoning is done by the user's **own CLI coding agents** — Claude Code or Codex —
running as a background subprocess, so it bills the user's subscription and never needs an API key.

## Non-goals

- No direct Anthropic/OpenAI API calls, no API keys, no local LLMs.
- No editing of the ComfyUI graph (the agent only reads media files and searches the web).
- No multi-node "chat panel". One node, one run per queue.

## Reference environment (what the first version was built against)

| Item | Value |
|---|---|
| Claude Code | 2.1.274, native Windows install, logged in via claude.ai |
| Codex | 0.144.0, native Windows install, logged in via ChatGPT |
| ComfyUI | 0.36.0, frontend 1.52.7, `comfy_api.latest` V3 schema, native `VIDEO` type |
| Claude flags | `-p --output-format stream-json --verbose --model --effort --json-schema --system-prompt --tools --allowedTools --permission-mode dontAsk --strict-mcp-config --safe-mode --setting-sources "" --resume` |
| Codex flags | `codex --search exec --json --skip-git-repo-check -c sandbox_mode="read-only" -m <model> -i <img> --output-schema <file> -c key=value` and `codex exec resume <thread_id> -` |
| Gotchas | When `ANTHROPIC_API_KEY` is set in the shell it must be stripped from the child env, or Claude bills the API instead of the subscription. A Codex `config.toml` default model newer than the installed CLI is rejected → always pass `-m` explicitly. Codex `--search` is a top-level flag, not an `exec` flag. Both CLIs read stdin; feed the prompt through stdin. |

## Architecture

```
ComfyUI graph ──► AIPromptAssistant.execute()
                    │  1. media.py: IMAGE/VIDEO tensors → PNG files in workspace/runs/<id>/
                    │  2. prompts.py: system prompt + user message + JSON schema
                    │  3. backends/*.py: build argv + env for the chosen CLI
                    │  4. runner.py: Popen, stream stdout lines, parse → LogEvent
                    │        └─► PromptServer.send_sync("ai_prompt_assistant.log", …)  ──► web/ai_prompt_assistant.js (log widget)
                    │  5. parse structured result → (prompt, negative_prompt, notes, log)
                    │  6. sessions.py: remember session/thread id per node for "continue"
                    └─► outputs
```

### Modules

Core code lives in the `aipa/` subpackage (unique name + relative imports, so nothing collides with
other packs' top-level `config.py`/`prompts.py` in ComfyUI's shared interpreter). `aipa/` has no
ComfyUI imports (only an optional `comfy.model_management` probe), so it is unit-testable standalone.

- `__init__.py` — `comfy_entrypoint` (async), `WEB_DIRECTORY`, registers HTTP routes.
- `nodes.py` — the V3 node (`io.ComfyNode`), schema, execute.
- `server_routes.py` — `POST /ai_prompt_assistant/cancel`, `GET /ai_prompt_assistant/status`.
- `aipa/backends/base.py` — `Backend` protocol: `resolve_model`, `build_command`, `stdin_payload`,
  `parse_line`, `finalize`; `extract_json_object` fallback parser.
- `aipa/backends/claude_code.py` — Claude Code `stream-json` parser and argv builder.
- `aipa/backends/codex.py` — Codex JSONL parser and argv builder.
- `aipa/runner.py` — subprocess lifecycle: sanitized env, stdin prompt, line streaming, cancellation
  (ComfyUI interrupt + HTTP cancel route), timeout, Windows tree kill. `python -m aipa.runner` is a
  manual harness.
- `aipa/media.py` — tensor → PNG, video → evenly-sampled frames, longest-side resize.
- `aipa/prompts.py` — system prompt per task/target, user-message assembly, output JSON schema.
- `aipa/sessions.py` — per-node session ids persisted to `workspace/sessions.json`.
- `aipa/config.py` — CLI path discovery (`shutil.which` + known install paths + `config.json`
  override), workspace paths, run-dir cleanup, env sanitizing.
- `web/ai_prompt_assistant.js` — log console widget, model filter by backend, Stop button.

### Node interface

Inputs (widgets unless noted):

| id | type | notes |
|---|---|---|
| `text` | STRING multiline | idea / draft prompt / question |
| `backend` | COMBO `claude_code`, `codex` | |
| `model` | COMBO, prefixed `claude/…`, `codex/…` | JS hides options of the other backend |
| `custom_model` | STRING | overrides `model` when non-empty |
| `effort` | COMBO `default, low, medium, high, xhigh, max` | Claude `--effort`, Codex `model_reasoning_effort` |
| `task` | COMBO `enhance`, `describe_media`, `chat` | |
| `target` | COMBO of generator families | shapes prompt style/length |
| `instructions` | STRING multiline | extra guidance |
| `web_search` | BOOLEAN | allows WebSearch/WebFetch (Claude) / `--search` (Codex) |
| `session` | COMBO `new`, `continue` | continue = resume this node's last conversation |
| `image` | IMAGE (optional socket) | batch → up to `max_frames` images |
| `video` | VIDEO (optional socket) | sampled to `max_frames` frames |
| `max_frames` | INT 1–16, default 6 | |
| `max_image_side` | INT 256–2048, default 1024 | |
| `timeout_sec` | INT 30–3600, default 600 | |
| `seed` | INT, control_after_generate | cache control only, never sent to the model |

Outputs: `prompt`, `negative_prompt`, `notes`, `log` (all STRING).

### Log streaming

Server → client event `ai_prompt_assistant.log` with `{node_id, kind, text}` where `kind` is
`status | thinking | text | tool | tool_result | search | error | result | done`. The JS widget
clears on `status:started`, appends color-coded lines, autoscrolls, and shows a Stop button
that POSTs `/ai_prompt_assistant/cancel` with the node id.

### Run-alone button (added 2026-09-18, frontend only)

Goal: run the assistant by itself without executing the whole workflow, **without** turning it into
an output node (that would make an unconnected assistant run on every full queue).

ComfyUI's partial execution (`partial_execution_targets`) only accepts OUTPUT nodes as targets. So the
▶ Run button takes `app.graphToPrompt()`, adds a temporary `PreviewAny` sink (id ≥ 900000, never on
the canvas or in the saved workflow) wired to the assistant's `prompt` output, and calls
`api.queuePrompt(0, prompt, { partialExecutionTargets: [sinkId] })`. The full graph is still sent, so
the backend keeps every other node's cache; only the assistant and its upstream nodes execute.
Subgraph instances are resolved through the flattened execution id (`"12:34"`).

**Native blue play button.** The selection toolbox only offers "Execute to selected output nodes" for
nodes whose *client-side* def has `output_node`. `beforeRegisterNodeDef` sets `nodeData.output_node =
true` on the raw def (the frontend builds its `ComfyNodeDefImpl` from it afterwards), which also gives
the native orange highlight. The server def is untouched (`/object_info` still reports
`output_node: false`), so an unconnected assistant is still skipped by full queues. The native command
ends in `api.queuePrompt(n, prompt, {partialExecutionTargets})`; `patchQueuePrompt()` wraps that once
and replaces any target whose `class_type` is `AIPromptAssistant` with a temporary sink, exactly like
the ▶ Run button. Other targets selected alongside it pass through unchanged.

**Widget width guard.** Selecting a node makes the properties side panel draw unknown widget types
through a legacy renderer that assigns `widget.width` (panel width) and never resets it; the DOM
widget container prefers `widget.width` over the node width, so the log stayed shrunk. The log
widget defines `width` as a no-op accessor so it always follows the node.

Seed semantics mirror the frontend's own partial execution: a plain Run does not advance the seed, so
a following full queue hits the cache and reuses the same prompt. Shift+click rolls a new seed first.
If a Run is served from cache the log says so (`execution_cached`, scoped to runs started by the
button so normal queues behave exactly as before).

### Safety / sandboxing of the agent

- Claude: `--tools Read,WebSearch,WebFetch` (WebFetch only when web_search) + `--allowedTools` the same
  + `--permission-mode dontAsk` + `--strict-mcp-config --safe-mode --setting-sources ""`.
  cwd = pack `workspace/` so `Read` of the run's media needs no permission. No Bash/Edit/Write.
- Codex: `-s read-only`, `-c approval_policy="never"`, `-c mcp_servers={}`, `--skip-git-repo-check`,
  cwd = pack `workspace/`.
- Env: strip `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`, `CLAUDECODE`,
  `CLAUDE_CODE_ENTRYPOINT`; set `NO_COLOR=1`.

### Result parsing

Preferred: Claude `result.structured_output` / Codex final `agent_message` JSON conforming to
`{prompt, negative_prompt, notes}`. Fallback: first JSON object found in the text; last fallback:
whole text → `prompt`.

### Error handling

- CLI not found → clear error naming the searched paths and the `config.json` override.
- Non-zero exit / `is_error` / `turn.failed` → raise with the CLI's message (also streamed to the log).
- Timeout or ComfyUI interrupt or Stop button → kill process tree, raise.
- Media conversion failures → raise before launching the CLI.

### Testing

- `tests/test_all.py` — recorded stream-json / JSONL fixtures → LogEvents + result, argv/env
  construction for both backends, tensor→PNG shape/resize, frame sampling, prompts, sessions.
- `tests/test_runner.py` — subprocess lifecycle against real child processes (stream, cancel, timeout).
- `tests/test_skills.py` — skill loading/matching/saving, research orchestration, usage counting.
- Manual: run the standalone runner from the venv against both CLIs with an image; then load the
  node in ComfyUI, check `/object_info/AIPromptAssistant`, run a workflow, watch the log widget.
