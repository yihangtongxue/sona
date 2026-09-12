"""Local temporary files and fake legacy keyring only; run manually."""

import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from sona.ai_model_service import AIModelProfile, AIModelService
from sona.credentials import CredentialError, LOCAL_KEY_PREFIX, LocalCredentialStore
from sona.database import ModelRepository


class LocalCredentialTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = LocalCredentialStore(self.root / "credentials")
        self.reference = LOCAL_KEY_PREFIX + str(uuid.uuid4())

    def test_restart_encryption_and_owner_permissions(self):
        secret = "fake-api-key-本地加密"
        self.store.save(self.reference, secret)
        restarted = LocalCredentialStore(self.store.directory)
        self.assertEqual(restarted.read(self.reference), secret)
        for path in self.store.directory.iterdir():
            self.assertNotIn(secret.encode(), path.read_bytes())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)

    def test_ciphertexts_cannot_be_tampered_with_or_swapped(self):
        other = LOCAL_KEY_PREFIX + str(uuid.uuid4())
        self.store.save(self.reference, "first-fake-key")
        self.store.save(other, "second-fake-key")
        path = self.store._path(self.reference)
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaises(CredentialError):
            self.store.read(self.reference)
        path.write_bytes(self.store._path(other).read_bytes())
        with self.assertRaises(CredentialError):
            self.store.read(self.reference)

    def test_missing_master_is_not_recreated_over_existing_ciphertext(self):
        self.store.save(self.reference, "fake-key")
        master = self.store.master_path.read_bytes()
        self.store.master_path.unlink()
        with self.assertRaisesRegex(CredentialError, "解密密钥丢失"):
            self.store.read(self.reference)
        with self.assertRaisesRegex(CredentialError, "解密密钥丢失"):
            self.store.save(LOCAL_KEY_PREFIX + str(uuid.uuid4()), "replacement")
        self.assertFalse(self.store.master_path.exists())
        self.store.master_path.write_bytes(master)
        self.assertEqual(self.store.read(self.reference), "fake-key")

    def test_corrupt_master_is_not_replaced(self):
        self.store.save(self.reference, "fake-key")
        self.store.master_path.write_bytes(b"invalid")
        with self.assertRaises(CredentialError):
            self.store.read(self.reference)
        with self.assertRaises(CredentialError):
            self.store.save(self.reference, "replacement")
        self.assertEqual(self.store.master_path.read_bytes(), b"invalid")

    def test_failed_atomic_replace_keeps_previous_secret(self):
        self.store.save(self.reference, "original")
        with patch("sona.credentials.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(CredentialError):
                self.store.save(self.reference, "replacement")
        self.assertEqual(self.store.read(self.reference), "original")
        self.assertEqual(list(self.store.directory.glob(".credential-*")), [])

    def test_store_instances_share_master_without_losing_other_keys(self):
        self.store.save(self.reference, "first")
        master = self.store.master_path.read_bytes()
        other = LOCAL_KEY_PREFIX + str(uuid.uuid4())
        restarted = LocalCredentialStore(self.store.directory)
        restarted.save(other, "second")
        self.store.delete(self.reference)
        self.assertEqual(self.store.read(other), "second")
        self.assertEqual(self.store.master_path.read_bytes(), master)
        self.assertEqual(restarted.read(self.reference), "")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(CredentialError):
            self.store.save("../../outside", "fake-key")
        self.assertFalse((self.root / "outside").exists())

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires additional privileges")
    def test_master_and_directory_symlinks_are_rejected(self):
        target = self.root / "outside"
        target.write_bytes(b"unchanged")
        self.store.directory.mkdir()
        self.store.master_path.symlink_to(target)
        with self.assertRaises(CredentialError):
            self.store.save(self.reference, "fake-key")
        self.assertEqual(target.read_bytes(), b"unchanged")
        linked = self.root / "linked"
        linked.symlink_to(self.store.directory, target_is_directory=True)
        with self.assertRaises(CredentialError):
            LocalCredentialStore(linked).read(self.reference)


class LegacyCredentialMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "sona.sqlite3"
        ModelRepository(self.database, ())
        self.service = AIModelService(self.database)
        self.reference = str(uuid.uuid4())
        self.identifier = str(uuid.uuid4())
        self.service.repository.insert(AIModelProfile(
            self.identifier, "旧模型", "openai", "test-model", api_key_ref=self.reference,
        ))
        self.keyring = ModuleType("keyring")
        self.keyring.get_password = Mock(return_value="legacy-fake-key")
        self.keyring.set_password = Mock(side_effect=AssertionError("No keyring writes"))
        self.keyring.delete_password = Mock(side_effect=AssertionError("No keyring cleanup"))
        patched = patch.dict("sys.modules", {"keyring": self.keyring})
        patched.start()
        self.addCleanup(patched.stop)

    def test_legacy_migration_survives_restart_and_preserves_job_snapshot(self):
        self.service.repository.save_test_result(self.identifier, success=True)
        self.service.select_model(self.identifier)
        snapshot = self.service.default_generation_profile()
        self.keyring.get_password.assert_not_called()
        self.assertEqual(self.service.prepare_generation(snapshot).api_key, "legacy-fake-key")
        restarted = AIModelService(self.database)
        self.assertEqual(restarted.default_generation_profile(), snapshot)
        self.assertEqual(restarted.prepare_generation(snapshot).api_key, "legacy-fake-key")
        self.assertEqual(restarted.get_model_api_key(self.identifier), "legacy-fake-key")
        self.keyring.get_password.assert_called_once()
        self.keyring.delete_password.assert_not_called()

    def test_denied_migration_does_not_prompt_again_and_allows_replacement(self):
        self.keyring.get_password.side_effect = RuntimeError("private backend error")
        for _ in range(2):
            with self.assertRaises(CredentialError) as raised:
                self.service.get_model_api_key(self.identifier)
            self.assertNotIn("private backend error", str(raised.exception))
        self.service.update_model(self.identifier, "旧模型", "openai", "test-model",
                                  api_key="replacement-key")
        self.assertEqual(self.service.get_model_api_key(self.identifier), "replacement-key")
        self.keyring.get_password.assert_called_once()
        self.keyring.delete_password.assert_not_called()

    def test_replacement_and_cleanup_never_read_legacy_keyring(self):
        self.service.update_model(self.identifier, "旧模型", "openai", "test-model",
                                  api_key="replacement-key")
        self.service.delete_model(self.identifier)
        self.keyring.get_password.assert_not_called()
        self.keyring.delete_password.assert_not_called()

    def test_corrupted_migrated_key_does_not_fall_back_to_keyring(self):
        self.service.get_model_api_key(self.identifier)
        self.service._credentials._path(self.reference).write_bytes(b"tampered")
        with self.assertRaises(CredentialError):
            AIModelService(self.database).get_model_api_key(self.identifier)
        self.keyring.get_password.assert_called_once()

    def test_new_missing_key_never_uses_keyring(self):
        rows = self.service.add_model("新模型", "openai", "test-model", api_key="new-fake-key")
        identifier = next(row["id"] for row in rows if row["id"] != self.identifier)
        reference = self.service.repository.get_profile(identifier)["api_key_ref"]
        self.service._credentials.delete(reference)
        with self.assertRaises(CredentialError):
            self.service.get_model_api_key(identifier)
        self.keyring.get_password.assert_not_called()
