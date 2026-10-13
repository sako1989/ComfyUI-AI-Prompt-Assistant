"""HTTP routes: cancel a running assistant, report CLI availability."""

from __future__ import annotations

from aiohttp import web

from .aipa import config, runner, skills


def register(prompt_server) -> None:
    routes = prompt_server.routes

    @routes.get("/ai_prompt_assistant/skills")
    async def _skills(request: web.Request):
        return web.json_response({"skills": skills.SkillStore().options()})

    @routes.post("/ai_prompt_assistant/cancel")
    async def _cancel(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        node_id = str(data.get("node_id", ""))
        ok = runner.cancel(node_id) if node_id else False
        return web.json_response({"cancelled": ok, "node_id": node_id})

    @routes.get("/ai_prompt_assistant/status")
    async def _status(request: web.Request):
        cfg = config.load_config()
        out = {"active": runner.active_node_ids(), "cli": {}}
        for name in ("claude", "codex"):
            try:
                out["cli"][name] = config.find_cli(name, cfg)
            except config.CLINotFound as e:
                out["cli"][name] = None
                out.setdefault("errors", {})[name] = str(e)
        return web.json_response(out)
