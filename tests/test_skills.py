"""Run directly: python tests/test_skills.py (no ComfyUI or network needed)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aipa.backends import BACKENDS
from aipa.backends.base import ParseState, RunResult, RunSpec
from aipa import prompts, runner
from aipa.research import ResumeError, run_with_skills
from aipa.skills import SkillStore
from aipa.usage import UsageCounter


def result(data, searches=0):
    return RunResult(json.dumps(data), data, "session", None, {}, completed_searches=searches)


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SkillStore(self.root / "skills")
        self.spec = RunSpec("a red cat for Nova 9", "system", None, None, True, [],
                            prompts.OUTPUT_SCHEMA, None, self.root, self.root / "run")
        self.calls = []

    def fake(self, answers):
        def run(b, exe, spec, **kwargs):
            self.calls.append(spec)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return run

    def execute(self, answers, **kwargs):
        return run_with_skills(BACKENDS["codex"], "fake", self.spec, node_id="1", timeout_sec=60,
                               on_event=lambda e: None, store=self.store, run=self.fake(answers), **kwargs)

    def topic(self, *subjects):
        return result({"prompt": "", "topics": list(subjects)})

    def researched(self, searches=1, verified=True):
        return result({"prompt": "# Nova 9\nUse clear natural language.", "verified": verified,
                       "sources": ["https://example.org/model-card"]}, searches)

    def final(self):
        return result({"prompt": "A red cat.", "negative_prompt": "", "notes": ""})

    def test_research_then_reuse_across_store_instances(self):
        self.execute([self.topic("Nova 9"), self.researched(), self.final()])
        self.assertEqual(len(self.store.catalog()), 1)
        self.assertIn("Use clear natural language", self.calls[-1].system_prompt)
        self.assertFalse(self.calls[-1].web_search)
        self.store = SkillStore(self.store.root)
        self.calls.clear()
        self.execute([self.topic("Nova 9"), self.final()])
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(any(s.web_search for s in self.calls))

    def test_search_claim_without_tool_evidence_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Could not verify"):
            self.execute([self.topic("Nova 9"), self.researched(0), self.researched(0)])
        self.assertEqual(self.store.catalog(), [])
        self.assertEqual(len(self.calls), 3)

    def test_unknown_or_unsourced_research_is_rejected(self):
        bad = self.researched()
        bad.structured["sources"] = []
        with self.assertRaisesRegex(RuntimeError, "Could not verify"):
            self.execute([self.topic("Nova 9"), self.researched(verified=False), bad])
        self.assertEqual(self.store.catalog(), [])

    def test_web_off_blocks_miss_but_accepts_cache(self):
        self.spec.web_search = False
        with self.assertRaisesRegex(RuntimeError, "web_search is off"):
            self.execute([self.topic("Nova 9")])
        self.store.save_research("Nova 9", "Guidance", ["https://example.org"])
        self.execute([self.topic("Nova 9"), self.final()])

    def test_explicit_model_cannot_be_ignored_by_detector(self):
        self.execute([self.topic(), self.researched(), self.final()], generation_model="Nova 9")
        self.assertIsNotNone(self.store.find("Nova 9"))

    def test_exact_versions_and_manual_aliases(self):
        self.store.root.mkdir()
        (self.store.root / "manual.md").write_text(
            '---\nname: Nova 9\naliases: ["Nova-9", "Nova version 9"]\n---\nPrecise advice.', encoding="utf-8")
        self.assertIsNotNone(self.store.find("Nova-9"))
        self.assertIsNone(self.store.find("Nova 9.1"))
        self.assertIsNone(self.store.find("Nova"))
        self.execute([self.topic("Nova version 9"), self.final()], skill="manual.md")
        self.assertIn("Precise advice", self.calls[-1].system_prompt)

    def test_refresh_preserves_manual_file(self):
        self.store.root.mkdir()
        path = self.store.root / "manual.md"
        path.write_text("Manual content", encoding="utf-8")
        self.execute([self.topic(), self.researched(), self.final()], skill="manual.md", skill_action="refresh")
        self.assertEqual(path.read_text(), "Manual content")
        self.assertEqual(len(self.store.catalog()), 2)

    def test_create_arbitrary_topic(self):
        self.execute([self.topic(), self.researched(), self.final()], skill_action="create", skill_topic="Lighting")
        self.assertIsNotNone(self.store.find("Lighting"))

    def test_path_traversal_and_missing_selected_skill_fail(self):
        outside = self.root / "outside.md"
        outside.write_text("outside")
        with self.assertRaises(ValueError):
            self.store.load("../outside.md")
        with self.assertRaises(OSError):
            self.execute([], skill="missing.md")

    def test_manual_edits_change_fingerprint(self):
        s = self.store.save_research("Nova 9", "Before", ["https://example.org"])
        before = self.store.fingerprint()
        path = self.store.root / s.key
        path.write_text(path.read_text().replace("Before", "After"))
        self.assertNotEqual(before, self.store.fingerprint())

    def test_resume_reinjects_current_skills_and_policy(self):
        self.spec.resume_id = "previous"
        self.execute([self.topic("Nova 9"), self.researched(), self.final()])
        self.assertIsNone(self.calls[0].resume_id)
        self.assertIsNone(self.calls[1].resume_id)
        self.assertEqual(self.calls[-1].resume_id, "previous")
        self.assertIn("LOCAL SKILLS", self.calls[-1].user_message)
        self.assertIn("RESEARCH POLICY", self.calls[-1].user_message)

    def test_cancel_does_not_retry_or_generate(self):
        with self.assertRaises(runner.RunCancelled):
            self.execute([self.topic("Nova 9"), runner.RunCancelled("stop")])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.store.catalog(), [])

    def test_malformed_plan_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "identify research subjects"):
            self.execute([result({"prompt": "", "topics": "Nova"})])

    def test_no_models_no_research(self):
        self.execute([self.topic(), self.final()])
        self.assertEqual(self.store.catalog(), [])

    def test_usage_includes_identification_rejected_research_and_final(self):
        answers = [self.topic("Nova 9"), self.researched(0), self.researched(), self.final()]
        for answer in answers:
            answer.usage = {"input_tokens": 100, "output_tokens": 10, "cache_read": 20}
        response, _ = self.execute(answers)
        self.assertEqual(response.usage["total_tokens"], 440)
        self.assertEqual(response.usage["cache_read"], 80)
        self.assertEqual(response.usage["calls"], 4)
        self.assertIsNone(response.usage["cost_usd"])

    def test_refresh_receives_requested_changes_and_saves_preference(self):
        self.spec.user_message = "Update this skill to always include a negative prompt."
        old = self.store.save_research("Nova 9", "Existing conventions", ["https://example.org"])
        plan = self.topic()
        plan.structured["negative_prompt_policy"] = "required"
        final = self.final()
        final.structured["negative_prompt"] = "blur, distorted paws"
        self.execute([plan, self.researched(), final], skill=old.key, skill_action="refresh")
        request = json.loads(self.calls[1].user_message)
        self.assertEqual(request["user_request"], self.spec.user_message)
        self.assertIn("Existing conventions", request["current_skill"])
        self.assertTrue(self.store.find("Nova 9").metadata["require_negative_prompt"])
        self.assertEqual(self.calls[-1].schema["properties"]["negative_prompt"]["minLength"], 1)
        self.assertNotIn("minLength", prompts.OUTPUT_SCHEMA["properties"]["negative_prompt"])

    def test_cached_required_negative_is_retried_and_never_silently_empty(self):
        saved = self.store.save_research("Nova 9", "Advice", ["https://example.org"], require_negative_prompt=True)
        corrected = self.final()
        corrected.structured["negative_prompt"] = "blur"
        self.execute([self.topic(), self.final(), corrected], skill=saved.key)
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(any(s.web_search for s in self.calls))
        blank = self.final()
        blank.structured["negative_prompt"] = "  "
        with self.assertRaisesRegex(RuntimeError, "omitted the required negative"):
            self.execute([self.topic(), blank, blank], skill=saved.key)

    def test_refresh_preserves_negative_preference_without_repeated_request(self):
        saved = self.store.save_research("Nova 9", "Advice", ["https://example.org"], require_negative_prompt=True)
        final = self.final()
        final.structured["negative_prompt"] = "blur"
        self.execute([self.topic(), self.researched(), final], skill=saved.key, skill_action="refresh")
        self.assertTrue(self.store.find("Nova 9").metadata["require_negative_prompt"])

    def test_explicit_omit_overrides_saved_preference_for_this_run(self):
        saved = self.store.save_research("Nova 9", "Advice", ["https://example.org"], require_negative_prompt=True)
        plan = self.topic()
        plan.structured["negative_prompt_policy"] = "omit"
        self.execute([plan, self.final()], skill=saved.key)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("minLength", self.calls[-1].schema["properties"]["negative_prompt"])
        self.assertTrue(self.store.find("Nova 9").metadata["require_negative_prompt"])

    def test_only_final_resume_failure_invalidates_conversation(self):
        self.spec.resume_id = "previous"
        error = RunResult("", None, None, "quota exceeded", {})
        with self.assertRaises(RuntimeError) as caught:
            self.execute([error])
        self.assertNotIsInstance(caught.exception, ResumeError)
        with self.assertRaises(ResumeError):
            self.execute([self.topic(), error])


class UsageTests(unittest.TestCase):
    def test_codex_cached_tokens_are_already_in_input(self):
        counter = UsageCounter("codex")
        counter.add({"input_tokens": 100, "output_tokens": 10, "cache_read": 80})
        self.assertEqual(counter.snapshot()["total_tokens"], 110)
        self.assertIsNone(counter.snapshot()["cost_usd"])

    def test_claude_separate_cache_tokens_and_reported_cost(self):
        counter = UsageCounter("claude_code")
        counter.add({"input_tokens": 100, "output_tokens": 10, "cache_read": 80,
                     "cache_creation": 20, "cost_usd": 0.02})
        counter.add({"input_tokens": 50, "output_tokens": 5, "cost_usd": 0})
        self.assertEqual(counter.snapshot()["total_tokens"], 265)
        self.assertEqual(counter.snapshot()["cost_usd"], 0.02)
        self.assertFalse(counter.snapshot()["cost_partial"])

    def test_missing_reports_mark_partial_not_zero_cost(self):
        counter = UsageCounter("codex")
        counter.add({"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01})
        counter.add({})
        self.assertEqual(counter.snapshot()["total_tokens"], 15)
        self.assertTrue(counter.snapshot()["tokens_partial"])
        self.assertTrue(counter.snapshot()["cost_partial"])


class SearchEvidenceTests(unittest.TestCase):
    def test_codex_actual_cli_event_without_status(self):
        b, st = BACKENDS["codex"], ParseState()
        b.parse_line('{"type":"item.completed","item":{"id":"ws_1","type":"web_search",'
                     '"query":"LTX-2.3 official model card","action":{"type":"search",'
                     '"query":"LTX-2.3 official model card","queries":["LTX-2.3 official model card"]}}}', st)
        self.assertEqual(st.completed_searches, 1)

    def test_codex_completed_search_only(self):
        b, st = BACKENDS["codex"], ParseState()
        for event, status, action, count in [
            ("item.started", "in_progress", "search", 0),
            ("item.completed", "failed", "search", 0),
            ("item.completed", "completed", "open_page", 0),
            ("item.completed", "completed", "search", 1),
            ("item.completed", "completed", "search", 1),
        ]:
            b.parse_line(json.dumps({"type": event, "item": {"id": "a", "type": "web_search",
                         "status": status, "action": {"type": action, "query": "Nova"}}}), st)
            self.assertEqual(st.completed_searches, count)
        self.assertEqual(b.finalize(st, 0, "").completed_searches, 1)

    def test_claude_correlates_successful_search_results(self):
        b, st = BACKENDS["claude_code"], ParseState()
        def event(kind, block):
            b.parse_line(json.dumps({"type": kind, "message": {"content": [block]}}), st)
        for ident, tool, failed, expected in [("1", "Read", False, 0), ("2", "WebSearch", True, 0),
                                              ("3", "WebSearch", False, 1)]:
            event("assistant", {"type": "tool_use", "id": ident, "name": tool, "input": {}})
            event("user", {"type": "tool_result", "tool_use_id": ident, "is_error": failed, "content": "results"})
            self.assertEqual(st.completed_searches, expected)
        event("user", {"type": "tool_result", "tool_use_id": "3", "content": "duplicate"})
        self.assertEqual(st.completed_searches, 1)


if __name__ == "__main__":
    unittest.main()
