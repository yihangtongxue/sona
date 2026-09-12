import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from sona.diagnostics import DiagnosticFormatter, DiagnosticHandler, LOG_FILES, export_bundle
from sona.file_lock import exclusive_file_lock


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.logs = self.root / "logs"

    def record(self):
        return logging.LogRecord("sona.test", logging.ERROR, __file__, 21,
            "secret text %s %s %s", ("sk-do-not-export", "/Users/private/audio.mp3", "failed"), None,
            func="process")

    def test_formatter_excludes_messages_credentials_paths_and_exception_text(self):
        record = self.record()
        record.task_id = "untrusted private task name"
        try:
            raise ValueError("private transcript and credential")
        except ValueError:
            record.exc_info = sys.exc_info()
        text = DiagnosticFormatter().format(record)
        for private in ("secret text", "sk-do-not-export", "/Users/", "private transcript", "untrusted"):
            self.assertNotIn(private, text)
        data = json.loads(text)
        self.assertEqual(data["values"], ["[omitted]", "[omitted]", "failed"])
        self.assertEqual(data["exception"], "ValueError")
        self.assertEqual(data["function"], "process")

    def test_rotation_is_bounded_and_export_excludes_other_files(self):
        handler = DiagnosticHandler(self.logs)
        self.addCleanup(handler.close)
        with patch("sona.diagnostics.MAX_LOG_BYTES", 1024):
            for _ in range(30):
                handler.handle(self.record())
        self.assertTrue((self.logs / "sona.log.2").exists())
        for path in self.logs.glob("sona.log*"):
            self.assertIn(path.name, LOG_FILES)
            self.assertLessEqual(path.stat().st_size, 1024)
        (self.logs / "private.pem").write_text("never export")
        destination = self.root / "diagnostics.zip"
        export_bundle(self.logs, destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()), {*LOG_FILES, "info.json"})
            self.assertNotIn("sk-do-not-export", archive.read("sona.log").decode())

    def test_disk_error_does_not_break_application_logging(self):
        handler = DiagnosticHandler(self.logs)
        self.addCleanup(handler.close)
        with patch("sona.diagnostics.os.open", side_effect=OSError("disk full")):
            handler.handle(self.record())

    def test_log_export_lock_contention_is_actionable_and_preserves_destination(self):
        handler = DiagnosticHandler(self.logs)
        self.addCleanup(handler.close)
        handler.handle(self.record())
        destination = self.root / "diagnostics.zip"
        destination.write_bytes(b"previous export")
        with exclusive_file_lock(self.logs / ".write.lock"):
            with self.assertRaisesRegex(ValueError, "稍后重试"):
                export_bundle(self.logs, destination)
        self.assertEqual(destination.read_bytes(), b"previous export")

    def test_export_failure_keeps_previous_file_and_cleans_temporary_archive(self):
        handler = DiagnosticHandler(self.logs)
        self.addCleanup(handler.close)
        handler.handle(self.record())
        destination = self.root / "diagnostics.zip"
        destination.write_bytes(b"previous export")
        with patch("sona.diagnostics.zipfile.ZipFile", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "保存失败"):
                export_bundle(self.logs, destination)
        self.assertEqual(destination.read_bytes(), b"previous export")
        self.assertEqual(list(self.root.glob(".sona-logs-*")), [])
