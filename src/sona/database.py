from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .models import ModelDefinition, ModelEvent


SCHEMA_VERSION = 3
MIGRATIONS = {
    1: (
        """CREATE TABLE models (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            locale TEXT NOT NULL,
            storage TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}'
        )""",
        """CREATE TABLE model_installations (
            model_id TEXT PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('unknown', 'installed', 'not_installed', 'downloading', 'unsupported')),
            resource_path TEXT,
            checked_at TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE TABLE model_selections (
            kind TEXT PRIMARY KEY,
            model_id TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
            selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    ),
    2: (
        "ALTER TABLE model_installations ADD COLUMN downloaded_bytes INTEGER",
        "ALTER TABLE model_installations ADD COLUMN total_bytes INTEGER",
        "ALTER TABLE model_installations ADD COLUMN artifact_version TEXT",
    ),
    3: (
        """CREATE TABLE audio_files (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            suffix TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
    ),
}


class ModelRepository:
    def __init__(self, database: Path, models: Sequence[ModelDefinition]) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # Serialize migrations and seed updates across simultaneous app launches.
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("数据库版本高于当前软件支持的版本，请使用较新的 Sona。")
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[target]:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {target}")
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
