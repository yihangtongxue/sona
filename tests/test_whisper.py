from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from sona.models import BUILTIN_MODELS
from sona.providers.whisper import WhisperProvider


class Response(io.BytesIO):
    def __init__(self, payload, status=200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(payload))}

    def getcode(self):
        return self.status


class WhisperStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.payload = b"test whisper checkpoint"
        source = self.root / "source.pt"
        source.write_bytes(self.payload)
        definition = next(model for model in BUILTIN_MODELS if model.id == "whisper-small")
        self.model = replace(
            definition, artifact_url=source.as_uri(), artifact_filename="checkpoint.pt",
            artifact_sha256=sha256(self.payload).hexdigest(), artifact_version="test",
        )
        self.provider = WhisperProvider(self.root / "models", self.root / "downloads")
        self.addCleanup(self.provider.close)

    def partial(self, payload):
        partial = self.root / "downloads" / f"{self.model.id}.partial" / "checkpoint.pt"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(payload)
        return partial

    def test_download_status_and_delete(self):
        events = []
        installed = self.provider.run("download", self.model, events.append)
        self.assertEqual(installed.status, "installed")
        self.assertTrue(Path(installed.resource_path).joinpath("checkpoint.pt").is_file())
        self.assertEqual(events[-1].status, "verifying")
        status = self.provider.run("status", self.model, events.append)
        self.assertEqual(status.status, "installed")
        self.assertEqual(status.downloaded_bytes, len(self.payload))
        deleted = self.provider.run("delete", self.model, events.append)
        self.assertEqual(deleted.status, "supported")
        self.assertFalse(deleted.has_files)
        self.assertFalse(Path(installed.resource_path).exists())

    def test_resume_valid_http_range(self):
        self.partial(self.payload[:5])
        response = Response(self.payload[5:], 206, {
            "Content-Length": str(len(self.payload) - 5),
            "Content-Range": f"bytes 5-{len(self.payload) - 1}/{len(self.payload)}",
        })
        with patch("sona.providers.whisper.urllib.request.urlopen", return_value=response) as request:
            result = self.provider.run("download", self.model, lambda event: None)
        self.assertEqual(result.status, "installed")
        self.assertEqual(request.call_args.args[0].get_header("Range"), "bytes=5-")
        self.assertEqual(Path(result.resource_path, "checkpoint.pt").read_bytes(), self.payload)

    def test_invalid_range_preserves_partial(self):
        partial = self.partial(self.payload[:5])
        response = Response(self.payload[5:], 206, {
            "Content-Length": str(len(self.payload) - 5),
            "Content-Range": f"bytes 0-{len(self.payload) - 6}/{len(self.payload)}",
        })
        with patch("sona.providers.whisper.urllib.request.urlopen", return_value=response):
            result = self.provider.run("download", self.model, lambda event: None)
        self.assertEqual(result.status, "failed")
        self.assertEqual(partial.read_bytes(), self.payload[:5])

    def test_server_ignoring_range_restarts_without_appending(self):
        self.partial(b"old bytes")
        with patch("sona.providers.whisper.urllib.request.urlopen", return_value=Response(self.payload)):
            result = self.provider.run("download", self.model, lambda event: None)
        self.assertEqual(result.status, "installed")
        self.assertEqual(Path(result.resource_path, "checkpoint.pt").read_bytes(), self.payload)

    def test_truncated_response_preserves_download_for_retry(self):
        partial = self.partial(b"")
        response = Response(self.payload[:5], headers={"Content-Length": str(len(self.payload))})
        with patch("sona.providers.whisper.urllib.request.urlopen", return_value=response):
            result = self.provider.run("download", self.model, lambda event: None)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.has_files)
        self.assertEqual(partial.read_bytes(), self.payload[:5])

    def test_completed_partial_handles_http_416(self):
        self.partial(self.payload)
        error = urllib.error.HTTPError(self.model.artifact_url, 416, "range", {}, None)
        with patch("sona.providers.whisper.urllib.request.urlopen", side_effect=error):
            result = self.provider.run("download", self.model, lambda event: None)
        self.assertEqual(result.status, "installed")

    def test_checksum_mismatch_cannot_install(self):
        partial = self.partial(b"")
        with patch("sona.providers.whisper.urllib.request.urlopen", return_value=Response(b"corrupt")):
            result = self.provider.run("download", self.model, lambda event: None)
        self.assertEqual(result.status, "failed")
        self.assertFalse(partial.exists())
        self.assertNotEqual(self.provider.run("status", self.model, lambda event: None).status, "installed")

    def test_pause_during_verification_preserves_file_and_allows_retry(self):
        def emit(event):
            if event.status == "verifying":
                self.provider.cancel(self.model.id)

        result = self.provider.run("download", self.model, emit)
        self.assertEqual(result.status, "paused")
        self.assertEqual(result.downloaded_bytes, len(self.payload))
        self.assertTrue(result.has_files)
        self.assertEqual(self.provider.run("download", self.model, lambda event: None).status, "installed")

    def test_pause_after_finished_task_does_not_cancel_next_download(self):
        self.provider.cancel(self.model.id)
        self.assertEqual(self.provider.run("download", self.model, lambda event: None).status, "installed")

    def test_close_stops_download_before_network_request(self):
        self.provider.close()
        with patch("sona.providers.whisper.urllib.request.urlopen") as request:
            result = self.provider.run("download", self.model, lambda event: None)
        self.assertEqual(result.status, "paused")
        request.assert_not_called()

    def test_partial_files_can_be_detected_and_deleted(self):
        partial = self.partial(self.payload[:5])
        status = self.provider.run("status", self.model, lambda event: None)
        self.assertTrue(status.has_files)
        self.assertEqual(status.downloaded_bytes, 5)
        result = self.provider.run("delete", self.model, lambda event: None)
        self.assertEqual(result.status, "supported")
        self.assertFalse(partial.parent.exists())

    def test_delete_permission_error_is_not_reported_as_success(self):
        partial = self.partial(self.payload[:5])
        with patch("sona.providers.whisper.shutil.rmtree", side_effect=PermissionError("file is busy")):
            result = self.provider.run("delete", self.model, lambda event: None)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.has_files)
        self.assertTrue(partial.exists())
        self.assertIn("PermissionError", result.error)

    def test_stale_legacy_lock_file_does_not_block_download(self):
        downloads = self.root / "downloads"
        downloads.mkdir()
        (downloads / f"{self.model.id}.lock").write_text('{"pid": 123, "started_at": 0}')
        self.assertEqual(self.provider.run("download", self.model, lambda event: None).status, "installed")

    def test_active_lock_blocks_another_provider_then_releases(self):
        (self.root / "downloads").mkdir()
        other = WhisperProvider(self.root / "models", self.root / "downloads")
        self.addCleanup(other.close)
        with self.provider._model_lock(self.model):
            self.assertEqual(other.run("download", self.model, lambda event: None).status, "locked")
            self.assertEqual(other.run("delete", self.model, lambda event: None).status, "locked")
        self.assertEqual(other.run("download", self.model, lambda event: None).status, "installed")

    def test_process_death_releases_lock_without_finally(self):
        (self.root / "downloads").mkdir()
        script = """
import os, sys
from pathlib import Path
from sona.models import BUILTIN_MODELS
from sona.providers.whisper import WhisperProvider
provider = WhisperProvider(Path(sys.argv[1]), Path(sys.argv[2]))
model = next(model for model in BUILTIN_MODELS if model.id == 'whisper-small')
with provider._model_lock(model):
    os._exit(0)
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        subprocess.run(
            [sys.executable, "-c", script, str(self.root / "models"), str(self.root / "downloads")],
            env=environment, check=True, timeout=10, capture_output=True,
        )
        self.assertEqual(self.provider.run("download", self.model, lambda event: None).status, "installed")


if __name__ == "__main__":
    unittest.main()
