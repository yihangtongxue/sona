"""Persist user intent separately from verified runtime readiness."""

import json
import logging
import multiprocessing
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from ..file_lock import FileLocked, exclusive_file_lock
from .catalog import BUNDLE, TOTAL_BYTES
from .download import Paused, check_cancel, downloaded_bytes, install, verify_files
from .probe import run_probe

logger = logging.getLogger(__name__)
ACTIVE = {'checking', 'downloading', 'preparing', 'verifying', 'pausing'}
DEFAULT = {'status': 'checking', 'enabled': False, 'runtime': '', 'hardware': {},
           'downloaded_bytes': 0, 'error': '', 'pause_requested': False, 'token': ''}


class StateStore:
    def __init__(self, database):
        self.database = database
        with self.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS acceleration_settings '
                         '(id INTEGER PRIMARY KEY CHECK(id = 1), payload TEXT NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO acceleration_settings VALUES(1, ?)',
                         (json.dumps(DEFAULT),))

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.database, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def read(self):
        with self.connection() as conn:
            return {**DEFAULT, **json.loads(conn.execute(
                'SELECT payload FROM acceleration_settings WHERE id=1').fetchone()[0])}

    def patch(self, *, expected_token=None, **changes):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            state = {**DEFAULT, **json.loads(conn.execute(
                'SELECT payload FROM acceleration_settings WHERE id=1').fetchone()[0])}
            if expected_token is not None and state['token'] != expected_token:
                return state
            state.update(changes)
            conn.execute('UPDATE acceleration_settings SET payload=? WHERE id=1', (json.dumps(state),))
            return state


class Cancellation:
    def __init__(self, store, closed):
        self.store, self.closed = store, closed
        self._last, self._paused = 0, False

    def is_set(self):
        if self.closed.is_set():
            return True
        now = time.monotonic()
        if now - self._last >= 0.2:
            self._paused = self.store.read()['pause_requested']
            self._last = now
        return self._paused


