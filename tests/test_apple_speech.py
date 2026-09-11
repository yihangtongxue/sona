"""Native protocol and engine routing regressions; no Swift/ASR execution."""

import io
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sona.models import BUILTIN_MODELS, ModelEvent
from sona.providers.apple_speech import _parse_event
from sona.transcription.apple import TranscriptEvents, transcribe
from sona.transcription.service import TranscriptionService, _signal_native_group


APPLE = next(model for model in BUILTIN_MODELS if model.provider == 'apple-speech')


def event(kind, **values):
    return {'protocol_version': 2, 'kind': kind, **values}


class AppleProtocolTests(unittest.TestCase):
    def test_installed_assets_from_old_helper_do_not_enable_selection(self):
        state = _parse_event(json.dumps({'status': 'installed', 'detail': 'ready'}))
        self.assertEqual(state.status, 'unknown')
        self.assertIn('版本', state.detail)
        state = _parse_event(json.dumps({'protocol_version': 2, 'status': 'installed', 'detail': 'ready'}))
        self.assertEqual(state.status, 'installed')

    def test_partial_segments_require_a_terminal_result(self):
        events = TranscriptEvents(3, Mock())
        events.accept(event('segment', start=0.25, end=1.5, text='你好。'))
        self.assertIsNone(events.result)
        events.accept(event('segment', start=1.5, end=2.5, text='再见。'))
        events.accept(event('result', duration=3, language='zh_CN'))
        self.assertEqual(events.result['text'], '你好。再见。')
        self.assertEqual(events.result['segments'][0]['start'], 0.25)
        with self.assertRaises(ValueError):
            events.accept(event('segment', start=2.5, end=3, text='重复结果'))

    def test_empty_speech_is_a_valid_completed_result(self):
        events = TranscriptEvents(3, Mock())
        events.accept(event('result', duration=3, language='zh_CN'))
        self.assertEqual(events.result['text'], '')
        self.assertEqual(events.result['segments'], [])

    def test_invalid_timestamps_and_durations_are_rejected(self):
        for start, end in ((float('nan'), 1), (0, float('inf')), (True, 1), (-1, 1), (2, 1), (0, 100)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                TranscriptEvents(3, Mock()).accept(event('segment', start=start, end=end, text='内容'))
        with self.assertRaises(ValueError):
            TranscriptEvents(3, Mock()).accept(event('result', duration=8, language='zh_CN'))

    def test_native_failure_never_completes_partial_results(self):
        events = TranscriptEvents(3, Mock())
        events.accept(event('segment', start=0, end=1, text='部分内容'))
        with self.assertRaisesRegex(RuntimeError, '资源不可用'):
            events.accept(event('error', detail='资源不可用'))
        self.assertIsNone(events.result)

    def test_helper_eof_or_nonzero_exit_does_not_save_partial_text(self):
        for terminal, code in ((False, 0), (True, 1)):
            with self.subTest(terminal=terminal, code=code), tempfile.TemporaryDirectory() as directory:
                messages = [event('segment', start=0, end=1, text='部分内容')]
                if terminal:
                    messages.append(event('result', duration=3, language='zh_CN'))
                process = Mock(stdout=io.StringIO('\n'.join(json.dumps(item) for item in messages)))
                process.wait.return_value = code
                process.poll.return_value = code
                with patch('sona.transcription.apple._helper_command', return_value=['helper']), \
                     patch('sona.transcription.apple._decode_to_wave', return_value=3), \
                     patch('sona.transcription.apple.subprocess.Popen', return_value=process), \
                     patch('sona.transcription.apple.threading.Timer'), \
                     self.assertRaises(RuntimeError):
                    transcribe(Path('audio.wav'), Path(directory), 'zh-CN', Mock())


class AppleRoutingTests(unittest.TestCase):
    def test_system_model_routes_without_a_whisper_bundle_or_model_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TranscriptionService.__new__(TranscriptionService)
            service._closed = threading.Event()
            service._paths = SimpleNamespace(audio_dir=Path(directory))
            service._models = {APPLE.id: APPLE}
            provider = Mock()
            provider.run.return_value = ModelEvent('installed', 'ready')
            service._providers = {'apple-speech': provider, 'whisper': Mock()}
            identifier = str(uuid.uuid4())
            record = {'audio_id': identifier, 'model_id': APPLE.id, 'suffix': '.wav'}
            task = dict(record, attempt=1, engine='apple-speech', model_revision='system-macOS-26.5')
            service.repository = Mock()
            service.repository.pending.return_value = [record]
            service.repository.claim.return_value = task
            scratch = []

            def execute(claimed, source, model_path, locale, work_dir):
                self.assertEqual(claimed['engine'], 'apple-speech')
                self.assertIsNone(model_path)
                self.assertEqual(locale, 'zh-CN')
                self.assertTrue(Path(work_dir).is_dir())
                scratch.append(Path(work_dir))

            service._execute = Mock(side_effect=execute)
            with patch('sona.transcription.service.platform.mac_ver', return_value=('26.5', (), '')), \
                 patch('sona.transcription.service.engine_supported', return_value=False):
                self.assertTrue(service._next())
            service.repository.claim.assert_called_once_with(identifier, 'apple-speech', 'system-macOS-26.5', APPLE.id)
            service._providers['whisper'].run.assert_not_called()
            self.assertFalse(scratch[0].exists())

    def test_cancel_targets_native_process_group_and_handles_start_race(self):
        with patch('sona.transcription.service.sys.platform', 'darwin'), \
             patch('sona.transcription.service.os.killpg', create=True) as killpg:
            _signal_native_group(1234, 15)
            killpg.assert_called_once_with(1234, 15)
            killpg.side_effect = ProcessLookupError
            _signal_native_group(1234, 15)


if __name__ == '__main__':
    unittest.main()
