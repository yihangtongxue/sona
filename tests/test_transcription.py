"""Storage/queue regressions; no real inference, downloads, or GUI startup."""

import base64
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sona.audio_library import AudioLibrary
from sona.database import ModelRepository
from sona.models import BUILTIN_MODELS, ModelEvent
from sona.transcription.repository import TaskRepository
from sona.transcription.service import TranscriptionService


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

    def test_apple_default_binds_new_and_waiting_imports(self):
        waiting = self.import_audio()
        apple = next(model for model in BUILTIN_MODELS if model.provider == 'apple-speech')
        self.models.save_result(apple, ModelEvent('installed', 'ready'), select=True)
        imported = self.import_audio()
        self.tasks.pending()
        for identifier in (waiting, imported):
            self.assertEqual(self.tasks.get(identifier)['model_id'], apple.id)
            self.assertEqual(self.tasks.get(identifier)['status'], 'queued')

    def test_retry_switches_between_whisper_and_apple(self):
        self.select_model()
        identifier = self.import_audio()
        apple = next(model for model in BUILTIN_MODELS if model.provider == 'apple-speech')
        self.models.save_result(apple, ModelEvent('installed', 'ready'), select=True)
        self.tasks.pending()
        self.assertEqual(self.tasks.get(identifier)['model_id'], self.model.id)
        self.tasks.cancel(identifier)
        self.tasks.retry(identifier)
        self.assertEqual(self.tasks.get(identifier)['model_id'], apple.id)
        self.tasks.cancel(identifier)
        self.select_model()
        self.tasks.retry(identifier)
        self.assertEqual(self.tasks.get(identifier)['model_id'], self.model.id)

    def test_apple_result_preserves_engine_and_timestamps(self):
        apple = next(model for model in BUILTIN_MODELS if model.provider == 'apple-speech')
        self.models.save_result(apple, ModelEvent('installed', 'ready'), select=True)
        identifier = self.import_audio()
        task = self.tasks.claim(identifier, 'apple-speech', 'system-macOS-26.5', apple.id)
        result = dict(self.result, language='zh_CN', device='Apple Speech · 系统引擎')
        self.tasks.complete(task, result)
        saved = TaskRepository(self.database).result(identifier)
        self.assertEqual(saved['engine'], 'apple-speech')
        self.assertEqual(saved['model_id'], apple.id)
        self.assertEqual(saved['segments'], result['segments'])
        self.assertEqual(saved['device'], result['device'])

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

    def test_completed_copy_is_removed_but_text_and_original_are_preserved(self):
        self.select_model()
        original = Path(self.temp.name) / '录音.wav'
        original.write_bytes(b'original audio')
        identifier = self.import_audio()
        self.tasks.complete(self.claim(identifier), self.result)
        self.audio.cleanup_completed()
        self.audio.cleanup_completed()  # Cleanup remains safe after an interruption/retry.
        row = self.audio.list_files()[0]
        self.assertFalse(row['available'])
        self.assertTrue(row['has_result'])
        self.assertEqual(row['transcription_status'], 'completed')
        self.assertEqual(original.read_bytes(), b'original audio')
        self.assertEqual(TaskRepository(self.database).result(identifier)['segments'], self.result['segments'])
        self.audio.delete_file(identifier)
        self.assertEqual(self.audio.list_files(), [])
        with self.assertRaises(ValueError):
            self.tasks.result(identifier)

    def test_cleanup_keeps_incomplete_and_unsaved_audio(self):
        self.select_model()
        for status in ('queued', 'waiting_model', 'transcribing', 'cancelling', 'cancelled', 'failed', 'completed'):
            with self.subTest(status=status):
                identifier = self.import_audio()
                with self.tasks.connection() as db:
                    db.execute('UPDATE transcription_tasks SET status=? WHERE audio_id=?', (status, identifier))
                self.audio.cleanup_completed()
                # Even a completed flag alone is insufficient: a saved result is required.
                row = next(row for row in self.audio.list_files() if row['id'] == identifier)
                self.assertTrue(row['available'])
                self.assertFalse(row['has_result'])

    def test_cancelled_late_result_does_not_release_audio(self):
        self.select_model()
        identifier = self.import_audio()
        task = self.claim(identifier)
        self.tasks.cancel(identifier)
        self.tasks.complete(task, self.result)
        self.tasks.settle_cancel(identifier)
        self.audio.cleanup_completed()
        self.assertTrue(self.audio.list_files()[0]['available'])
        self.tasks.retry(identifier)
        self.assertIsNotNone(self.claim(identifier))

    def test_result_transaction_failure_keeps_audio_for_retry(self):
        self.select_model()
        identifier = self.import_audio()
        task = self.claim(identifier)
        with self.tasks.connection() as db:
            db.execute("""CREATE TRIGGER reject_completed BEFORE UPDATE ON transcription_tasks
                WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.tasks.complete(task, self.result)
        self.audio.cleanup_completed()
        self.assertTrue(self.audio.list_files()[0]['available'])
        with self.assertRaises(ValueError):
            self.tasks.result(identifier)

    def test_failed_cleanup_retries_after_reopen_without_losing_result(self):
        self.select_model()
        identifier = self.import_audio()
        self.tasks.complete(self.claim(identifier), self.result)
        unlink = Path.unlink

        def refuse_copy(path, *args, **kwargs):
            if path.name == f'{identifier}.wav':
                raise PermissionError('file in use')
            return unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', refuse_copy):
            self.audio.cleanup_completed()
        self.assertTrue(self.audio.list_files()[0]['available'])
        self.assertEqual(self.tasks.get(identifier)['status'], 'completed')
        self.assertEqual(self.tasks.result(identifier)['text'], self.result['text'])
        reopened = AudioLibrary(self.database, Path(self.temp.name) / 'audio')
        self.addCleanup(reopened.close)
        reopened.cleanup_completed()
        self.assertFalse(reopened.list_files()[0]['available'])
        self.assertEqual(self.tasks.result(identifier)['text'], self.result['text'])

    def test_cleanup_waits_for_import_owned_by_another_instance(self):
        self.select_model()
        identifier = self.import_audio()
        self.tasks.complete(self.claim(identifier), self.result)
        other = AudioLibrary(self.database, Path(self.temp.name) / 'audio')
        self.addCleanup(other.close)
        pending = other.begin_import('new.wav', 3)
        self.audio.cleanup_completed()
        self.assertTrue(self.audio.list_files()[0]['available'])
        other.abort_import(pending)
        self.audio.cleanup_completed()
        self.assertFalse(self.audio.list_files()[0]['available'])

    def test_silent_success_also_releases_copy(self):
        self.select_model()
        identifier = self.import_audio()
        self.tasks.complete(self.claim(identifier), dict(self.result, text='', segments=[]))
        self.audio.cleanup_completed()
        self.assertFalse(self.audio.list_files()[0]['available'])
        self.assertEqual(self.tasks.result(identifier)['text'], '')


class AudioCleanupSchedulingTests(unittest.TestCase):
    def test_cleanup_runs_on_queue_recovery_and_after_task_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TranscriptionService.__new__(TranscriptionService)
            service._paths = SimpleNamespace(data_dir=Path(directory))
            service._closed = threading.Event()
            events = []
            service.repository = Mock()
            service.repository.recover.side_effect = lambda: events.append('recover')
            service._audio_library = Mock()
            service._audio_library.cleanup_completed.side_effect = lambda: events.append('cleanup')

            def complete_task():
                events.append('worker-exited-and-result-saved')
                service._closed.set()
                return True

            service._next = Mock(side_effect=complete_task)
            service._supervise()
            self.assertEqual(events, ['recover', 'cleanup', 'worker-exited-and-result-saved', 'cleanup'])

    def test_cleanup_failure_does_not_change_task_state(self):
        service = TranscriptionService.__new__(TranscriptionService)
        service.repository = Mock()
        service._audio_library = Mock()
        service._audio_library.cleanup_completed.side_effect = OSError('temporary failure')
        with self.assertLogs('sona.transcription.service', level='ERROR'):
            service._cleanup_completed_audio()
        self.assertEqual(service.repository.mock_calls, [])