class AccelerationService:
    def __init__(self, paths):
        self.root = paths.acceleration_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(paths.database)
        self._closed = threading.Event()
        self._mutex = threading.Lock()
        self._thread = None
        self._start('startup', tolerate_busy=True)

    def status(self):
        state = self.store.read()
        # A second open window can recover a crashed installer once its OS lock
        # is released. Recovery performs local checks, never resumes a download.
        if state['status'] in ACTIVE and not self._closed.is_set():
            self._start('startup', tolerate_busy=True)
            state = self.store.read()
        # Keep internal paths, tokens and native error details out of the UI.
        return {key: state[key] for key in ('status', 'enabled', 'hardware', 'downloaded_bytes')} | {
            'total_bytes': TOTAL_BYTES, 'has_runtime': bool(state['runtime']),
        }

    def action(self, action):
        if action not in ('download', 'pause', 'enable', 'disable', 'check'):
            raise ValueError('未知的加速操作。')
        if self._closed.is_set():
            raise ValueError('应用正在关闭。')
        if action == 'pause':
            state = self.store.read()
            if state['status'] in ('downloading', 'preparing', 'pausing'):
                self.store.patch(pause_requested=True, status='pausing')
            return self.status()
        self._start(action)
        return self.status()

    def _start(self, action, tolerate_busy=False):
        with self._mutex:
            if self._thread and self._thread.is_alive():
                if tolerate_busy:
                    return
                raise ValueError('正在处理，请稍候。')
            lease = exclusive_file_lock(self.root / '.operation.lock')
            try:
                lease.__enter__()
            except FileLocked:
                if tolerate_busy:
                    return
                raise ValueError('另一个窗口正在处理加速设置，请稍候。') from None
            try:
                previous = self.store.read()
                if action == 'disable':
                    self.store.patch(enabled=False, status='disabled', error='')
                    logger.info('用户关闭转录加速；已下载组件保留，当前任务不受影响')
                    lease.__exit__(None, None, None)
                    return
                if action in ('download', 'enable'):
                    if previous['hardware'].get('kind') != 'nvidia' or previous['hardware'].get('driver_required'):
                        raise ValueError('请先检查显卡是否支持加速。')
                    if action == 'enable' and not previous['runtime']:
                        raise ValueError('请先下载加速组件。')
                self.store.patch(status='checking', pause_requested=False, error='', token=uuid.uuid4().hex,
                                 **({'enabled': True} if action in ('download', 'enable') else {}))
                self._thread = threading.Thread(target=self._operate, args=(action, previous, lease),
                                                name='acceleration-manager', daemon=True)
                self._thread.start()
            except Exception:
                lease.__exit__(None, None, None)
                raise

    def _probe(self, cancel, path=None, index=0):
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=run_probe, args=(send, str(path) if path else None, index),
                                  name='sona-acceleration-check', daemon=True)
        started = False
        try:
            process.start()
            started = True
            send.close()
            deadline = time.monotonic() + (90 if path else 15)
            while time.monotonic() < deadline:
                check_cancel(cancel)
                if receive.poll(0.2):
                    try:
                        return receive.recv()
                    except EOFError:
                        break
                if not process.is_alive():
                    break
            return {'ok': False, 'error': f'Probe exited or timed out; exitcode={process.exitcode}'}
        finally:
            if started:
                process.join(timeout=0.3)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join()
                logger.info('加速检查子进程退出 pid=%s exitcode=%s', process.pid, process.exitcode)
                process.close()
            receive.close()
            send.close()

    def _operate(self, action, previous, lease):
        cancel = Cancellation(self.store, self._closed)
        phase = 'check'
        try:
            logger.info('加速操作开始 action=%s enabled=%s', action, self.store.read()['enabled'])
            result = self._probe(cancel)
            if not result['ok']:
                self._check_failed(result)
                return
            hardware = result['hardware']
            self.store.patch(hardware=hardware)
            kind = hardware['kind']
            if kind != 'nvidia':
                self.store.patch(status=kind)
                return
            if hardware.get('driver_required'):
                self.store.patch(status='driver_required')
                return
            path = self._runtime_path(previous['runtime'])
            # Interrupted downloads never resume from startup or "check".
            if action == 'startup' and previous['status'] in ('downloading', 'preparing', 'pausing', 'paused'):
                received = (TOTAL_BYTES if path is not None and previous['downloaded_bytes'] == TOTAL_BYTES
                            else downloaded_bytes(self.root / 'downloads' / BUNDLE))
                self.store.patch(status='paused', downloaded_bytes=received)
                return
            prepared_before_pause = (previous['status'] == 'paused' and path is not None
                                     and previous['downloaded_bytes'] == TOTAL_BYTES)
            if action == 'download' and not prepared_before_pause:
                phase = 'download'
                last_emit = [0, '']

                def emit(status, received):
                    check_cancel(cancel)
                    now = time.monotonic()
                    if now - last_emit[0] >= 0.5 or last_emit[1] != status:
                        self.store.patch(status=status, downloaded_bytes=received)
                        if status != last_emit[1]:
                            logger.info('加速组件阶段 status=%s bytes=%d/%d', status, received, TOTAL_BYTES)
                        last_emit[:] = [now, status]

                self.store.patch(status='downloading')
                path = install(self.root, cancel, emit)
                # Publish the immutable path, but do not make it eligible yet.
                self.store.patch(runtime=path.name, downloaded_bytes=TOTAL_BYTES)
                phase = 'check'
            if path is None:
                partial = downloaded_bytes(self.root / 'downloads' / BUNDLE)
                status = 'paused' if partial else 'available'
                if previous['status'] == 'download_failed':
                    status = 'download_failed'
                self.store.patch(status=status, downloaded_bytes=partial)
                return
            if not self.store.read()['enabled'] and action == 'startup':
                self.store.patch(status='disabled')
                return
            self.store.patch(status='verifying')
            try:
                verify_files(path, cancel)
            except (OSError, ValueError, KeyError, TypeError) as error:
                logger.exception('本地加速组件损坏，需要重新下载')
                self.store.patch(status='download_failed', runtime='', error=str(error))
                return
            result = self._probe(cancel, path, hardware['device_index'])
            if not result['ok']:
                self._check_failed(result)
                return
            check_cancel(cancel)
            enabled = self.store.read()['enabled']
            self.store.patch(status='ready' if enabled else 'disabled', error='')
            logger.info('加速组件检查通过，后续任务使用 %s', 'NVIDIA GPU' if enabled else 'CPU')
        except Paused:
            self.store.patch(status='paused' if action == 'download' else 'check_failed', pause_requested=False)
            logger.info('加速操作已停止 action=%s phase=%s', action, phase)
        except Exception as error:
            logger.exception('加速操作失败 action=%s phase=%s', action, phase)
            self.store.patch(status='download_failed' if phase == 'download' else 'check_failed',
                             error=f'{type(error).__name__}: {error}')
        finally:
            lease.__exit__(None, None, None)

    def _check_failed(self, result):
        status = 'driver_required' if result.get('driver_required') else 'check_failed'
        logger.warning('加速不可用 status=%s error=%s', status, result.get('error', ''))
        self.store.patch(status=status, error=result.get('error', ''))

    def _runtime_path(self, name):
        if not name or '/' in name or '\\' in name or ':' in name or not name.startswith(BUNDLE + '-'):
            return None
        path = (self.root / name).resolve()
        return path if path.parent == self.root.resolve() and path.is_dir() else None

    def runtime_for_task(self):
        state = self.store.read()
        if not state['enabled'] or state['status'] != 'ready':
            return None
        path = self._runtime_path(state['runtime'])
        if path is None:
            self.report_failure({'token': state['token']}, 'Runtime directory is missing')
            return None
        return {'path': str(path), 'device_index': state['hardware'].get('device_index', 0), 'token': state['token']}

    def report_failure(self, runtime, error):
        # An old job cannot invalidate a newer installation or recheck.
        self.store.patch(expected_token=runtime['token'], status='check_failed', error=error)
        logger.warning('本次显卡调用失败，后续任务使用 CPU，等待用户重新检查 reason=%s', error)

    def close(self):
        self._closed.set()
        with self._mutex:
            thread = self._thread
        if thread:
            thread.join()
