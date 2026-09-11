from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager


logger = logging.getLogger(__name__)


class TaskRepository:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def recover(self):
        # Called only by the process holding the scheduler lock. A terminated
        # engine cannot resume in the middle of an audio; rerun the whole task.
        with self.connection() as db:
            db.execute("UPDATE transcription_tasks SET status='queued', detail='上次转录中断，正在重新排队。' "
                       "WHERE status='transcribing'")
            db.execute("UPDATE transcription_tasks SET status='cancelled', detail='' WHERE status='cancelling'")

    def pending(self):
        with self.connection() as db:
            db.execute("""UPDATE transcription_tasks SET status='queued', model_id=(
                SELECT s.model_id FROM model_selections s JOIN models m ON m.id=s.model_id
                WHERE s.kind='speech' AND m.provider IN ('whisper','apple-speech'))
                WHERE model_id IS NULL AND status='waiting_model'
                AND EXISTS(SELECT 1 FROM model_selections s JOIN models m ON m.id=s.model_id
                    WHERE s.kind='speech' AND m.provider IN ('whisper','apple-speech'))""")
            rows = db.execute("""SELECT t.*, a.name, a.suffix FROM transcription_tasks t
                JOIN audio_files a ON a.id=t.audio_id
                WHERE t.status IN ('queued','waiting_model') ORDER BY t.created_at, t.audio_id""").fetchall()
        return [dict(row) for row in rows]

    def wait_for_model(self, identifier, detail):
        with self.connection() as db:
            changed = db.execute("""UPDATE transcription_tasks SET status='waiting_model', detail=?
                WHERE audio_id=? AND status IN ('queued','waiting_model')
                AND (status!='waiting_model' OR detail!=?)""", (detail, identifier, detail))
        if changed.rowcount:
            logger.info('任务等待模型 reason=%s', detail, extra={'task_id': identifier})

    def claim(self, identifier, engine, revision, model_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("""UPDATE transcription_tasks SET status='transcribing',
                engine=?, model_revision=?, attempt=attempt+1, detail='正在准备转录。', error='', device='',
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE audio_id=? AND model_id=? AND status IN ('queued','waiting_model')""",
                (engine, revision, identifier, model_id))
            if not updated.rowcount:
                return None
            return dict(db.execute("SELECT * FROM transcription_tasks WHERE audio_id=?", (identifier,)).fetchone())

    def get(self, identifier):
        with self.connection() as db:
            row = db.execute("SELECT * FROM transcription_tasks WHERE audio_id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("音频任务不存在。")
        return dict(row)

    def update(self, task, status, detail='', error='', device=None):
        with self.connection() as db:
            db.execute("""UPDATE transcription_tasks SET status=?, detail=?, error=?,
                device=COALESCE(?,device), updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE audio_id=? AND attempt=? AND status='transcribing'""",
                (status, detail, error, device, task['audio_id'], task['attempt']))

    def settle_cancel(self, identifier):
        with self.connection() as db:
            db.execute("""UPDATE transcription_tasks SET status='cancelled', detail='', error='',
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE audio_id=? AND status='cancelling'""",
                (identifier,))

    def cancel(self, identifier):
        with self.connection() as db:
            db.execute("""UPDATE transcription_tasks SET
                status=CASE WHEN status='transcribing' THEN 'cancelling' ELSE 'cancelled' END,
                detail='', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE audio_id=? AND status IN ('waiting_model','queued','transcribing')""", (identifier,))

    def retry(self, identifier):
        with self.connection() as db:
            cursor = db.execute("""UPDATE transcription_tasks SET status='waiting_model',
                model_id=COALESCE((SELECT s.model_id FROM model_selections s
                    JOIN models m ON m.id=s.model_id
                    WHERE s.kind='speech' AND m.provider IN ('whisper','apple-speech')),model_id),
                engine=NULL, model_revision=NULL, detail='', error='', device='',
                created_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE audio_id=? AND status IN ('failed','cancelled')""", (identifier,))
            if not cursor.rowcount:
                raise ValueError("只有失败或已取消的任务可以重试。")

    def complete(self, task, result):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status,attempt FROM transcription_tasks WHERE audio_id=?",
                                 (task['audio_id'],)).fetchone()
            if not current or current['status'] != 'transcribing' or current['attempt'] != task['attempt']:
                logger.info('忽略已取消或过期执行的结果 attempt=%s', task['attempt'],
                            extra={'task_id': task['audio_id']})
                return
            db.execute("""INSERT OR REPLACE INTO transcription_results
                (audio_id,text,segments_json,language,duration,model_id,engine,model_revision,device)
                VALUES (?,?,?,?,?,?,?,?,?)""", (
                    task['audio_id'], result['text'], json.dumps(result['segments'], ensure_ascii=False),
                    result['language'], result['duration'], task['model_id'], task['engine'],
                    task['model_revision'], result['device']))
            db.execute("""UPDATE transcription_tasks SET status='completed',
                detail=?, error='', device=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE audio_id=?""", (
                    '' if result['text'].strip() else '未识别到语音。', result['device'], task['audio_id']))
        logger.info('结果已保存 status=completed device=%s segments=%d',
                    result['device'], len(result['segments']), extra={'task_id': task['audio_id']})

    def result(self, identifier):
        with self.connection() as db:
            row = db.execute("""SELECT r.*, a.name FROM transcription_results r
                JOIN audio_files a ON a.id=r.audio_id WHERE r.audio_id=?""", (identifier,)).fetchone()
        if not row:
            raise ValueError("转录结果尚未生成。")
        result = dict(row)
        result['segments'] = json.loads(result.pop('segments_json'))
        return result
