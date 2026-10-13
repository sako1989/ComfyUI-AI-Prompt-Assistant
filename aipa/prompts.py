"""System prompt, user message assembly and output schema."""

from __future__ import annotations

from typing import Optional

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "The final generation prompt (or the answer, in chat mode)."},
        "negative_prompt": {"type": "string", "description": "Negative prompt if the target model uses one, else empty string."},
        "notes": {"type": "string", "description": "Brief explanation of choices, assumptions, and any web sources used."},
    },
    "required": ["prompt", "negative_prompt", "notes"],
    "additionalProperties": False,
}

TASKS = ["enhance", "describe_media", "chat"]

# id -> (label for docs, guidance injected into the system prompt)
TARGETS: dict[str, tuple[str, str]] = {
    "auto": (
        "Auto-detect",
        "Infer the most likely target from the user's text, instructions and media. If unclear, write a "
        "natural-language prompt (60-120 words) that works for modern models such as Flux, Qwen-Image or Wan.",
    ),
    "flux": (
        "Flux (1 / Kontext / 2)",
        "Natural-language prose, one or two paragraphs, 50-120 words. Order: subject and action, setting, "
        "lighting, camera/lens/composition, style and mood. No tag soup, no weights like (word:1.2), no "
        "quality spam. Flux mostly ignores negative prompts: leave negative_prompt empty unless a CFG-style "
        "workflow is mentioned.",
    ),
    "sdxl": (
        "SDXL / Illustrious / Pony-style",
        "Comma-separated tags and short phrases, most important first. Quality tags are fine "
        "(masterpiece, best quality) but keep them short. Weights in the form (term:1.2) are allowed. "
        "Provide a useful negative_prompt (artifacts, anatomy, unwanted styles).",
    ),
    "sd15": (
        "Stable Diffusion 1.5",
        "Short comma-separated tags, aim for under 75 tokens. Emphasis with (term:1.2) allowed. "
        "Provide a strong negative_prompt.",
    ),
    "qwen_image": (
        "Qwen-Image / Qwen-Image-Edit",
        "Detailed natural language, 60-150 words. Qwen renders text very well: if signage or text must "
        "appear, quote it exactly and say where it appears. For edits, describe the change precisely and "
        "what must stay unchanged. negative_prompt optional (short).",
    ),
    "z_image": (
        "Z-Image / HiDream / Chroma",
        "Natural-language prose similar to Flux, 50-120 words, precise about lighting and materials. "
        "negative_prompt short or empty.",
    ),
    "wan_video": (
        "Wan 2.1 / 2.2 (text/image-to-video)",
        "One paragraph, 80-150 words, present tense. Describe: subject and appearance, the action as it "
        "unfolds, camera movement (e.g. slow dolly-in, handheld, static), environment, lighting, style. "
        "Motion must be explicit and physically plausible for a 5 second clip. Provide a negative_prompt "
        "listing common video artifacts (e.g. static, blurry, distorted hands, text, watermark, jitter).",
    ),
    "ltx_video": (
        "LTX-Video",
        "One long detailed paragraph, 100-200 words, chronological, in present tense, describing what "
        "happens first to last, camera motion, lighting, and mood. Very specific about movement. "
        "negative_prompt: standard video artifacts.",
    ),
    "hunyuan_video": (
        "HunyuanVideo",
        "Natural-language paragraph, 60-150 words: subject, action, scene, camera, lighting, style. "
        "Mention motion explicitly. negative_prompt: video artifacts.",
    ),
    "cloud_video": (
        "Kling / Veo / Sora / Runway",
        "Cinematic shot description, 40-120 words: shot type, subject and action, camera movement, "
        "lens feel, lighting, atmosphere, pacing. Avoid impossible physics. negative_prompt: short list "
        "or empty.",
    ),
    "midjourney": (
        "Midjourney",
        "Concise evocative phrases separated by commas, then optional parameters such as --ar 16:9 "
        "--stylize 200. negative_prompt: list of words for --no (without the flag).",
    ),
    "custom": (
        "Custom (see instructions)",
        "Follow the user's instructions for format and length exactly.",
    ),
}

TARGET_OPTIONS = list(TARGETS.keys())


