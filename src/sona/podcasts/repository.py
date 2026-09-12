"""Durable acquisition state; audio_files are created only after downloading."""

import sqlite3
import json
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

    def create(self, url, strategy='subtitle_first', subtitle_language='original'):
        platform, episode, canonical = normalize_episode(url)
        if strategy not in ('subtitle_first', 'transcribe') or subtitle_language not in ('original', 'zh', 'en', 'ja', 'ko'):
            raise ValueError('无效的字幕获取选项。')
        if platform != 'youtube':
            strategy, subtitle_language = 'subtitle_first', 'original'
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('''SELECT id FROM podcast_imports WHERE platform=? AND episode_id=?
                UNION SELECT import_id AS id FROM media_import_aliases WHERE platform=? AND source_id=?''',
                                  (platform, episode, platform, episode)).fetchone()
            if existing:
                return {'id': existing['id'], 'existing': True}
            identifier = str(uuid.uuid4())
            db.execute('INSERT INTO podcast_imports(id,platform,episode_id,source_url,strategy,subtitle_language) VALUES (?,?,?,?,?,?)',
                       (identifier, platform, episode, canonical, strategy, subtitle_language))
            return {'id': identifier, 'existing': False}

    def resolve_source(self, identifier, source_url):
        """Bind a resolved BV/part before downloading; merge aliases atomically."""
        platform, episode, canonical = normalize_episode(source_url)
        if platform != 'bilibili' or episode.startswith('short:'):
            raise ValueError('无效的 B站解析结果。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT * FROM podcast_imports WHERE id=?', (identifier,)).fetchone()
            if not job or job['platform'] != platform or job['status'] != 'resolving':
                return None  # Cancellation wins over a late resolution event.
            existing = db.execute('SELECT id FROM podcast_imports WHERE platform=? AND episode_id=? AND id<>?',
                                  (platform, episode, identifier)).fetchone()
            target = existing['id'] if existing else identifier
            db.execute('INSERT OR REPLACE INTO media_import_aliases(platform,source_id,import_id) VALUES (?,?,?)',
                       (platform, job['episode_id'], target))
            if existing:
                db.execute('UPDATE media_import_aliases SET import_id=? WHERE import_id=?', (target, identifier))
                db.execute('DELETE FROM podcast_imports WHERE id=?', (identifier,))
            else:
                db.execute('UPDATE podcast_imports SET episode_id=?,source_url=? WHERE id=?',
                           (episode, canonical, identifier))
            return target

    def get(self, identifier):
        with self.connection() as db:
            row = db.execute('SELECT * FROM podcast_imports WHERE id=?', (identifier,)).fetchone()
        return dict(row) if row else None

    def save_subtitles(self, identifier, result):
        """A text-only import never creates audio_files or enqueues a model."""
        from .captions import validate_result

        validate_result(result)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT status,platform FROM podcast_imports WHERE id=?', (identifier,)).fetchone()
            if not job or job['status'] != 'importing' or job['platform'] != 'youtube':
                return False
            db.execute('''INSERT INTO subtitle_results
                (import_id,text,segments_json,language,duration,source_kind,size_bytes)
                VALUES (?,?,?,?,?,?,?)''', (identifier, result['text'],
                json.dumps(result['segments'], ensure_ascii=False), result['language'], result['duration'],
                result['source_kind'], len(result['text'].encode('utf-8'))))
            db.execute("UPDATE podcast_imports SET status='imported',detail='' WHERE id=?", (identifier,))
            return True

    def delete_subtitles(self, identifier):
        with self.connection() as db:
            return bool(db.execute('''DELETE FROM podcast_imports WHERE id=? AND status='imported'
                AND EXISTS(SELECT 1 FROM subtitle_results WHERE import_id=podcast_imports.id)''',
                (identifier,)).rowcount)

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
