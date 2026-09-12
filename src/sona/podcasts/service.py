"""Serialize downloads, persist progress, and hand complete files to AudioLibrary."""

import logging
import multiprocessing
import os
import shutil
import threading
import time
import uuid

from ..file_lock import FileLocked, exclusive_file_lock
from .repository import ACTIVE, ImportRepository
from .worker import fetch_episode


logger = logging.getLogger(__name__)


class PodcastService:
    def __init__(self, paths, audio_library, activity):
        self.repository = ImportRepository(paths.database)
        self.library = audio_library
        self.activity = activity
        self.root = paths.downloads_dir / 'podcasts'
        self.root.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._mutation = threading.RLock()
        self._context = multiprocessing.get_context('spawn')
        self._thread = threading.Thread(target=self._supervise, name='podcast-imports', daemon=True)
        self._thread.start()

    def _directory(self, identifier):
        return self.root / str(uuid.UUID(identifier))

    def _cleanup(self, identifier):
        try:
            # A child surviving its parent briefly still owns this lock. Never
            # remove its files or launch a replacement until it releases them.
            with exclusive_file_lock(self.root / f'{str(uuid.UUID(identifier))}.lock'):
                shutil.rmtree(self._directory(identifier), ignore_errors=False)
            return True
        except FileNotFoundError:
            return True
        except (FileLocked, OSError):
            return False

    def create(self, url):
        with self._mutation:
            if self._stop.is_set():
                raise ValueError('应用正在关闭。')
            result = self.repository.create(url)
            self._wake.set()
            return result

    def cancel(self, identifier):
        with self._mutation:
            job = self.repository.get(identifier)
            self.repository.cancel(identifier)
            if job and job['status'] == 'importing' and self._cleanup(identifier):
                self.repository.settle_cancel(identifier)
            self._wake.set()

    def retry(self, identifier):
        with self._mutation:
            if self._stop.is_set():
                raise ValueError('应用正在关闭。')
            self.repository.retry(identifier)
            self._wake.set()

    def delete(self, identifier):
        with self._mutation:
            job = self.repository.get(identifier)
            if not job:
                self.library.delete_file(identifier)
                return
            if job['status'] in ACTIVE:
                raise ValueError('请先取消获取，等待结束后再删除。')
            if job['status'] == 'imported':
                self.library.delete_file(identifier)
            else:
                self.repository.delete_pending(identifier)
            self._cleanup(identifier)

    def _recover(self):
        self.repository.recover()
        self._sweep()

    def _sweep(self):
        with self.repository.connection() as db:
            jobs = {row['id']: row['status'] for row in db.execute('SELECT id,status FROM podcast_imports')}
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            try:
                identifier = str(uuid.UUID(directory.name))
            except ValueError:
                continue
            if jobs.get(identifier) not in ('resolving', 'downloading', 'importing'):
                if self._cleanup(identifier):
                    self.repository.settle_cancel(identifier)
        # Also settle jobs cancelled before a directory or child was created.
        for identifier, status in jobs.items():
            if status == 'cancelling' and not self._directory(identifier).exists():
                self.repository.settle_cancel(identifier)

    def _supervise(self):
        while not self._stop.is_set():
            try:
                with exclusive_file_lock(self.root / '.scheduler.lock'):
                    self._recover()
                    while not self._stop.is_set():
                        self._wake.clear()
                        with self.activity.operation(background=True) as allowed:
                            if allowed:
                                self._sweep()
                                pending = self.repository.pending()
                                if pending and not self._stop.is_set():
                                    job = pending[0]
                                    if job['status'] == 'importing':
                                        self._commit(job)
                                    else:
                                        self._run(job)
                        self._wake.wait(1)
            except FileLocked:
                self._stop.wait(1)
            except Exception:
                logger.exception('播客获取队列暂不可用，将重试')
                self._stop.wait(2)

    def _commit(self, job):
        with self._mutation:
            current = self.repository.get(job['id'])
            if not current or current['status'] != 'importing' or self._stop.is_set():
                return
            if self.library.is_importing():
                return  # A local file copy holds the library lock; retry later.
            try:
                self.library.adopt_download(current, self._directory(job['id']))
            except (FileLocked, RuntimeError):
                return
            except Exception:
                self.repository.patch(job['id'], status='failed', stage='importing',
                                      detail='音频入库失败，请检查可用磁盘空间后重新获取。')
                logger.exception('播客音频入库失败', extra={'task_id': job['id']})
                return
            committed = self.repository.get(job['id'])
            if not committed or committed['status'] != 'imported':
                return
            self._cleanup(job['id'])
            logger.info('播客音频已入库，等待本地转录', extra={'task_id': job['id']})

    def _run(self, job):
        identifier = job['id']
        with self._mutation:
            current = self.repository.get(identifier)
            if not current or current['status'] != 'waiting_fetch':
                return
            if not self._cleanup(identifier):
                self.repository.patch(identifier, status='failed', detail='临时文件仍被占用或无法清理，请稍后重试。')
                return
            self._directory(identifier).mkdir(parents=True, exist_ok=True)
            if not self.repository.patch(identifier, status='resolving', stage='resolving', detail=''):
                self._cleanup(identifier)
                return
        receive, send = self._context.Pipe(duplex=True)
        process = self._context.Process(target=fetch_episode,
            args=(job, str(self._directory(identifier)), send, os.getpid()), daemon=True)
        started = False
        outcome = None
        stage = 'resolving'
        deadline = time.monotonic() + 180
        try:
            process.start()
            started = True
            send.close()
            while True:
                current = self.repository.get(identifier)
                if self._stop.is_set() or not current or current['status'] == 'cancelling':
                    break
                if time.monotonic() > deadline:
                    outcome = {'kind': 'error', 'stage': stage, 'detail': '获取超时，请检查网络后重试。'}
                    break
                if receive.poll(0.2):
                    try:
                        event = receive.recv()
                    except EOFError:
                        break
                    kind = event.get('kind')
                    if kind == 'metadata':
                        stage = 'downloading'
                        deadline = time.monotonic() + 3600
                        self.repository.patch(identifier, status='downloading', stage=stage,
                            **{key: event[key] for key in ('name', 'podcast_title', 'cover_url', 'duration')})
                    elif kind == 'progress':
                        self.repository.patch(identifier, downloaded_bytes=event['downloaded_bytes'],
                                              total_bytes=event['total_bytes'])
                    elif kind in ('complete', 'error'):
                        outcome = event
                        break
                elif not process.is_alive():
                    break
        except Exception:
            logger.exception('播客获取进程异常', extra={'task_id': identifier})
        finally:
            if started:
                process.join(timeout=0.5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
            receive.close()
            send.close()
        with self._mutation:
            current = self.repository.get(identifier)
            if not current:
                self._cleanup(identifier)
                return
            if current['status'] == 'cancelling':
                if self._cleanup(identifier):
                    self.repository.settle_cancel(identifier)
                return
            if self._stop.is_set():
                self.repository.patch(identifier, status='interrupted', detail='应用已关闭，获取中断，请重试。')
            elif outcome and outcome['kind'] == 'complete':
                self.repository.patch(identifier, status='importing', stage='importing', suffix=outcome['suffix'],
                                      downloaded_bytes=outcome['size_bytes'], total_bytes=outcome['size_bytes'])
                self._commit(self.repository.get(identifier))
                return
            else:
                self.repository.patch(identifier, status='failed', stage=(outcome or {}).get('stage', stage),
                    detail=(outcome or {}).get('detail', '获取进程意外结束，请重试。'))
            self._cleanup(identifier)

    def close(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=8)
