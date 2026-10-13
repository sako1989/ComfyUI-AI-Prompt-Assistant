# Security policy

## Reporting a vulnerability

Please report security problems privately: open the repository's **Security** tab and use
[**Report a vulnerability**](https://github.com/sako1989/ComfyUI-AI-Prompt-Assistant/security/advisories/new).
If that form is unavailable, open an issue that only asks for a private contact, without any
details of the problem. Include the ComfyUI version, the backend (Claude Code or Codex) and its CLI
version, and the steps to reproduce.

Only the latest version on the `main` branch is supported.

## What this pack does on your machine

- Starts the `claude` or `codex` CLI as a child process of ComfyUI, with your own login.
- Claude Code is limited to the `Read`, `WebSearch` and `WebFetch` tools, with MCP servers and user
  settings disabled. Codex runs with a read-only sandbox, approvals set to `never`, and MCP servers
  disabled. Neither is given shell, edit or write tools by this pack.
- Writes reference media to `workspace/runs/`, conversation ids to `workspace/sessions.json`, and
  researched skills to `skills/learned-*/`. The agent itself never writes files; the host process does.
- Registers three unauthenticated routes on the ComfyUI server: `GET /ai_prompt_assistant/status`,
  `POST /ai_prompt_assistant/cancel`, `GET /ai_prompt_assistant/skills`.

## In scope

- Escaping the tool or sandbox restrictions above through node inputs, skills, or web content.
- Path traversal or arbitrary file read/write through skill names or the HTTP routes.
- Command or argument injection through node inputs.

## Out of scope

- Anything that requires editing `config.json` (`extra_claude_args`, `extra_codex_args`, CLI paths):
  whoever can write that file already controls the machine.
- Exposure caused by publishing the ComfyUI port to an untrusted network.
- Behaviour of the Claude Code or Codex CLIs themselves; report those to their vendors.
