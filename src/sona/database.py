from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .models import ModelDefinition, ModelEvent


# Legacy databases may retain user_version up to 5, even after version tracking
# was removed. Version 6 adopted the current schema; version 7 adds the podcast
# acquisition table. Version 8 adds Bilibili and canonical link aliases.
SCHEMA_VERSION = 8
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS models (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            locale TEXT NOT NULL,
            storage TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}'
        )""",
    """CREATE TABLE IF NOT EXISTS model_installations (
            model_id TEXT PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('unknown', 'installed', 'not_installed', 'downloading', 'unsupported')),
            resource_path TEXT,
            checked_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            downloaded_bytes INTEGER,
            total_bytes INTEGER,
            artifact_version TEXT
        )""",
    """CREATE TABLE IF NOT EXISTS model_selections (
            kind TEXT PRIMARY KEY,
            model_id TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
            selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    """CREATE TABLE IF NOT EXISTS audio_files (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            suffix TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
    """CREATE TABLE IF NOT EXISTS podcast_imports (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL CHECK(platform IN ('xiaoyuzhou','apple','bilibili')),
            episode_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '正在获取音频信息',
            podcast_title TEXT NOT NULL DEFAULT '',
            cover_url TEXT NOT NULL DEFAULT '',
            duration REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'waiting_fetch'
                CHECK(status IN ('waiting_fetch','resolving','downloading','importing',
                                 'imported','cancelling','cancelled','failed','interrupted')),
            stage TEXT NOT NULL DEFAULT 'resolving',
            detail TEXT NOT NULL DEFAULT '',
            downloaded_bytes INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            suffix TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(platform, episode_id)
        )""",
    """CREATE TABLE IF NOT EXISTS transcription_tasks (
            audio_id TEXT PRIMARY KEY REFERENCES audio_files(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'waiting_model'
                CHECK(status IN ('waiting_model','queued','transcribing','cancelling',
                                 'completed','failed','cancelled')),
            model_id TEXT,
            engine TEXT,
            model_revision TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            device TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""",
    """CREATE INDEX IF NOT EXISTS transcription_queue ON transcription_tasks(status, created_at)""",
    """CREATE TABLE IF NOT EXISTS transcription_results (
            audio_id TEXT PRIMARY KEY REFERENCES audio_files(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            segments_json TEXT NOT NULL,
            language TEXT NOT NULL,
            duration REAL NOT NULL,
            model_id TEXT NOT NULL,
            engine TEXT NOT NULL,
            model_revision TEXT NOT NULL,
            device TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""",
    """CREATE TRIGGER IF NOT EXISTS enqueue_import AFTER INSERT ON audio_files BEGIN
            INSERT INTO transcription_tasks(audio_id, model_id, status)
            VALUES (NEW.id, (SELECT s.model_id FROM model_selections s
                JOIN models m ON m.id=s.model_id
                WHERE s.kind='speech' AND m.provider IN ('whisper','apple-speech')),
                CASE WHEN EXISTS(SELECT 1 FROM model_selections s JOIN models m ON m.id=s.model_id
                    WHERE s.kind='speech' AND m.provider IN ('whisper','apple-speech'))
                    THEN 'queued' ELSE 'waiting_model' END);
        END""",
    """CREATE TABLE IF NOT EXISTS ai_model_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_name TEXT NOT NULL,
            base_url TEXT,
            api_key_ref TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'untested'
                CHECK(status IN ('untested', 'ready', 'failed')),
            last_tested_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""",
    """CREATE TABLE IF NOT EXISTS ai_model_selections (
            feature TEXT PRIMARY KEY,
            model_id TEXT NOT NULL REFERENCES ai_model_profiles(id) ON DELETE CASCADE,
            selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    """CREATE TABLE IF NOT EXISTS ai_credential_cleanup (
            key_ref TEXT PRIMARY KEY
        )""",
    """CREATE TABLE IF NOT EXISTS manuscripts (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '待整理文稿',
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','optimizing','completed','failed')),
            source_text TEXT NOT NULL,
            model_json TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""",
    """CREATE INDEX IF NOT EXISTS manuscript_queue ON manuscripts(status, created_at)""",
    """CREATE TABLE IF NOT EXISTS media_import_aliases (
            platform TEXT NOT NULL,
            source_id TEXT NOT NULL,
            import_id TEXT NOT NULL REFERENCES podcast_imports(id) ON DELETE CASCADE,
            PRIMARY KEY(platform, source_id)
        )""",
)


def migrate_media_imports(connection):
    """Rebuild the v7 CHECK constraint inside the initialization transaction."""
    if connection.execute('PRAGMA user_version').fetchone()[0] >= 8:
        return
    existing = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='podcast_imports'").fetchone()
    if not existing or "'bilibili'" in existing[0]:
        return
    statement = next(item for item in SCHEMA if item.startswith('CREATE TABLE IF NOT EXISTS podcast_imports ('))
    connection.execute(statement.replace('podcast_imports (', 'podcast_imports_v8 (', 1))
    connection.execute('INSERT INTO podcast_imports_v8 SELECT * FROM podcast_imports')
    connection.execute('DROP TABLE podcast_imports')
    connection.execute('ALTER TABLE podcast_imports_v8 RENAME TO podcast_imports')


class ModelRepository:
    def __init__(self, database: Path, models: Sequence[ModelDefinition]) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # Serialize initialization and seed updates across app instances.
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                raise ValueError("数据库来自较新的 Sona 版本，请使用新版应用打开，不能直接降级。")
            migrate_media_imports(connection)
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            for model in models:
                connection.execute(
                    """INSERT INTO models (id, provider, name, kind, locale, storage)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           provider=excluded.provider, name=excluded.name, kind=excluded.kind,
                           locale=excluded.locale, storage=excluded.storage""",
                    (model.id, model.provider, model.name, model.kind, model.locale, model.storage),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO model_installations (model_id) VALUES (?)", (model.id,),
                )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # Each operation owns its connection; bridge and worker threads never share one.
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def list_models(self) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT m.*, i.status AS installation_status, i.resource_path,
                          i.checked_at, i.last_error, i.downloaded_bytes, i.total_bytes,
                          i.artifact_version,
                          EXISTS(SELECT 1 FROM model_selections s
                                 WHERE s.kind=m.kind AND s.model_id=m.id) AS selected
                   FROM models m JOIN model_installations i ON i.model_id=m.id
                   ORDER BY m.id""",
            ).fetchall()
        return [dict(row, selected=bool(row["selected"])) for row in rows]

    def save_result(
        self, model: ModelDefinition, event: ModelEvent, *, select: bool,
        clear_selection: bool = False,
    ) -> None:
        if clear_selection and (select or event.status != "supported"):
            raise ValueError("仅可在确认删除成功后清除默认模型。")
        installation_status = {
            "installed": "installed", "supported": "not_installed",
            "waiting": "downloading", "preparing": "downloading",
            "downloading": "downloading", "verifying": "downloading",
            "unsupported": "unsupported",
            "paused": "not_installed",
        }.get(event.status, "unknown")
        with self._connection() as connection:
            connection.execute(
                """UPDATE model_installations SET status=?,
                       resource_path=CASE WHEN ? = 'installed' THEN ? ELSE NULL END,
                       checked_at=CURRENT_TIMESTAMP, last_error=?,
                       downloaded_bytes=COALESCE(?, downloaded_bytes),
                       total_bytes=COALESCE(?, total_bytes),
                       artifact_version=COALESCE(?, artifact_version)
                   WHERE model_id=?""",
                (installation_status, event.status, event.resource_path, event.error,
                 event.downloaded_bytes, event.total_bytes, event.artifact_version, model.id),
            )
            if select:
                if event.status != "installed":
                    raise ValueError("模型资源不可用，不能设为当前模型。")
                connection.execute(
                    """INSERT INTO model_selections (kind, model_id) VALUES (?, ?)
                       ON CONFLICT(kind) DO UPDATE SET model_id=excluded.model_id,
                           selected_at=CURRENT_TIMESTAMP""",
                    (model.kind, model.id),
                )
            if clear_selection:
                connection.execute(
                    "DELETE FROM model_selections WHERE kind=? AND model_id=?",
                    (model.kind, model.id),
                )
