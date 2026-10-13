"""The AI Prompt Assistant V3 node."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback

from comfy_api.latest import io

from .aipa import config, media, prompts, research, skills
from .aipa.backends import BACKENDS, EFFORT_OPTIONS, model_options
from .aipa.backends.base import LogEvent, RunSpec
from .aipa.sessions import SessionStore

log = logging.getLogger("ai_prompt_assistant")

_SESSIONS = SessionStore(config.SESSIONS_PATH)


def _emit(node_id: str, kind: str, text: str) -> None:
    """Push a log line to the frontend widget (and mirror status to the node progress text)."""
    payload = {"node_id": str(node_id), "kind": kind, "text": text, "ts": time.time()}
    try:
        from server import PromptServer  # type: ignore

        PromptServer.instance.send_sync(config.EVENT_NAME, payload)
        if kind in ("status", "error"):
            try:
                PromptServer.instance.send_progress_text(text[:120], str(node_id))
            except Exception:
                pass
    except Exception:
        pass
    if kind == "error":
        log.error("[AI Prompt Assistant %s] %s", node_id, text)
    else:
        log.info("[AI Prompt Assistant %s] %s: %s", node_id, kind, text[:200])


def _workflow_id(extra_pnginfo) -> str:
    try:
        wf = (extra_pnginfo or {}).get("workflow") or {}
        wid = wf.get("id")
        return str(wid) if wid else ""
    except Exception:
        return ""


class AIPromptAssistant(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="AIPromptAssistant",
            display_name="AI Prompt Assistant (Claude Code / Codex)",
            category="AI Prompt Assistant",
            description=(
                "Enhances or builds a generation prompt with your own Claude Code or Codex CLI running in "
                "the background (uses your subscription, no API key). Accepts text plus optional reference "
                "IMAGE / VIDEO, can search the web, and streams its thinking into the log panel."
            ),
            search_aliases=["prompt enhancer", "claude code", "codex", "llm prompt", "prompt assistant"],
            inputs=[
                io.String.Input("text", multiline=True, default="",
                                placeholder="Your idea, draft prompt, or question...",
                                tooltip="Rough idea, existing prompt to improve, or (chat mode) a question."),
                io.Combo.Input("backend", options=[b.name for b in BACKENDS.values()], default="claude_code",
                               tooltip="Which local CLI agent runs the request. Both use your existing login."),
                io.Combo.Input("model", options=model_options(), default="claude/default",
                               tooltip="Model for the selected backend. The list is filtered by backend in the UI."),
                io.String.Input("custom_model", default="", optional=True,
                                placeholder="override model id (optional)",
                                tooltip="Any model id the CLI accepts; overrides the dropdown when not empty."),
                io.Combo.Input("effort", options=EFFORT_OPTIONS, default="medium",
                               tooltip="Reasoning effort. Claude: --effort. Codex: model_reasoning_effort."),
                io.Combo.Input("task", options=prompts.TASKS, default="enhance",
                               tooltip="enhance: improve/build a prompt from text (+media). describe_media: turn "
                                       "image/video into a prompt. chat: free-form answer in the prompt output."),
                io.Combo.Input("target", options=prompts.TARGET_OPTIONS, default="auto",
                               tooltip="Generation model family; shapes prompt style and length."),
                io.String.Input("instructions", multiline=True, default="", optional=True,
                                placeholder="Extra guidance, e.g. 'cinematic, 90 words, keep the red dress'",
                                tooltip="Optional extra guidance for the assistant."),
                io.Boolean.Input("web_search", default=True,
                                 tooltip="Allow enforced research for subjects without local skills. When off, missing knowledge stops the run."),
                io.Combo.Input("session", options=["new", "continue"], default="new",
                               tooltip="continue = resume this node's previous conversation so you can iterate "
                                       "('make it moodier'). new = fresh conversation every run."),
                io.Image.Input("image", optional=True, tooltip="Reference image(s). A batch is sampled to max_frames."),
                io.Video.Input("video", optional=True, tooltip="Reference video; evenly sampled to max_frames frames."),
                io.Int.Input("max_frames", default=6, min=1, max=16, advanced=True,
                             tooltip="Max images / video frames sent to the model."),
                io.Int.Input("max_image_side", default=1024, min=256, max=2048, step=64, advanced=True,
                             tooltip="Longest side of each reference image after downscaling."),
                io.Int.Input("timeout_sec", default=600, min=30, max=3600, step=10, advanced=True,
                             tooltip="Kill the CLI if it runs longer than this."),
                io.Int.Input("seed", default=0, min=0, max=0xFFFFFFFFFFFFFFFF, control_after_generate=True,
                             tooltip="Only controls caching: change it to force a re-run with identical inputs. "
                                     "Never sent to the model."),
                # Append widgets to preserve positional values in existing saved workflows.
                io.String.Input("generation_model", default="", optional=True,
                                tooltip="Exact image/video model version. Forces a local-skill check and research on a miss; distinct from the assistant model."),
                io.Combo.Input("skill", options=skills.SkillStore().options(), default="auto", optional=True,
                               tooltip="Auto matches exact model names/aliases; select a Markdown skill to also load it explicitly."),
                io.Combo.Input("skill_action", options=["auto", "create", "refresh"], default="auto", optional=True,
                               tooltip="Auto reuses local skills. Create/refresh performs verified web research and saves a learned skill."),
                io.String.Input("skill_topic", default="", optional=True,
                                tooltip="Topic/model to research for create/refresh. Falls back to generation_model or selected skill name."),
            ],
            outputs=[
                io.String.Output("prompt", tooltip="Enhanced prompt (or the answer in chat mode)."),
                io.String.Output("negative_prompt"),
                io.String.Output("notes", tooltip="Assistant's explanation / sources."),
                io.String.Output("log", tooltip="Full run log as text."),
            ],
            hidden=[io.Hidden.unique_id, io.Hidden.extra_pnginfo],
        )

    @classmethod
    async def execute(
        cls,
        text: str,
        backend: str,
        model: str,
        effort: str,
        task: str,
        target: str,
        web_search: bool,
        session: str,
        max_frames: int,
        max_image_side: int,
        timeout_sec: int,
        seed: int,
        custom_model: str = "",
        instructions: str = "",
        image=None,
        video=None,
        generation_model: str = "",
        skill: str = "auto",
        skill_action: str = "auto",
        skill_topic: str = "",
    ) -> io.NodeOutput:
        # read hidden values before the first await (cls.hidden is per-execution state)
        node_id = str(cls.hidden.unique_id or "?")
        wf_id = _workflow_id(cls.hidden.extra_pnginfo)
        session_key = f"{wf_id}:{node_id}" if wf_id else node_id

        log_lines: list[str] = []
        flags = {"error_emitted": False}

        def on_event(ev: LogEvent) -> None:
            if ev.kind == "error":
                flags["error_emitted"] = True
            stamp = time.strftime("%H:%M:%S")
            log_lines.append(f"[{stamp}] {ev.kind:>11}: {ev.text}")
            _emit(node_id, ev.kind, ev.text)

        on_event(LogEvent("status", "started"))
        try:
            # run the blocking work off the event loop so async nodes in the same prompt keep going
            prompt_out, negative_out, notes_out = await asyncio.to_thread(
                cls._run, node_id, session_key, on_event,
                text, backend, model, custom_model, effort, task, target, instructions,
                bool(web_search), session, int(max_frames), int(max_image_side), int(timeout_sec),
                image, video, generation_model, skill, skill_action, skill_topic,
            )
        except BaseException as e:
            if not flags["error_emitted"]:
                on_event(LogEvent("error", f"{type(e).__name__}: {e}"))
            raise
        on_event(LogEvent("done", "done"))
        return io.NodeOutput(prompt_out, negative_out, notes_out, "\n".join(log_lines))

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return skills.SkillStore().fingerprint()

    @classmethod
    def validate_inputs(cls, skill="auto"):
        if skill != "auto":
            try:
                skills.SkillStore().load(skill)
            except (OSError, ValueError) as exc:
                return str(exc)
        return True

    @classmethod
    def _run(cls, node_id, session_key, on_event, text, backend, model, custom_model, effort, task, target,
             instructions, web_search, session, max_frames, max_image_side, timeout_sec, image, video,
             generation_model="", skill="auto", skill_action="auto", skill_topic=""):
        b = BACKENDS[backend]
        cfg = config.load_config()
        exe = config.find_cli(b.cli_name, cfg)

        workspace = config.ensure_workspace()
        run_dir = config.new_run_dir()
        config.cleanup_runs(cfg.get("keep_runs", 20), exclude=run_dir)

        # ---- media -----------------------------------------------------------------
        image_paths: list[str] = []
        video_paths: list[str] = []
        video_meta = None
        try:
            if image is not None:
                paths = media.image_batch_to_pngs(image, run_dir, max_frames=max_frames, max_side=max_image_side)
                image_paths = [str(p) for p in paths]
                on_event(LogEvent("status", f"Saved {len(image_paths)} reference image(s) -> {run_dir.name}"))
            if video is not None:
                paths, video_meta = media.video_to_frame_pngs(video, run_dir, max_frames=max_frames, max_side=max_image_side)
                video_paths = [str(p) for p in paths]
                on_event(LogEvent("status", f"Sampled {len(video_paths)} video frame(s) of {video_meta['frame_count']} "
                                            f"({video_meta['duration_sec']} s @ {video_meta['fps']} fps)"))
        except Exception as e:
            raise RuntimeError(f"Media conversion failed: {e}") from e

        has_media = bool(image_paths or video_paths)
        if task == "describe_media" and not has_media:
            on_event(LogEvent("warn", "task=describe_media but no image/video connected; proceeding with text only"))

        # ---- session ---------------------------------------------------------------
        resume_id = None
        if session == "continue":
            resume_id = _SESSIONS.get(session_key, b.name)
            on_event(LogEvent("status", f"Resuming conversation {resume_id}" if resume_id
                              else "No previous conversation for this node; starting a new one"))
        else:
            _SESSIONS.clear(session_key, b.name)

        # ---- prompt ----------------------------------------------------------------
        model_id = b.resolve_model(model, custom_model)
        effort_id = None if effort in (None, "", "default") else effort
        spec = RunSpec(
            user_message=prompts.build_user_message(text, instructions, task, target, image_paths, video_paths,
                                                    video_meta, resume=bool(resume_id)),
            system_prompt=prompts.build_system_prompt(task, target, False, b.name, has_media),
            model=model_id,
            effort=effort_id,
            web_search=web_search,
            image_paths=image_paths + video_paths,
            schema=prompts.OUTPUT_SCHEMA,
            resume_id=resume_id,
            workspace=workspace,
            run_dir=run_dir,
            extra_args=list(cfg.get(f"extra_{b.cli_name}_args", []) or []),
        )
        if generation_model.strip():
            spec.user_message += "\nExact generation model requested: " + generation_model.strip()
        on_event(LogEvent("status", f"Backend {b.display} | model {model_id or 'default'} | effort {effort_id or 'default'} | "
                                    f"web search {'on' if web_search else 'off'}"))

        # ---- run -------------------------------------------------------------------
        try:
            previous_topics = json.loads(_SESSIONS.get(session_key, b.name + ":topics") or "[]") if resume_id else []
            result, topics = research.run_with_skills(
                b, exe, spec, node_id=node_id, timeout_sec=float(timeout_sec), on_event=on_event,
                strip_api_keys=bool(cfg.get("strip_api_keys", True)),
                target=target, generation_model=generation_model, skill=skill,
                skill_action=skill_action, skill_topic=skill_topic, previous_topics=previous_topics,
            )
        except Exception as e:
            if resume_id and isinstance(e, research.ResumeError):
                _SESSIONS.clear(session_key, b.name)
                on_event(LogEvent("warn", "Previous conversation could not be resumed and has been forgotten; "
                                          "run again to start fresh"))
            log.debug(traceback.format_exc())
            raise

        for w in result.warnings:
            on_event(LogEvent("warn", w))

        if result.error:
            # the parser already streamed CLI-reported errors; execute() emits anything not yet shown
            if resume_id:
                _SESSIONS.clear(session_key, b.name)
                on_event(LogEvent("warn", "Previous conversation could not be resumed and has been forgotten; "
                                          "run again to start fresh"))
            raise RuntimeError(result.error)

        if result.session_id:
            _SESSIONS.set(session_key, b.name, result.session_id)
            _SESSIONS.set(session_key, b.name + ":topics", json.dumps(topics))

        # ---- result ----------------------------------------------------------------
        prompt_out, negative_out, notes_out = _unpack(result.structured, result.text)
        if not prompt_out.strip():
            raise RuntimeError(f"{b.display} returned an empty result")

        on_event(LogEvent("result", prompt_out))
        if negative_out.strip():
            on_event(LogEvent("result", "Negative: " + negative_out))
        if notes_out.strip():
            on_event(LogEvent("text", "Notes: " + notes_out))
        return prompt_out, negative_out, notes_out


def _unpack(structured, text: str) -> tuple[str, str, str]:
    if isinstance(structured, dict) and structured.get("prompt") is not None:
        p = structured.get("prompt")
        n = structured.get("negative_prompt") or ""
        notes = structured.get("notes") or ""
        return (str(p).strip(), str(n).strip(), str(notes).strip())
    return ((text or "").strip(), "", "")
