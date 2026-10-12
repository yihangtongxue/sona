import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sona.languages import normalize_language
from sona.manuscript_export import manuscript_filename, render, write_export
from sona.transcription.service import TranscriptionService
from sona.transcription_settings import TranscriptionSettings

DOC = {"name": "访谈", "text": "你好世界", "language": "zh", "duration": 3725.2,
       "segments": [{"start": 0, "end": 1.5, "text": "你好"}, {"start": 3723.25, "end": 3725.2, "text": " 世界 "},
                    {"start": 3725.2, "end": 3725.2, "text": "  "}]}


class ExportTests(unittest.TestCase):
    def test_srt_uses_segment_timestamps(self):
        self.assertEqual(render(DOC, "srt"), "1\n00:00:00,000 --> 00:00:01,500\n你好\n\n"
                                            "2\n01:02:03,250 --> 01:02:05,200\n世界\n")

    def test_srt_without_segments_is_one_cue_and_empty_is_rejected(self):
        self.assertIn("00:00:00,000 --> 00:00:09,000\n全文", render({"text": "全文", "duration": 9}, "srt"))
        with self.assertRaisesRegex(ValueError, "没有可导出"):
            render({"text": " "}, "srt")

    def test_markdown_for_transcript_and_manuscript(self):
        markdown = render(DOC, "markdown")
        self.assertTrue(markdown.startswith("# 访谈\n\n- 语言：zh\n- 时长：01:02:05\n"))
        self.assertIn("**[01:02:03]** 世界", markdown)
        self.assertEqual(render({"title": "标题", "body": "## 小节\n正文"}, "md"), "# 标题\n\n## 小节\n正文\n")

    def test_formats_and_suffixes(self):
        self.assertEqual(manuscript_filename("a/b", "srt"), "a_b.srt")
        with self.assertRaisesRegex(ValueError, "导出格式"):
            render(DOC, "docx")
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "x.md"
            write_export("# hi\n", target, "md")
            self.assertEqual(target.read_text(), "# hi\n")
            with self.assertRaisesRegex(ValueError, r"\.srt"):
                write_export("x", Path(root) / "x.txt", "srt")


class LanguageTests(unittest.TestCase):
    def test_normalize(self):
        for value, expected in ((None, "auto"), ("", "auto"), ("AUTO", "auto"), ("zh-CN", "zh"), ("En", "en"), ("yue", "yue")):
            self.assertEqual(normalize_language(value), expected)
        for value in ("xx", 3):
            with self.assertRaises(ValueError):
                normalize_language(value)

    def test_settings_persist_and_service_resolves_override_first(self):
        with tempfile.TemporaryDirectory() as root:
            settings = TranscriptionSettings(Path(root))
            self.assertEqual(settings.get_language(), "auto")
            settings.set_language("ja")
            self.assertEqual(TranscriptionSettings(Path(root)).get_language(), "ja")
            service = TranscriptionService.__new__(TranscriptionService)
            service.repository = Mock()
            service._language_settings = settings
            service.repository.language.return_value = None
            self.assertEqual(service.language_for("a"), "ja")
            service.repository.language.return_value = "en"
            self.assertEqual(service.language_for("a"), "en")
            settings.set_language("auto")
            service.repository.language.return_value = None
            self.assertIsNone(service.language_for("a"))

    def test_worker_passes_language_to_mlx_whisper(self):
        from sona.transcription import worker

        fake = types.SimpleNamespace(transcribe=Mock(return_value={"text": "hi", "segments": [], "language": "en"}))
        mlx_core = types.SimpleNamespace(set_default_device=Mock(), gpu="gpu")
        mlx = types.ModuleType("mlx")
        mlx.core = mlx_core
        with patch.dict(sys.modules, {"mlx_whisper": fake, "mlx": mlx, "mlx.core": mlx_core}):
            worker._mlx([0.0] * 16000, Path("/m"), lambda *_: None, "en")
        self.assertEqual(fake.transcribe.call_args.kwargs["language"], "en")


if __name__ == "__main__":
    unittest.main()
