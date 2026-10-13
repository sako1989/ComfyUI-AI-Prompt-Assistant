# Changelog

## 0.1.1

- Security: the Claude Code backend pre-approved `Read` for every path, so the agent could open
  files outside the node's folder. `Read` is now limited to the pack's `workspace/` folder.
- Docs: corrected the description of the Codex sandbox (read-only, but it can read local files).

## 0.1.0

First public release.

- `AI Prompt Assistant (Claude Code / Codex)` node: text, optional `IMAGE` and `VIDEO` in;
  `prompt`, `negative_prompt`, `notes` and `log` out.
- Claude Code and Codex backends using the CLI's existing subscription login.
- Tasks (`enhance`, `describe_media`, `chat`) and per-model-family prompt targets.
- Enforced web research with local Markdown skills (manual and learned).
- Resumable per-node conversations.
- Live log panel with Stop / Clear / Copy-prompt, per-run token counter, and run-alone buttons.