def _task_block(task: str, has_media: bool) -> str:
    if task == "describe_media":
        return (
            "TASK: Describe the attached media faithfully as a generation prompt that would recreate it: "
            "subject(s), pose/action, composition and framing, environment, lighting, colour palette, "
            "materials/textures, mood, artistic style or photographic look (lens, depth of field). For video "
            "frames, describe how the scene and camera move over time. The user's text, if any, says what to "
            "focus on or what to change relative to the media."
        )
    if task == "chat":
        return (
            "TASK: Act as a helpful prompt-engineering assistant and answer the user's question or request "
            "directly. Put your complete answer in the `prompt` field (plain text, may contain line breaks). "
            "Leave `negative_prompt` empty unless the user asks for one. Use `notes` for sources or caveats."
        )
    base = (
        "TASK: Turn the user's text into the best possible generation prompt for the target model. If the "
        "text is a rough idea, build a complete prompt; if it is already a prompt, enhance it: keep every "
        "intent, subject, and constraint the user gave, fix vagueness, add concrete visual detail "
        "(subject, setting, lighting, camera, style, mood) and remove contradictions. Never invent a "
        "different subject."
    )
    if has_media:
        base += (
            " Reference media is attached: use it for style, palette, composition, identity or motion cues "
            "as appropriate, but the user's text has priority over the media."
        )
    return base


def build_system_prompt(task: str, target: str, web_search: bool, backend_name: str, has_media: bool) -> str:
    target_label, target_guide = TARGETS.get(target, TARGETS["auto"])
    media_hint = (
        "Reference media is provided as PNG files listed in the user message. Use the Read tool on each "
        "listed file to actually look at it before writing."
        if backend_name == "claude_code"
        else "Reference media, when provided, is attached to this message as images."
    )
    search_hint = (
        "WEB SEARCH: enabled. If the request mentions a model, LoRA, style, artist, franchise, product, "
        "place, person, event or technical term you are not confident about, or asks about something recent, "
        "run 1-3 focused web searches first and use what you learn. Do not search for things you already "
        "know well."
        if web_search
        else "WEB SEARCH: disabled. Rely on your own knowledge; say in `notes` if something should be verified."
    )
    return "\n\n".join([
        "You are an expert prompt engineer for AI image and video generation, embedded in a ComfyUI node. "
        "You do not edit files or run commands; you only read the provided media, optionally search the web, "
        "and return a result.",
        _task_block(task, has_media),
        f"TARGET MODEL: {target_label}.\nTarget conventions: {target_guide}",
        media_hint,
        search_hint,
        "LANGUAGE: Write `prompt` and `negative_prompt` in English (generation models expect English) unless "
        "the user explicitly asks for another language. `notes` may be in the user's language.",
        "OUTPUT: Return only a JSON object with exactly these keys: \"prompt\" (string), "
        "\"negative_prompt\" (string, empty if not applicable), \"notes\" (string, 1-3 sentences). No markdown "
        "fences, no commentary outside the JSON. Do not wrap the prompt in quotes inside the string.",
    ])


def build_user_message(
    text: str,
    instructions: str,
    task: str,
    target: str,
    image_paths: list[str],
    video_frame_paths: list[str],
    video_meta: Optional[dict],
    *,
    resume: bool = False,
) -> str:
    parts: list[str] = []
    if resume:
        parts.append("Follow-up in the same conversation. Apply the new request below to our previous result, "
                     "unless it clearly describes a brand-new prompt.")
    parts.append(f"Task mode: {task}. Target: {TARGETS.get(target, TARGETS['auto'])[0]}.")
    if instructions and instructions.strip():
        parts.append("Extra instructions from the user:\n" + instructions.strip())
    if text and text.strip():
        parts.append("User text:\n" + text.strip())
    else:
        parts.append("User text: (none provided)")
    if image_paths:
        parts.append("Reference image file(s):\n" + "\n".join(f"- {p}" for p in image_paths))
    if video_frame_paths:
        meta_line = ""
        if video_meta:
            meta_line = (
                f" (video: {video_meta.get('frame_count')} frames, {video_meta.get('fps')} fps, "
                f"{video_meta.get('duration_sec')} s, {video_meta.get('width')}x{video_meta.get('height')}; "
                f"frames sampled at {video_meta.get('sampled_times_sec')} s)"
            )
        parts.append(
            f"Reference video frames in chronological order{meta_line}:\n"
            + "\n".join(f"- {p}" for p in video_frame_paths)
        )
    parts.append("Return the JSON object now.")
    return "\n\n".join(parts)
