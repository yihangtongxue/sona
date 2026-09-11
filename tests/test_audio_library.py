import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sona.audio_library import AudioLibrary
from sona.database import ModelRepository


class AudioLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / "sona.sqlite3"
        self.audio = root / "audio"
        ModelRepository(self.database, ())
        self.library = AudioLibrary(self.database, self.audio)
        self.addCleanup(self.library.close)

    def import_file(self, name="recording.wav", data=b"sample audio"):
        identifier = self.library.begin_import(name, len(data))
        self.library.append_chunk(identifier, 0, base64.b64encode(data).decode("ascii"))
        self.library.finish_import(identifier)
        return identifier

    def test_import_is_only_listed_after_commit_and_survives_reopen(self):
        identifier = self.library.begin_import("recording.wav", 6)
        self.library.append_chunk(identifier, 0, "YWJj")
        self.assertEqual(self.library.list_files(), [])
        self.library.append_chunk(identifier, 3, "ZGVm")
        self.library.finish_import(identifier)
        self.library.close()
        reopened = AudioLibrary(self.database, self.audio)
        self.addCleanup(reopened.close)
        records = reopened.list_files()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "recording.wav")
        self.assertTrue(records[0]["available"])
        self.assertEqual((self.audio / f"{identifier}.wav").read_bytes(), b"abcdef")

    def test_incorrect_offset_and_incomplete_commit_are_rejected(self):
        identifier = self.library.begin_import("recording.wav", 6)
        with self.assertRaises(ValueError):
            self.library.append_chunk(identifier, 3, "YWJj")
        with self.assertRaises(ValueError):
            self.library.finish_import(identifier)
        self.library.abort_import(identifier)
        self.assertEqual(self.library.list_files(), [])
        self.assertFalse((self.audio / ".pending.audio").exists())

    def test_cancel_releases_lock_for_another_instance(self):
        identifier = self.library.begin_import("recording.wav", 3)
        other = AudioLibrary(self.database, self.audio)
        self.addCleanup(other.close)
        with self.assertRaises(RuntimeError):
            other.begin_import("other.wav", 3)
        self.library.abort_import(identifier)
        other.abort_import(other.begin_import("other.wav", 3))

    def test_same_name_imports_keep_separate_copies(self):
        first = self.import_file()
        second = self.import_file()
        self.assertNotEqual(first, second)
        self.library.delete_file(first)
        self.assertEqual([row["id"] for row in self.library.list_files()], [second])
        self.assertTrue((self.audio / f"{second}.wav").is_file())

    def test_commit_failure_does_not_create_history(self):
        identifier = self.library.begin_import("recording.wav", 3)
        self.library.append_chunk(identifier, 0, "YWJj")
        with patch.object(self.library, "_insert", side_effect=OSError("database busy")):
            with self.assertRaises(OSError):
                self.library.finish_import(identifier)
        self.library.abort_import(identifier)
        self.assertEqual(self.library.list_files(), [])
        self.assertFalse((self.audio / f"{identifier}.wav").exists())

    def test_failed_file_removal_restores_record_and_file(self):
        identifier = self.import_file()
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.suffix == ".deleted":
                raise PermissionError("file in use")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            with self.assertRaises(PermissionError):
                self.library.delete_file(identifier)
        self.assertEqual(self.library.list_files()[0]["id"], identifier)
        self.assertTrue((self.audio / f"{identifier}.wav").is_file())

    def test_recover_completed_rename_before_database_commit(self):
        import uuid

        identifier = str(uuid.uuid4())
        (self.audio / f"{identifier}.wav").write_bytes(b"abc")
        (self.audio / ".pending.json").write_text(json.dumps({
            "id": identifier, "name": "recovered.wav", "suffix": ".wav", "size_bytes": 3,
        }))
        reopened = AudioLibrary(self.database, self.audio)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.list_files()[0]["name"], "recovered.wav")

    def test_missing_file_can_be_removed_from_history(self):
        identifier = self.import_file()
        (self.audio / f"{identifier}.wav").unlink()
        self.assertFalse(self.library.list_files()[0]["available"])
        self.library.delete_file(identifier)
        self.assertEqual(self.library.list_files(), [])

    def test_invalid_filename_cannot_escape_audio_directory(self):
        with self.assertRaises(ValueError):
            self.library.begin_import("../outside.wav", 3)
