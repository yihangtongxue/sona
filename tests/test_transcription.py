"""Storage/queue regressions; no real inference, downloads, or GUI startup."""

import base64
import tempfile
import unittest
from pathlib import Path

from sona.audio_library import AudioLibrary
from sona.database import ModelRepository
from sona.models import BUILTIN_MODELS, ModelEvent
from sona.transcription.repository import TaskRepository


class TranscriptionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / 'sona.sqlite3'
        self.models = ModelRepository(self.database, BUILTIN_MODELS)
        self.audio = AudioLibrary(self.database, root / 'audio')
        self.addCleanup(self.audio.close)
        self.tasks = TaskRepository(self.database)
        self.model = next(model for model in BUILTIN_MODELS if model.bundle)
        self.result = {'text': '你好。', 'segments': [{'start': 0.0, 'end': 1.5, 'text': '你好。'}],
                       'language': 'zh', 'duration': 2.0, 'device': 'CPU'}

    def import_audio(self):
        identifier = self.audio.begin_import('录音.wav', 3)
        self.audio.append_chunk(identifier, 0, base64.b64encode(b'abc').decode())
        self.audio.finish_import(identifier)
        return identifier

    def select_model(self):
        self.models.save_result(self.model, ModelEvent('installed', 'ready'), select=True)

    def claim(self, identifier):
        self.tasks.pending()
        return self.tasks.claim(identifier, 'faster-whisper', 'revision', self.tasks.get(identifier)['model_id'])

    def test_import_and_queue_are_committed_together(self):
        self.select_model()
        identifier = self.import_audio()
        task = self.tasks.get(identifier)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['model_id'], self.model.id)
        self.assertEqual(len(self.tasks.pending()), 1)

    def test_unconfigured_import_waits_then_binds_default(self):
        identifier = self.import_audio()
        self.assertIsNone(self.tasks.get(identifier)['model_id'])
        self.select_model()
        self.tasks.pending()
        self.assertEqual(self.tasks.get(identifier)['model_id'], self.model.id)

    def test_later_selection_does_not_change_bound_task(self):
        self.select_model()
        identifier = self.import_audio()
        other = next(model for model in BUILTIN_MODELS if model.bundle and model.id != self.model.id)
        self.models.save_result(other, ModelEvent('installed', 'ready'), select=True)
        self.tasks.pending()
        self.assertEqual(self.tasks.get(identifier)['model_id'], self.model.id)

    def test_stale_queue_snapshot_cannot_claim_a_different_retry_model(self):
        self.select_model()
        identifier = self.import_audio()
        self.tasks.cancel(identifier)
        other = next(model for model in BUILTIN_MODELS if model.bundle and model.id != self.model.id)
        self.models.save_result(other, ModelEvent('installed', 'ready'), select=True)
        self.tasks.retry(identifier)
        self.assertIsNone(self.tasks.claim(identifier, 'faster-whisper', 'old-revision', self.model.id))
        self.assertEqual(self.tasks.get(identifier)['model_id'], other.id)

    def test_cancel_wins_over_late_result(self):
        self.select_model()
        identifier = self.import_audio()
        task = self.claim(identifier)
        self.tasks.cancel(identifier)
        self.tasks.complete(task, self.result)
        self.tasks.settle_cancel(identifier)
        self.assertEqual(self.tasks.get(identifier)['status'], 'cancelled')
        with self.assertRaises(ValueError):
            self.tasks.result(identifier)

    def test_old_attempt_cannot_overwrite_retry(self):
        self.select_model()
        identifier = self.import_audio()
        first = self.claim(identifier)
        self.tasks.update(first, 'failed', 'failure')
        self.tasks.retry(identifier)
        second = self.claim(identifier)
        self.tasks.complete(first, self.result)
        self.assertEqual(self.tasks.get(identifier)['status'], 'transcribing')
        self.tasks.complete(second, self.result)
        self.assertEqual(self.tasks.result(identifier)['segments'], self.result['segments'])

    def test_running_audio_cannot_be_deleted_until_cancel_settles(self):
        self.select_model()
        identifier = self.import_audio()
        self.claim(identifier)
        with self.assertRaises(RuntimeError):
            self.audio.delete_file(identifier)
        self.tasks.cancel(identifier)
        with self.assertRaises(RuntimeError):
            self.audio.delete_file(identifier)
        self.tasks.settle_cancel(identifier)
        self.audio.delete_file(identifier)
        with self.assertRaises(ValueError):
            self.tasks.get(identifier)

    def test_completed_result_survives_reopen_and_cascades_on_delete(self):
        self.select_model()
        identifier = self.import_audio()
        self.tasks.complete(self.claim(identifier), self.result)
        reopened = TaskRepository(self.database)
        self.assertEqual(reopened.result(identifier)['text'], '你好。')
        self.assertTrue(self.audio.list_files()[0]['has_result'])
        self.audio.delete_file(identifier)
        with self.assertRaises(ValueError):
            reopened.result(identifier)

    def test_recovery_requeues_interrupted_task_without_affecting_completed(self):
        self.select_model()
        first = self.import_audio()
        second = self.import_audio()
        self.tasks.complete(self.claim(first), self.result)
        self.claim(second)
        self.tasks.recover()
        self.assertEqual(self.tasks.get(first)['status'], 'completed')
        self.assertEqual(self.tasks.get(second)['status'], 'queued')

    def test_partial_import_never_creates_task(self):
        identifier = self.audio.begin_import('录音.wav', 6)
        self.audio.append_chunk(identifier, 0, base64.b64encode(b'abc').decode())
        self.audio.abort_import(identifier)
        with self.assertRaises(ValueError):
            self.tasks.get(identifier)
