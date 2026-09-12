"""Durable acquisition state; audio_files are created only after downloading."""

import sqlite3
import uuid
from contextlib import contextmanager

from .urls import normalize_episode


ACTIVE = ('resolving', 'downloading', 'importing', 'cancelling')


class ImportRepository:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, url):
        platform, episode, canonical = normalize_episode(url)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id FROM podcast_imports WHERE platform=? AND episode_id=?',
                                  (platform, episode)).fetchone()
            if existing:
                return {'id': existing['id'], 'existing': True}
            identifier = str(uuid.uuid4())
            db.execute('INSERT INTO podcast_imports(id,platform,episode_id,source_url) VALUES (?,?,?,?)',
                       (identifier, platform, episode, canonical))
            return {'id': identifier, 'existing': False}

    def get(self, identifier):
        with self.connection() as db:
            row = db.execute('SELECT * FROM podcast_imports WHERE id=?', (identifier,)).fetchone()
        return dict(row) if row else None

    def recover(self):
        # The scheduler file lock proves that no other parent owns these jobs.
        # A file committed before a crash is authoritative even if status wasn't saved.
        with self.connection() as db:
            db.execute("""UPDATE podcast_imports SET status='imported', detail=''
                WHERE EXISTS(SELECT 1 FROM audio_files WHERE id=podcast_imports.id)""")
            db.execute("""UPDATE podcast_imports SET status='interrupted', detail='上次获取中断，请重试。'
                WHERE status IN ('resolving','downloading')""")
            db.execute("UPDATE podcast_imports SET status='cancelled', detail='' WHERE status='cancelling'")

    def pending(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM podcast_imports
                WHERE status IN ('waiting_fetch','importing') ORDER BY created_at,id""")]

    def patch(self, identifier, **fields):
        allowed = {'status', 'stage', 'detail', 'name', 'podcast_title', 'cover_url', 'duration',
                   'downloaded_bytes', 'total_bytes', 'suffix'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError('无效的获取任务更新。')
        with self.connection() as db:
            # Cancellation wins over late progress events. Settlement is explicit.
            cursor = db.execute(f"UPDATE podcast_imports SET {','.join(f'{key}=?' for key in fields)} "
                                "WHERE id=? AND status NOT IN ('cancelling','cancelled','imported')",
                                (*fields.values(), identifier))
            return bool(cursor.rowcount)

    def cancel(self, identifier):
        with self.connection() as db:
            cursor = db.execute("""UPDATE podcast_imports SET status=CASE
                WHEN status='waiting_fetch' THEN 'cancelled' ELSE 'cancelling' END, detail=''
                WHERE id=? AND status IN ('waiting_fetch','resolving','downloading','importing')""", (identifier,))
            if not cursor.rowcount:
                raise ValueError('获取阶段已结束，请刷新列表后操作。')

    def settle_cancel(self, identifier):
        with self.connection() as db:
            db.execute("UPDATE podcast_imports SET status='cancelled', detail='', downloaded_bytes=0, total_bytes=0 "
                       "WHERE id=? AND status='cancelling'", (identifier,))

    def retry(self, identifier):
        with self.connection() as db:
            cursor = db.execute("""UPDATE podcast_imports SET status='waiting_fetch', stage='resolving',
                detail='', downloaded_bytes=0, total_bytes=0, suffix=''
                WHERE id=? AND status IN ('failed','cancelled','interrupted')
                AND NOT EXISTS(SELECT 1 FROM audio_files WHERE id=podcast_imports.id)""", (identifier,))
            if not cursor.rowcount:
                raise ValueError('只有获取失败、中断或已取消的任务可以重新获取。')

    def delete_pending(self, identifier):
        with self.connection() as db:
            cursor = db.execute("""DELETE FROM podcast_imports WHERE id=?
                AND status IN ('waiting_fetch','failed','cancelled','interrupted')
                AND NOT EXISTS(SELECT 1 FROM audio_files WHERE id=podcast_imports.id)""", (identifier,))
            if not cursor.rowcount:
                raise ValueError('请先取消获取，等待结束后再删除。')
