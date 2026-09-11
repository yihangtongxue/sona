from __future__ import annotations

import logging
import multiprocessing
import threading
import time
import uuid
import sys
import os
import platform
import signal
import tempfile
from contextlib import nullcontext

from ..file_lock import FileLocked, exclusive_file_lock
from ..audio_library import AUDIO_SUFFIXES
from ..models import engine_supported
from .repository import TaskRepository
from .worker import run_worker


logger = logging.getLogger(__name__)


class TranscriptionService:
    def __init__(self, paths, provider, models, acceleration=None, *, apple_provider=None, audio_library=None):
        self.repository = TaskRepository(paths.database)
        self._paths = paths
        self._providers = {'whisper': provider, 'apple-speech': apple_provider}
        self._acceleration = acceleration
        self._audio_library = audio_library
        self._models = {model.id: model for model in models if model.can_transcribe}
        self._closed = threading.Event()
        self._context = multiprocessing.get_context('spawn')
        paths.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._supervise, name='transcription-queue', daemon=True)
        self._thread.start()

    def _supervise(self):
        waiting_for_owner = False
        while not self._closed.is_set():
            try:
                # One queue owner per data directory, also across app instances.
                with exclusive_file_lock(self._paths.data_dir / '.transcription.lock'):
                    logger.info('已取得转录队列调度权，恢复未完成任务')
                    waiting_for_owner = False
                    self.repository.recover()
                    next_cleanup = 0.0
                    while not self._closed.is_set():
                        if time.monotonic() >= next_cleanup:
                            self._cleanup_completed_audio()
                            next_cleanup = time.monotonic() + 30
                        if self._next():
                            # _next returns only after the worker has exited and
                            # its result transaction / cancellation has settled.
                            self._cleanup_completed_audio()
                            next_cleanup = time.monotonic() + 30
                        else:
                            self._closed.wait(2)
            except FileLocked:
                if not waiting_for_owner:
                    logger.info('另一个应用实例正在调度转录，本实例等待接管')
                    waiting_for_owner = True
                self._closed.wait(2)
            except Exception:
                logger.exception('Transcription queue failed; retrying')
                self._closed.wait(2)

    def _cleanup_completed_audio(self):
        if self._audio_library is None:
            return
        try:
            self._audio_library.cleanup_completed()
        except Exception:
            # Cleanup must never turn a persisted transcript into a failed task.
            logger.exception('音频副本清理暂未完成，将稍后重试')

    def _next(self):
        states = {}
        for record in self.repository.pending():
            if self._closed.is_set():
                return False
            model = self._models.get(record['model_id'])
            if model is None:
                self.repository.wait_for_model(record['audio_id'], '请在模型设置中准备并选择默认音频转文字模型。')
                continue
            if model.provider == 'whisper' and not engine_supported():
                self.repository.wait_for_model(record['audio_id'], 'Mac 端转录需要 Apple 芯片和原生 ARM64 Python。')
                continue
            try:
                provider = self._providers.get(model.provider)
                if provider is None:
                    raise ValueError('转录引擎未配置，请重新启动应用。')
                # Query once per sweep when many files await the same asset.
                if model.id not in states:
                    states[model.id] = provider.run('status', model, lambda _: None)
                state = states[model.id]
            except (OSError, ValueError) as error:
                self.repository.wait_for_model(record['audio_id'], f'无法读取 {model.name}：{error}')
                continue
            if self._closed.is_set():
                return False
            if state.status != 'installed':
                self.repository.wait_for_model(record['audio_id'],
                    f'{model.name}：{state.detail} 请前往模型设置处理。')
                continue
            revision = (f'system-macOS-{platform.mac_ver()[0]}' if model.engine == 'apple-speech'
                        else model.bundle['revision'])
            task = self.repository.claim(record['audio_id'], model.engine, revision, model.id)
            if task is None:
                continue
            task_log = logging.LoggerAdapter(logger, {'task_id': task['audio_id']})
            task_log.info('开始执行任务 attempt=%d model=%s engine=%s revision=%s',
                          task['attempt'], task['model_id'], task['engine'], task['model_revision'])
            try:
                # Only validated IDs/extensions participate in file paths.
                identifier = str(uuid.UUID(record['audio_id']))
                if record['suffix'] not in AUDIO_SUFFIXES:
                    raise ValueError('音频记录中的文件格式无效。')
                audio_path = self._paths.audio_dir / f"{identifier}{record['suffix']}"
                # The supervisor owns temporary audio, so killing a worker on
                # cancellation still removes its decoded file.
                scratch = (tempfile.TemporaryDirectory(prefix='sona-speech-')
                           if model.engine == 'apple-speech' else nullcontext(None))
                with scratch as work_dir:
                    self._execute(task, audio_path, state.resource_path, model.locale, work_dir)
            except Exception as error:
                task_log.exception('任务启动或结果保存失败')
                self.repository.update(task, 'failed', '无法启动或保存转录任务，请重试。', str(error))
            finally:
                self.repository.settle_cancel(task['audio_id'])
                if self._closed.is_set():
                    self.repository.update(task, 'queued', '应用关闭，等待下次启动后重新转录。')
            return True
        return False

    def _execute(self, task, audio_path, model_path, locale='zh-CN', work_dir=None):
        task_log = logging.LoggerAdapter(logger, {'task_id': task['audio_id']})
        started_at = time.monotonic()
        native = task['engine'] == 'apple-speech'
        runtime = self._acceleration.runtime_for_task() if self._acceleration and not native else None
        force_cpu = sys.platform == 'win32' and runtime is None
        fell_back = False
        task_log.info('任务设备选择 force_cpu=%s managed_acceleration=%s', force_cpu, bool(runtime))
        while not self._closed.is_set():
            if self.repository.get(task['audio_id'])['status'] != 'transcribing':
                return
            receive, send = self._context.Pipe(duplex=False)
            process = self._context.Process(
                target=run_worker,
                args=(send, str(audio_path), str(model_path),
                      str(self._paths.downloads_dir / f"{task['model_id']}.lock"),
                      task['engine'], force_cpu, task['audio_id'], runtime if not force_cpu else None,
                      locale, work_dir),
                name='sona-transcription', daemon=True,
            )
            outcome = None
            started = False
            gpu_started = False
            last_heartbeat = time.monotonic()
            try:
                process.start()
                started = True
                task_log.info('已启动识别子进程 pid=%s force_cpu=%s', process.pid, force_cpu)
                send.close()
                while not self._closed.is_set():
                    current = self.repository.get(task['audio_id'])
                    if current['status'] != 'transcribing':
                        task_log.info('停止识别子进程，任务状态已变更 status=%s', current['status'])
                        break
                    now = time.monotonic()
                    if now - last_heartbeat >= 15:
                        task_log.info('任务仍在执行 pid=%s device=%s stage=%s elapsed=%.2fs',
                                      process.pid, current['device'] or '待确定', current['detail'], now - started_at)
                        last_heartbeat = now
                    if receive.poll(0.2):
                        try:
                            event = receive.recv()
                        except EOFError:
                            break
                        kind = event['kind']
                        if kind == 'progress':
                            task_log.debug('收到进度 device=%s detail=%s', event.get('device', ''), event['detail'])
                            gpu_started = event.get('device', '').startswith('NVIDIA GPU') or gpu_started
                            detail = event['detail']
                            if fell_back:
                                detail = '显卡不可用，已切换为 CPU。' + detail
                            self.repository.update(task, 'transcribing', detail, device=event.get('device'))
                        else:
                            outcome = event
                            break
                    elif not process.is_alive():
                        break
            finally:
                # Wait briefly for a result sender to exit normally, otherwise
                # terminate. Cancellation and shutdown release native memory.
                if started:
                    if outcome:
                        process.join(timeout=0.5)
                    if process.is_alive():
                        task_log.info('结束识别子进程 pid=%s', process.pid)
                        if native:
                            _signal_native_group(process.pid, signal.SIGTERM)
                        process.terminate()
                        process.join(timeout=2)
                    if process.is_alive():
                        task_log.warning('强制结束识别子进程 pid=%s', process.pid)
                        process.kill()
                        process.join()
                    if native:
                        # Also reap a helper left behind by a crashed worker.
                        _signal_native_group(process.pid, signal.SIGKILL)
                    task_log.info('识别子进程已退出 pid=%s exitcode=%s', process.pid, process.exitcode)
                    process.close()
                receive.close()
                send.close()
            current = self.repository.get(task['audio_id'])
            if current['status'] == 'cancelling':
                self.repository.settle_cancel(task['audio_id'])
                task_log.info('任务已取消 elapsed=%.2fs', time.monotonic() - started_at)
                return
            if self._closed.is_set():
                task_log.info('应用关闭，任务重新排队')
                self.repository.update(task, 'queued', '应用关闭，等待下次启动后重新转录。')
                return
            if outcome is None:
                if (gpu_started or runtime) and not force_cpu:
                    task_log.warning('GPU 进程未返回结果，使用新的 CPU 进程重试')
                    force_cpu = True
                    fell_back = True
                    if runtime and self._acceleration:
                        self._acceleration.report_failure(runtime, 'GPU process exited without a result')
                    self.repository.update(task, 'transcribing', '显卡进程异常，正在切换为 CPU。', device='CPU')
                    continue
                task_log.error('识别进程未返回结果，任务失败')
                self.repository.update(task, 'failed', '识别进程意外结束，请重试。',
                                       '识别进程没有返回结果，可能是运行库异常或内存不足。')
                return
            if outcome['kind'] == 'fallback' and not force_cpu:
                task_log.warning('收到 CPU 回退请求 reason=%s', outcome.get('error', ''))
                force_cpu = True
                fell_back = True
                if runtime and self._acceleration:
                    self._acceleration.report_failure(runtime, outcome.get('error', 'GPU call failed'))
                self.repository.update(task, 'transcribing', '显卡不可用，正在切换为 CPU。', device='CPU')
                continue
            if outcome['kind'] == 'unavailable':
                task_log.warning('模型暂不可用，任务等待重试')
                self.repository.update(task, 'waiting_model', '模型正在被管理，稍后自动重试。')
                self._closed.wait(2)
            elif outcome['kind'] == 'result':
                self.repository.complete(task, outcome['result'])
                # A cancellation can win the transaction immediately before completion.
                self.repository.settle_cancel(task['audio_id'])
                task_log.info('结果提交处理结束 elapsed=%.2fs', time.monotonic() - started_at)
            else:
                task_log.error('任务失败 detail=%s error=%s', outcome.get('detail', ''), outcome.get('error', ''))
                self.repository.update(task, 'failed', outcome.get('detail', '转录失败。'), outcome.get('error', ''))
            return

    def close(self):
        logger.info('正在停止转录队列')
        self._closed.set()
        if self._providers.get('apple-speech') is not None:
            self._providers['apple-speech'].close()
        self._thread.join()
        logger.info('转录队列已停止')


def _signal_native_group(pid, sig):
    if sys.platform == 'darwin':
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            # Cancellation may win before the worker creates its session.
            pass
