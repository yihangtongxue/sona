from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sona.database import MIGRATIONS, ModelRepository, SCHEMA_VERSION
from sona.model_service import ModelService
from sona.models import BUILTIN_MODELS, ModelEvent
from sona.paths import get_app_paths


MODEL = BUILTIN_MODELS[0]


class FakeProvider:
    def __init__(self, status: str = "installed") -> None:
        self.status = status
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def run(self, action, model, emit):
        self.calls.append((action, model.id))
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("Fake provider was not released.")
        return ModelEvent(self.status, "fake result")

    def close(self):
        self.release.set()

    def cancel(self, model_id):
        self.status = "paused"
        self.release.set()


class ModelStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "sona.sqlite3"
        self.repository = ModelRepository(self.database, BUILTIN_MODELS)

    def service(self, provider):
        service = ModelService(self.repository, {MODEL.provider: provider}, (MODEL,))
        self.addCleanup(service.close)
        return service

    def wait_for_result(self, service):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            model = service.list_models()[0]
            if not model["active"]:
                return model
            threading.Event().wait(0.01)
        self.fail("Model task did not finish.")

    def test_migration_and_selection_survive_reopening(self):
        self.repository.save_result(MODEL, ModelEvent("installed", "ready"), select=True)
        reopened = ModelRepository(self.database, BUILTIN_MODELS)
        record = reopened.list_models()[0]
        self.assertTrue(record["selected"])
        self.assertEqual(record["installation_status"], "installed")
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        finally:
            connection.close()

    def test_rejects_newer_database_without_downgrading_it(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RuntimeError):
            ModelRepository(self.database, BUILTIN_MODELS)

    def test_upgrade_version_one_preserves_existing_selection(self):
        old_database = Path(self.directory.name) / "version-one.sqlite3"
        connection = sqlite3.connect(old_database)
        try:
            with connection:
                for statement in MIGRATIONS[1]:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
                connection.execute(
                    "INSERT INTO models (id, provider, name, kind, locale, storage) VALUES (?, ?, ?, ?, ?, ?)",
                    (MODEL.id, MODEL.provider, MODEL.name, MODEL.kind, MODEL.locale, MODEL.storage),
                )
                connection.execute(
                    "INSERT INTO model_installations (model_id, status) VALUES (?, 'installed')", (MODEL.id,),
                )
                connection.execute(
                    "INSERT INTO model_selections (kind, model_id) VALUES (?, ?)", (MODEL.kind, MODEL.id),
                )
        finally:
            connection.close()
        record = ModelRepository(old_database, (MODEL,)).list_models()[0]
        self.assertTrue(record["selected"])
        self.assertEqual(record["installation_status"], "installed")
        self.assertIsNone(record["downloaded_bytes"])

    def test_failed_delete_keeps_default_selection(self):
        self.repository.save_result(MODEL, ModelEvent("installed", "ready"), select=True)
        service = self.service(FakeProvider("failed"))
        service.start(MODEL.id, "delete")
        self.assertTrue(self.wait_for_result(service)["selected"])

    def test_successful_delete_clears_selection(self):
        self.repository.save_result(MODEL, ModelEvent("installed", "ready"), select=True)
        service = self.service(FakeProvider("supported"))
        service.start(MODEL.id, "delete")
        self.assertFalse(self.wait_for_result(service)["selected"])

    def test_delete_record_and_selection_roll_back_together(self):
        self.repository.save_result(MODEL, ModelEvent("installed", "ready"), select=True)
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute(
                    "CREATE TRIGGER reject_clear BEFORE DELETE ON model_selections "
                    "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
                )
        finally:
            connection.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.save_result(
                MODEL, ModelEvent("supported", "deleted"), select=False, clear_selection=True,
            )
        record = self.repository.list_models()[0]
        self.assertTrue(record["selected"])
        self.assertEqual(record["installation_status"], "installed")

    def test_refresh_reads_list_only_once(self):
        service = self.service(FakeProvider())
        with patch.object(self.repository, "list_models", wraps=self.repository.list_models) as listing:
            service.refresh_all()
            self.assertEqual(listing.call_count, 1)
        self.wait_for_result(service)

    def test_service_uses_injected_model_definitions(self):
        custom = replace(MODEL, id="custom-speech")
        repository = ModelRepository(Path(self.directory.name) / "custom.sqlite3", (custom,))
        provider = FakeProvider()
        service = ModelService(repository, {custom.provider: provider}, (custom,))
        self.addCleanup(service.close)
        service.refresh_all()
        self.assertEqual(self.wait_for_result(service)["id"], custom.id)
        self.assertEqual(provider.calls, [("status", custom.id)])

    def test_progress_writes_are_throttled_and_final_bytes_are_saved(self):
        provider = FakeProvider()

        def download(action, model, emit):
            for size in range(100):
                emit(ModelEvent("downloading", "progress", downloaded_bytes=size, total_bytes=100))
            return ModelEvent("installed", "done", downloaded_bytes=100, total_bytes=100)

        service = self.service(provider)
        # Keep the save interval above the task duration; the final result still must persist.
        with patch.object(provider, "run", side_effect=download), patch.object(
            self.repository, "save_result", wraps=self.repository.save_result,
        ) as save, patch("sona.model_service.PROGRESS_SAVE_INTERVAL", 3600):
            service.start(MODEL.id, "download")
            result = self.wait_for_result(service)
            self.assertEqual(save.call_count, 2)
        self.assertEqual(result["downloaded_bytes"], 100)
        self.assertEqual(self.repository.list_models()[0]["downloaded_bytes"], 100)

    def test_pause_reaches_running_managed_provider(self):
        model = replace(MODEL, storage="managed")
        provider = FakeProvider()
        provider.release.clear()
        service = ModelService(self.repository, {model.provider: provider}, (model,))
        self.addCleanup(service.close)
        service.start(model.id, "download")
        self.assertTrue(provider.entered.wait(1))
        service.pause(model.id)
        result = self.wait_for_result(service)
        self.assertEqual(result["status"], "paused")
        self.assertFalse(result["cancelling"])

    def test_listing_does_not_check_or_download(self):
        self.repository.save_result(MODEL, ModelEvent("installed", "ready"), select=True)
        provider = FakeProvider()
        model = self.service(provider).list_models()[0]
        self.assertEqual(provider.calls, [])
        self.assertEqual(model["status"], "unchecked")
        self.assertTrue(model["selected"])

    def test_refresh_queries_without_downloading_or_selecting(self):
        provider = FakeProvider()
        service = self.service(provider)
        service.refresh_all()
        model = self.wait_for_result(service)
        self.assertEqual(provider.calls, [("status", MODEL.id)])
        self.assertEqual(model["status"], "installed")
        self.assertFalse(model["selected"])

    def test_download_and_selection_are_separate(self):
        provider = FakeProvider()
        service = self.service(provider)
        service.start(MODEL.id, "download")
        self.assertFalse(self.wait_for_result(service)["selected"])
        service.start(MODEL.id, "select")
        self.assertTrue(self.wait_for_result(service)["selected"])
        self.assertEqual(provider.calls, [("download", MODEL.id), ("status", MODEL.id)])

    def test_unavailable_model_cannot_be_selected(self):
        service = self.service(FakeProvider("supported"))
        service.start(MODEL.id, "select")
        model = self.wait_for_result(service)
        self.assertFalse(model["selected"])
        self.assertEqual(model["installation_status"], "not_installed")

    def test_unknown_status_keeps_preference_but_not_cached_availability(self):
        self.repository.save_result(MODEL, ModelEvent("installed", "ready"), select=True)
        service = self.service(FakeProvider("unknown"))
        service.start(MODEL.id, "status")
        model = self.wait_for_result(service)
        self.assertTrue(model["selected"])
        self.assertEqual(model["status"], "unknown")
        self.assertEqual(model["installation_status"], "unknown")

    def test_refresh_does_not_replace_running_download(self):
        provider = FakeProvider()
        provider.release.clear()
        service = self.service(provider)
        service.start(MODEL.id, "download")
        self.assertTrue(provider.entered.wait(1))
        service.refresh_all()
        provider.release.set()
        self.wait_for_result(service)
        self.assertEqual(provider.calls, [("download", MODEL.id)])

    def test_provider_error_is_persisted(self):
        provider = FakeProvider()
        with patch.object(provider, "run", side_effect=OSError("helper missing")):
            service = self.service(provider)
            service.start(MODEL.id, "status")
            model = self.wait_for_result(service)
        self.assertEqual(model["status"], "unknown")
        self.assertIn("helper missing", model["last_error"])

    def test_environment_databases_are_isolated(self):
        with patch.dict(os.environ, {"SONA_DATA_DIR": self.directory.name, "SONA_ENV": "development"}):
            development = get_app_paths()
        with patch.dict(os.environ, {"SONA_DATA_DIR": self.directory.name, "SONA_ENV": "test"}):
            testing = get_app_paths()
        dev_repository = ModelRepository(development.database, BUILTIN_MODELS)
        dev_repository.save_result(MODEL, ModelEvent("installed", "ready"), select=True)
        test_repository = ModelRepository(testing.database, BUILTIN_MODELS)
        self.assertNotEqual(development.database, testing.database)
        self.assertFalse(test_repository.list_models()[0]["selected"])


if __name__ == "__main__":
    unittest.main()
