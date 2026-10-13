"""ComfyUI-AI-Prompt-Assistant — prompt engineering with your own Claude Code / Codex CLI."""

import logging

from comfy_api.latest import ComfyExtension, io

from .nodes import AIPromptAssistant

WEB_DIRECTORY = "./web"

log = logging.getLogger("ai_prompt_assistant")

try:
    from server import PromptServer  # type: ignore

    from . import server_routes

    server_routes.register(PromptServer.instance)
except Exception as e:  # routes are optional (headless / tests)
    log.warning("AI Prompt Assistant: could not register HTTP routes: %s", e)


class AIPromptAssistantExtension(ComfyExtension):
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [AIPromptAssistant]


async def comfy_entrypoint() -> AIPromptAssistantExtension:
    return AIPromptAssistantExtension()


__all__ = ["comfy_entrypoint", "WEB_DIRECTORY"]
