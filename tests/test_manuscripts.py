"""Independent document storage and fake AI only; never call live providers."""

import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from sona.ai_model_service import AIModelService, ModelTestError
from sona.database import ModelRepository
from sona.file_lock import exclusive_file_lock
from sona.manuscripts import ManuscriptService, OPTIMIZE_PROMPT, split_transcript, title_material


class ManuscriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "sona.sqlite3"
        ModelRepository(self.database, ())
        self.ai = Mock()
        self.profile = {"id": "model-id", "provider": "openai", "model_name": "fake",
                        "base_url": None, "api_key_ref": "reference-only", "config": {}}
        self.ai.default_generation_profile.return_value = self.profile
        self.ai.prepare_generation.return_value = object()
        self.service = ManuscriptService(self.database, self.ai, start_worker=False)
        self.addCleanup(self.service.close)
        self.repo = self.service.repository
        with self.repo.connection() as db:
            db.execute("INSERT INTO audio_files(id,name,suffix,size_bytes) VALUES ('audio','不可用作标题.wav','.wav',3)")
            db.execute("UPDATE transcription_tasks SET status='completed' WHERE audio_id='audio'")
            db.execute("""INSERT INTO transcription_results
                       (audio_id,text,segments_json,language,duration,model_id,engine,model_revision,device)
                       VALUES ('audio','我今天去了公园然后回家','[]','zh',1,'speech','fake','1','CPU')""")

    def raw(self, identifier):
        with self.repo.connection() as db:
            return dict(db.execute("SELECT * FROM manuscripts WHERE id=?", (identifier,)).fetchone())

    def process(self, *responses):
        self.ai.generate_text.side_effect = responses
        self.service._process(self.repo.claim())

    def test_create_is_independent_and_does_not_call_ai(self):
        identifier = self.service.create("audio")
        row = self.repo.list_all()[0]
        self.assertEqual(row["status"], "queued")
        self.assertNotIn("wav", row["title"])
        self.assertNotIn("audio_id", self.raw(identifier))
        self.assertNotIn("source_text", row)
        self.assertNotIn("model_json", row)
        self.ai.generate_text.assert_not_called()
        with self.repo.connection() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("DELETE FROM audio_files WHERE id='audio'")
            self.assertIsNone(db.execute("SELECT * FROM transcription_results").fetchone())
        self.process("我今天去了公园，然后回家。", "公园散步")
        self.assertEqual(self.repo.result(identifier)["title"], "公园散步")

    def test_success_has_only_independent_body_and_ai_title(self):
        identifier = self.service.create("audio")
        with self.assertRaises(ValueError):
            self.repo.result(identifier)
        self.process("我今天去了公园，然后回家。", "公园散步")
        result = self.repo.result(identifier)
        self.assertEqual(result["body"], "我今天去了公园，然后回家。")
        self.assertEqual(result["title"], "公园散步")
        self.assertEqual(set(result), {"id", "title", "body", "created_at"})
        self.assertEqual(self.raw(identifier)["source_text"], "")
        self.assertEqual(self.raw(identifier)["model_json"], "{}")
        instruction = self.ai.generate_text.call_args_list[0].args[1]
        self.assertEqual(instruction, OPTIMIZE_PROMPT)
        self.assertIn("不得总结", instruction)

    def test_empty_missing_or_unfinished_source_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.create("missing")
        with self.repo.connection() as db:
            db.execute("UPDATE transcription_tasks SET status='failed'")
        with self.assertRaises(ValueError):
            self.service.create("audio")
        with self.repo.connection() as db:
            db.execute("UPDATE transcription_tasks SET status='completed'")
            db.execute("UPDATE transcription_results SET text='   '")
        with self.assertRaises(ValueError):
            self.service.create("audio")
        self.assertEqual(self.repo.list_all(), [])

    def test_missing_default_does_not_create_a_job(self):
        self.ai.default_generation_profile.side_effect = ValueError("请设置默认模型")
        with self.assertRaises(ValueError):
            self.service.create("audio")
        self.assertEqual(self.repo.list_all(), [])

    def test_long_input_is_fully_processed_in_order(self):
        original = "我今天去了公园。" * 500
        with self.repo.connection() as db:
            db.execute("UPDATE transcription_results SET text=?", (original,))
        identifier = self.service.create("audio")
        chunks = split_transcript(original)
        self.process(*chunks, "公园散步")
        calls = self.ai.generate_text.call_args_list[:-1]
        payloads = [json.loads(call.args[2]) for call in calls]
        self.assertEqual("".join(item["transcript"] for item in payloads), original)
        self.assertEqual(payloads[0]["context"], "")
        self.assertEqual(payloads[1]["context"], chunks[0][-200:])
        self.assertEqual(self.repo.result(identifier)["body"], "\n\n".join(chunks))

    def test_split_keeps_every_character_and_bounds_unpunctuated_input(self):
        for text in ("", "字" * 10000, "内容。\n\n" * 999, "word. " * 999):
            chunks = split_transcript(text)
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(0 < len(chunk) <= 1800 for chunk in chunks))

    def test_title_excerpts_include_beginning_and_end(self):
        body = "开头" + "内容" * 4000 + "结尾"
        excerpts = json.loads(title_material(body))["excerpts"]
        self.assertTrue(excerpts[0].startswith("开头"))
        self.assertTrue(excerpts[-1].endswith("结尾"))
        self.assertLessEqual(sum(map(len, excerpts)), 3600)

    def test_failure_never_exposes_partial_body_or_raw_exception(self):
        identifier = self.service.create("audio")
        self.process(RuntimeError("private-provider-error-with-secret"))
        row = self.repo.list_all()[0]
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("secret", row["error"])
        with self.assertRaises(ValueError):
            self.repo.result(identifier)

    def test_title_retry_reuses_finished_body_and_binds_current_default(self):
        identifier = self.service.create("audio")
        self.process("整理后的正文。", ModelTestError("网络超时"))
        self.assertEqual(self.raw(identifier)["body"], "整理后的正文。")
        with self.assertRaises(ValueError):
            self.repo.result(identifier)
        self.ai.default_generation_profile.return_value = dict(self.profile, id="other-model")
        self.service.retry(identifier)
        self.assertEqual(json.loads(self.raw(identifier)["model_json"])["id"], "other-model")
        self.ai.generate_text.reset_mock()
        self.process("新标题")
        self.assertEqual(self.ai.generate_text.call_count, 1)
        self.assertEqual(self.repo.result(identifier)["body"], "整理后的正文。")

    def test_invalid_title_does_not_complete(self):
        identifier = self.service.create("audio")
        self.process("正文。", "标题\n还有说明")
        self.assertEqual(self.raw(identifier)["status"], "failed")

    def test_restart_marks_only_running_jobs_failed_and_does_not_repeat_requests(self):
        first = self.service.create("audio")
        second = self.service.create("audio")
        claimed = self.repo.claim()
        self.repo.recover()
        self.assertEqual(self.raw(claimed["id"])["status"], "failed")
        other = ({first, second} - {claimed["id"]}).pop()
        self.assertEqual(self.raw(other)["status"], "queued")
        self.ai.generate_text.assert_not_called()

    def test_only_failed_jobs_can_retry_and_active_jobs_cannot_delete(self):
        identifier = self.service.create("audio")
        for action in (self.service.retry, self.repo.delete):
            with self.assertRaises(ValueError):
                action(identifier)
        self.repo.claim()
        with self.assertRaises(ValueError):
            self.repo.delete(identifier)
        self.repo.fail(identifier, "失败")
        self.repo.delete(identifier)
        self.assertEqual(self.repo.list_all(), [])
        with self.repo.connection() as db:
            self.assertIsNotNone(db.execute("SELECT * FROM transcription_results").fetchone())

    def test_close_prevents_further_requests(self):
        identifier = self.service.create("audio")
        job = self.repo.claim()
        self.service.close()
        self.service._process(job)
        self.ai.generate_text.assert_not_called()
        self.assertEqual(self.raw(identifier)["status"], "failed")

    def test_non_owner_does_not_recover_or_claim_jobs(self):
        identifier = self.service.create("audio")
        self.repo.claim()
        with exclusive_file_lock(self.service._lock_path):
            with patch.object(self.service._stop, "wait", side_effect=lambda _: self.service._stop.set()):
                self.service._schedule()
        self.assertEqual(self.raw(identifier)["status"], "optimizing")
        self.ai.prepare_generation.assert_not_called()


class GenerationSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "sona.sqlite3"
        ModelRepository(self.database, ())
        self.ai = AIModelService(self.database)
        for name, value in (("_read_secret", "fake-secret"), ("_save_secret", None), ("_delete_secret", None)):
            patched = patch.object(self.ai, name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)
        self.identifier = self.ai.add_model("测试", "zhipu-coding", "glm-5.3-flash", api_key="fake-secret")[0]["id"]

    def ready(self):
        self.ai.repository.save_test_result(self.identifier, success=True)
        self.ai.select_model(self.identifier)
        return self.ai.default_generation_profile()

    def response(self, content="正文。", finish_reason="stop", native=None):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish_reason,
            provider_specific_fields={"native_finish_reason": native},
        )])

    def fake_completion(self, **kwargs):
        module = ModuleType("litellm")
        module.completion = Mock(**kwargs)
        return module

    def test_default_must_be_explicitly_selected_and_ready(self):
        with self.assertRaises(ValueError):
            self.ai.default_generation_profile()
        self.ready()
        self.ai.repository.save_test_result(self.identifier, success=False, error="failed")
        with self.assertRaises(ValueError):
            self.ai.default_generation_profile()

    def test_snapshot_has_no_secret_and_session_does_not_expose_it_in_repr(self):
        snapshot = self.ready()
        self.assertNotIn("fake-secret", json.dumps(snapshot))
        session = self.ai.prepare_generation(snapshot)
        self.assertNotIn("fake-secret", repr(session))
        self.assertEqual(session.api_key, "fake-secret")

    def test_model_change_or_deletion_does_not_silently_reroute(self):
        snapshot = self.ready()
        with self.ai.repository.connection() as db:
            db.execute("UPDATE ai_model_profiles SET model_name='changed' WHERE id=?", (self.identifier,))
        with self.assertRaises(ModelTestError):
            self.ai.prepare_generation(snapshot)
        with self.ai.repository.connection() as db:
            db.execute("DELETE FROM ai_model_profiles WHERE id=?", (self.identifier,))
        with self.assertRaises(ModelTestError):
            self.ai.prepare_generation(snapshot)

    def test_generation_uses_bound_session_and_no_automatic_retry(self):
        snapshot = self.ready()
        session = self.ai.prepare_generation(snapshot)
        module = self.fake_completion(return_value=self.response())
        with patch.dict("sys.modules", {"litellm": module}):
            self.assertEqual(self.ai.generate_text(session, "instruction", "payload"), "正文。")
        options = module.completion.call_args.kwargs
        self.assertEqual(options["model"], "zai/glm-5.3-flash")
        self.assertEqual(options["api_base"], "https://open.bigmodel.cn/api/coding/paas/v4")
        self.assertEqual(options["num_retries"], 0)
        self.assertEqual(options["max_tokens"], 8192)
        self.assertEqual(options["extra_body"]["reasoning_effort"], "low")

    def test_empty_truncated_blocked_or_tool_responses_cannot_complete(self):
        session = self.ai.prepare_generation(self.ready())
        for response in (self.response(""), self.response(finish_reason="length"),
                         self.response(finish_reason="stop", native="network_error"),
                         self.response(finish_reason="content_filter"),
                         self.response(finish_reason="tool_calls"), SimpleNamespace(choices=[])):
            with self.subTest(response=response):
                module = self.fake_completion(return_value=response)
                with patch.dict("sys.modules", {"litellm": module}), self.assertRaises(ModelTestError):
                    self.ai.generate_text(session, "instruction", "payload")

    def test_provider_error_is_sanitized(self):
        session = self.ai.prepare_generation(self.ready())
        module = self.fake_completion(side_effect=RuntimeError("fake-secret and private text"))
        with patch.dict("sys.modules", {"litellm": module}), self.assertRaises(ModelTestError) as error:
            self.ai.generate_text(session, "instruction", "payload")
        self.assertNotIn("fake-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
