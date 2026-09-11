"""Opt-in and recovery regressions; native probes and downloads are mocked."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sona.acceleration.catalog import BUNDLE
from sona.acceleration.download import transfer, verify_files
from sona.acceleration.service import AccelerationService, StateStore


class AccelerationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = AccelerationService.__new__(AccelerationService)
        self.service.root = self.root
        self.service.store = StateStore(self.root / 'state.sqlite3')
        self.service._closed = threading.Event()
        self.service._mutex = threading.Lock()
        self.service._thread = None
        self.hardware = {'kind': 'nvidia', 'device_index': 0, 'name': 'Test GPU', 'driver_required': False}

    def operate(self, action, previous):
        with patch.object(self.service, '_probe', return_value={'ok': True, 'hardware': self.hardware}), \
                patch('sona.acceleration.service.install') as install:
            self.service._operate(action, previous, Mock())
            install.assert_not_called()

    def test_first_start_only_detects_and_never_downloads(self):
        self.operate('startup', self.service.store.read())
        state = self.service.store.read()
        self.assertEqual(state['status'], 'available')
        self.assertFalse(state['enabled'])
        self.assertIsNone(self.service.runtime_for_task())

    def test_interrupted_download_requires_explicit_resume(self):
        cache = self.root / 'downloads' / BUNDLE
        cache.mkdir(parents=True)
        (cache / 'cudart.zip').write_bytes(b'partial')
        previous = self.service.store.patch(status='downloading', enabled=True)
        self.operate('startup', previous)
        self.assertEqual(self.service.store.read()['status'], 'paused')
        self.assertEqual(self.service.store.read()['downloaded_bytes'], 7)
        self.assertIsNone(self.service.runtime_for_task())

    def test_hardware_detection_error_is_not_reported_as_cpu_only(self):
        with patch.object(self.service, '_probe', return_value={'ok': False, 'error': 'driver initialization failed'}):
            self.service._operate('startup', self.service.store.read(), Mock())
        self.assertEqual(self.service.store.read()['status'], 'check_failed')

    def ready(self):
        path = self.root / f'{BUNDLE}-test'
        path.mkdir()
        self.service.store.patch(status='ready', enabled=True, runtime=path.name, token='current', hardware=self.hardware)
        return self.service.runtime_for_task()

    def test_disable_retains_files_and_existing_job_snapshot(self):
        running = self.ready()
        self.service.action('disable')
        self.assertTrue(Path(running['path']).is_dir())
        self.assertIsNone(self.service.runtime_for_task())
        self.assertFalse(self.service.store.read()['enabled'])

    def test_failure_suppresses_future_gpu_jobs_but_preserves_preference(self):
        runtime = self.ready()
        self.service.report_failure(runtime, 'cublas load failure')
        self.assertIsNone(self.service.runtime_for_task())
        self.assertTrue(self.service.store.read()['enabled'])
        self.assertEqual(self.service.store.read()['status'], 'check_failed')

    def test_old_job_failure_cannot_invalidate_new_check(self):
        runtime = self.ready()
        self.service.store.patch(token='new-check')
        self.service.report_failure(runtime, 'old failure')
        self.assertIsNotNone(self.service.runtime_for_task())

    def test_damaged_runtime_requires_download_instead_of_endless_recheck(self):
        runtime = self.ready()
        self.operate('check', self.service.store.read())
        self.assertEqual(self.service.store.read()['status'], 'download_failed')
        self.assertEqual(self.service.store.read()['runtime'], '')
        self.assertTrue(Path(runtime['path']).is_dir())

    def test_pending_verification_cannot_be_used_for_transcription(self):
        self.ready()
        self.service.store.patch(status='verifying')
        self.assertIsNone(self.service.runtime_for_task())

    def test_wrong_range_does_not_append_to_partial_archive(self):
        archive = self.root / 'partial.zip'
        archive.write_bytes(b'abc')
        response = Mock(status=206, headers={'Content-Range': 'bytes 0-8/9'})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch('sona.acceleration.download.urlopen', return_value=response):
            with self.assertRaises(ValueError):
                transfer(archive, 'test', 9, threading.Event(), Mock())
        self.assertEqual(archive.read_bytes(), b'abc')

    def test_installed_manifest_rejects_parent_paths(self):
        (self.root / 'manifest.json').write_text(json.dumps({
            'bundle': BUNDLE, 'files': {'../outside.dll': {'size': 1, 'sha256': 'invalid'},
                                      'nvrtc-builtins-test.dll': {'size': 0, 'sha256': 'unused'}},
        }), encoding='utf-8')
        with patch('sona.acceleration.download.REQUIRED_DLLS', ()):
            with self.assertRaises(ValueError):
                verify_files(self.root, threading.Event())
