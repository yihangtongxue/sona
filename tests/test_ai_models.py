from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from sona.ai_model_service import AIModelProfile, AIModelService
from sona.database import ModelRepository


class AIModelManagementTests(unittest.TestCase):
    """Temporary SQLite and fake credentials only; no provider or keyring access."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "sona.sqlite3"
        ModelRepository(self.database, ())
        self.service = AIModelService(self.database)
        self.secrets = {}
        for method, implementation in (
            ("_read_secret", lambda ref: self.secrets.get(ref, "")),
            ("_save_secret", lambda ref, value: self.secrets.__setitem__(ref, value)),
            ("_delete_secret", lambda ref: self.secrets.pop(ref, None)),
        ):
            mock = patch.object(self.service, method, side_effect=implementation)
            mock.start()
            self.addCleanup(mock.stop)

    def add_model(self):
        existing = {row["id"] for row in self.service.list_models()}
        rows = self.service.add_model("测试模型", "openai", "test-model", api_key="fake-key")
        return next(row["id"] for row in rows if row["id"] not in existing)

    def update_model(self, identifier, **changes):
        values = dict(name="测试模型", provider="openai", model_name="test-model", api_key="fake-key")
        values.update(changes)
        return self.service.update_model(identifier, **values)

    def record(self, identifier):
        return self.service.repository.get_profile(identifier)

    def mark_ready(self, identifier):
        self.service.repository.save_test_result(identifier, success=True)

    def test_first_model_requires_manual_test_and_selection(self):
        identifier = self.add_model()
        row = self.service.list_models()[0]
        self.assertFalse(row["selected"])
        self.assertEqual(row["status"], "untested")
        with self.assertRaisesRegex(ValueError, "请先测试"):
            self.service.select_model(identifier)
        self.service.repository.save_test_result(identifier, success=False, error="测试失败")
        with self.assertRaisesRegex(ValueError, "请先测试"):
            self.service.repository.select(identifier)
        self.mark_ready(identifier)
        self.assertFalse(self.service.list_models()[0]["selected"])
        self.assertTrue(self.service.select_model(identifier)[0]["selected"])

    def test_default_rejects_edit_and_delete_without_writing_credentials(self):
        identifier = self.add_model()
        self.mark_ready(identifier)
        self.service.select_model(identifier)
        before = self.record(identifier)
        self.service._save_secret.reset_mock()
        with self.assertRaisesRegex(ValueError, "不能编辑"):
            self.update_model(identifier, api_key="replacement-key")
        with self.assertRaisesRegex(ValueError, "不能删除"):
            self.service.delete_model(identifier)
        self.service._save_secret.assert_not_called()
        self.assertEqual(before, self.record(identifier))
        profile = AIModelProfile(identifier, "新名称", "openai", "test-model", api_key_ref=before["api_key_ref"])
        with self.assertRaisesRegex(ValueError, "不能编辑"):
            self.service.repository.update(profile)

    def test_other_instance_selection_protects_stale_editor(self):
        identifier = self.add_model()
        self.mark_ready(identifier)
        other = AIModelService(self.database)
        other.repository.select(identifier)
        with self.assertRaisesRegex(ValueError, "不能编辑"):
            self.update_model(identifier)

    def test_previous_default_becomes_editable_after_manual_switch(self):
        first = self.add_model()
        second = self.add_model()
        self.mark_ready(first)
        self.mark_ready(second)
        self.service.select_model(first)
        self.service.select_model(second)
        self.update_model(first, name="改名")
        self.service.delete_model(first)
        rows = self.service.list_models()
        self.assertEqual([row["id"] for row in rows], [second])
        self.assertTrue(rows[0]["selected"])

    def test_rename_and_unchanged_save_preserve_test_and_credential(self):
        identifier = self.add_model()
        self.mark_ready(identifier)
        before = self.record(identifier)
        self.service._save_secret.reset_mock()
        self.service._delete_secret.reset_mock()
        for name in ("改名", "改名"):
            self.update_model(identifier, name=name)
            after = self.record(identifier)
            for field in ("status", "last_tested_at", "last_error", "api_key_ref"):
                self.assertEqual(before[field], after[field])
        self.service._save_secret.assert_not_called()
        self.service._delete_secret.assert_not_called()

    def test_rename_preserves_previous_failure(self):
        identifier = self.add_model()
        self.service.repository.save_test_result(identifier, success=False, error="权限不足")
        self.update_model(identifier, name="改名")
        self.assertEqual(self.record(identifier)["status"], "failed")
        self.assertEqual(self.record(identifier)["last_error"], "权限不足")

    def test_connection_changes_require_another_test(self):
        for changes in (
            {"provider": "anthropic"},
            {"model_name": "other-model"},
            {"base_url": "https://example.test/v1"},
            {"api_key": "replacement-key"},
            {"config": {"litellm_model": "openai/other-model"}},
        ):
            with self.subTest(changes=changes):
                identifier = self.add_model()
                self.mark_ready(identifier)
                old_ref = self.record(identifier)["api_key_ref"]
                self.update_model(identifier, **changes)
                record = self.record(identifier)
                self.assertEqual(record["status"], "untested")
                self.assertIsNone(record["last_tested_at"])
                with self.assertRaisesRegex(ValueError, "请先测试"):
                    self.service.select_model(identifier)
                if "api_key" in changes:
                    self.assertNotEqual(old_ref, record["api_key_ref"])
                    self.assertNotIn(old_ref, self.secrets)
                else:
                    self.assertEqual(old_ref, record["api_key_ref"])

    def test_empty_key_still_rejected_on_edit(self):
        identifier = self.add_model()
        before = self.record(identifier)
        with self.assertRaisesRegex(ValueError, "请填写 API Key"):
            self.update_model(identifier, api_key="  ")
        self.assertEqual(before, self.record(identifier))

    @staticmethod
    def response(content, finish_reason="stop"):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish_reason,
        )])

    def test_only_nonempty_text_passes_connection_test(self):
        identifier = self.add_model()
        fake_litellm = ModuleType("litellm")
        for response, expected in (
            (self.response("OK"), "ready"),
            (self.response("模型连接正常"), "ready"),
            (SimpleNamespace(choices=[]), "failed"),
            (self.response(None), "failed"),
            (self.response("   "), "failed"),
            (self.response(None, "length"), "failed"),
            (self.response("O", "length"), "failed"),
            (self.response("blocked", "content_filter"), "failed"),
            (self.response("blocked", "sensitive"), "failed"),
            (self.response("partial", "network_error"), "failed"),
            (self.response("partial", "model_context_window_exceeded"), "failed"),
        ):
            with self.subTest(response=response):
                fake_litellm.completion = Mock(return_value=response)
                with patch.dict("sys.modules", {"litellm": fake_litellm}):
                    self.service.test_model(identifier)
                row = self.record(identifier)
                self.assertEqual(row["status"], expected)
                self.assertEqual(bool(row["last_error"]), expected == "failed")
                self.assertFalse(self.service.list_models()[0]["selected"])

    def test_glm53_uses_documented_low_reasoning_without_disabling_thinking(self):
        for provider in ("zhipu", "zhipu-coding"):
            for model in ("glm-5.3-flash", "GLM-5.3"):
                with self.subTest(provider=provider, model=model):
                    identifier = self.add_model()
                    self.update_model(identifier, provider=provider, model_name=model)
                    fake_litellm = ModuleType("litellm")
                    fake_litellm.completion = Mock(return_value=self.response("OK"))
                    with patch.dict("sys.modules", {"litellm": fake_litellm}):
                        self.service.test_model(identifier)
                    fake_litellm.completion.assert_called_once()
                    kwargs = fake_litellm.completion.call_args.kwargs
                    self.assertEqual(kwargs["model"], f"zai/{model}")
                    self.assertEqual(kwargs["max_tokens"], 4096)
                    self.assertEqual(kwargs["timeout"], 45)
                    self.assertEqual(kwargs["num_retries"], 0)
                    self.assertEqual(kwargs["extra_body"], {
                        "thinking": {"type": "enabled"}, "reasoning_effort": "low",
                    })
                    expected_path = "/api/coding/paas/v4" if provider == "zhipu-coding" else "/api/paas/v4"
                    self.assertEqual(kwargs["api_base"], f"https://open.bigmodel.cn{expected_path}")
                    self.assertEqual(self.record(identifier)["status"], "ready")

    def test_vendor_options_do_not_leak_to_other_or_unknown_models(self):
        for provider, model, config in (
            ("openai", "test-model", {}),
            ("anthropic", "test-model", {}),
            ("google", "test-model", {}),
            ("openai-compatible", "glm-5.3-flash", {}),
            ("zhipu", "glm-4.7", {}),
            ("zhipu-coding", "unknown-model", {}),
            ("zhipu-coding", "glm-5.3-flash", {"litellm_model": "openai/other-model"}),
        ):
            with self.subTest(provider=provider, model=model, config=config):
                options = self.service._test_options({
                    "provider": provider, "model_name": model, "config": config,
                })
                self.assertEqual(options, {"max_tokens": 1024, "timeout": 30})

    def test_native_error_survives_litellm_finish_reason_normalization(self):
        identifier = self.add_model()
        response = self.response("partial", "stop")
        response.choices[0].provider_specific_fields = {"native_finish_reason": "network_error"}
        fake_litellm = ModuleType("litellm")
        fake_litellm.completion = Mock(return_value=response)
        with patch.dict("sys.modules", {"litellm": fake_litellm}):
            self.service.test_model(identifier)
        self.assertEqual(self.record(identifier)["status"], "failed")
        self.assertIn("供应商生成回复时发生异常", self.record(identifier)["last_error"])

    def test_truncated_reasoning_is_diagnosable_without_logging_secrets(self):
        identifier = self.add_model()
        response = self.response(None, "length")
        response.choices[0].message.reasoning_content = "private-reasoning"
        fake_litellm = ModuleType("litellm")
        fake_litellm.completion = Mock(return_value=response)
        with patch.dict("sys.modules", {"litellm": fake_litellm}):
            with self.assertLogs("sona.ai_model_service", level="INFO") as logs:
                self.service.test_model(identifier)
        fake_litellm.completion.assert_called_once()
        text = "\n".join(logs.output)
        self.assertIn("finish_reason=length", text)
        self.assertIn("text_chars=0", text)
        self.assertIn("reasoning_chars=17", text)
        self.assertIn("这不表示套餐额度耗尽", text)
        self.assertNotIn("private-reasoning", text)
        self.assertNotIn("fake-key", text)
        self.assertEqual(self.record(identifier)["status"], "failed")

    def test_other_provider_normalized_length_remains_a_failure(self):
        identifier = self.add_model()
        response = self.response("partial", "length")
        response.choices[0].provider_specific_fields = {"native_finish_reason": "max_tokens"}
        fake_litellm = ModuleType("litellm")
        fake_litellm.completion = Mock(return_value=response)
        with patch.dict("sys.modules", {"litellm": fake_litellm}):
            self.service.test_model(identifier)
        self.assertEqual(self.record(identifier)["status"], "failed")
        self.assertIn("单次测试输出达到上限", self.record(identifier)["last_error"])

    def test_failed_retest_does_not_silently_change_default(self):
        identifier = self.add_model()
        self.mark_ready(identifier)
        self.service.select_model(identifier)
        fake_litellm = ModuleType("litellm")
        fake_litellm.completion = Mock(return_value=self.response(None))
        with patch.dict("sys.modules", {"litellm": fake_litellm}):
            self.service.test_model(identifier)
        row = self.service.list_models()[0]
        self.assertTrue(row["selected"])
        self.assertEqual(row["status"], "failed")
        with self.assertRaisesRegex(ValueError, "不能编辑"):
            self.update_model(identifier)
