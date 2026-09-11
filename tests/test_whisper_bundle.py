import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sona.models import BUILTIN_MODELS, ModelEvent
from sona.providers.whisper_bundle import WhisperBundleProvider


class WhisperBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.provider = WhisperBundleProvider(root / 'models', root / 'downloads')
        self.addCleanup(self.provider.close)
        self.contents = {'config.json': b'{}', 'model.bin': b'model bytes'}
        files = []
        for name, payload in self.contents.items():
            files.append({'name': name, 'size': len(payload),
                          'sha256': hashlib.sha256(payload).hexdigest() if name.endswith('.bin') else None,
                          'git_sha1': hashlib.sha1(f'blob {len(payload)}\0'.encode() + payload).hexdigest()})
        definition = next(model for model in BUILTIN_MODELS if model.bundle)
        self.model = replace(definition, bundle={'repo': 'test/model', 'revision': 'a' * 40, 'files': files})

    def transfer(self, artifact, partial, emit, cancelled):
        payload = self.contents[artifact.artifact_filename]
        partial.write_bytes(payload)
        emit(ModelEvent('downloading', 'test', downloaded_bytes=len(payload)))
        return len(payload), len(payload)

    def download(self):
        with patch.object(self.provider, '_transfer', side_effect=self.transfer):
            return self.provider.run('download', self.model, lambda _: None)

    def test_legacy_checkpoint_is_preserved_and_not_reported_installed(self):
        legacy = self.provider._legacy_paths(self.model)[0]
        legacy.mkdir(parents=True)
        artifact = legacy / 'small.pt'
        artifact.write_bytes(b'legacy')
        self.assertEqual(self.provider.run('status', self.model, lambda _: None).status, 'supported')
        self.assertEqual(self.download().status, 'installed')
        self.assertEqual(artifact.read_bytes(), b'legacy')

    def test_missing_bundle_file_invalidates_installation(self):
        self.assertEqual(self.download().status, 'installed')
        (self.provider._target_dir(self.model) / 'config.json').unlink()
        self.assertEqual(self.provider.run('status', self.model, lambda _: None).status, 'supported')

    def test_corrupt_download_cannot_be_promoted(self):
        self.contents['model.bin'] = b'wrong bytes'
        self.assertEqual(self.download().status, 'failed')
        self.assertFalse(self.provider._target_dir(self.model).exists())

    def test_model_lease_prevents_deletion(self):
        self.assertEqual(self.download().status, 'installed')
        with self.provider._model_lock(self.model):
            result = self.provider.run('delete', self.model, lambda _: None)
        self.assertEqual(result.status, 'locked')
        self.assertEqual(self.provider.run('status', self.model, lambda _: None).status, 'installed')
