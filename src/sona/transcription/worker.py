"""Inference is imported only in a spawned child, never in the GUI process."""

from __future__ import annotations

import multiprocessing
import logging
import os
import threading
import time
import signal
import sys
from contextlib import closing
from pathlib import Path

from ..file_lock import FileLocked, exclusive_file_lock
from ..logging_config import configure_logging
from .chunks import audio_chunks, ChunkResults


logger = logging.getLogger(__name__)


def _watch_parent(native=False):
    parent = multiprocessing.parent_process()
    while parent is not None:
        if not parent.is_alive():
            logger.warning('主进程已退出，停止识别进程')
            if native:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(1)
        time.sleep(1)


def _mlx(audio, model_path, emit):
    logger.info('加载识别依赖 engine=mlx-whisper')
    import mlx.core as mx
    import mlx_whisper

    mx.set_default_device(mx.gpu)
    logger.info('开始加载模型并转录 device=Apple-GPU model_path=%s duration=%.2fs',
                model_path, len(audio) / 16000)
    started = time.monotonic()
    emit({'kind': 'progress', 'detail': '正在加载模型并转录。', 'device': 'Apple GPU'})
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=str(model_path), verbose=None,
                                   task='transcribe', language=None, condition_on_previous_text=False)
    segments = [{'start': float(s['start']), 'end': float(s['end']), 'text': s['text']}
                for s in result['segments']]
    logger.info('转录完成 language=%s segments=%d elapsed=%.2fs',
                result['language'], len(segments), time.monotonic() - started)
    return {'text': result['text'].strip(), 'segments': segments,
            'language': result['language'], 'duration': len(audio) / 16000, 'device': 'Apple GPU'}


def run_worker(send, audio_path, model_path, lock_path, engine, task_id='-', locale='zh-CN', work_dir=None):
    native = engine == 'apple-speech' and sys.platform == 'darwin'
    if native:
        # The supervisor can cancel Python, xcrun/Swift and the native helper
        # together, including when the Python worker exits unexpectedly.
        os.setsid()
    configure_logging(task_id)
    started = time.monotonic()
    logger.info('识别进程启动 engine=%s', engine)
    threading.Thread(target=_watch_parent, args=(native,), daemon=True).start()
    # Model resolution must be local; import/download is exclusively managed by
    # the resource UI, never implicitly triggered by an inference library.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    try:
        if engine == 'apple-speech':
            if not native or work_dir is None:
                raise ValueError('Apple Speech 需要 macOS 和可用的临时音频目录。')
            from .apple import transcribe
            result = transcribe(Path(audio_path), Path(work_dir), locale, send.send)
            send.send({'kind': 'result', 'result': result})
            logger.info('Apple Speech 转录结果已发送给主进程')
            return
        if engine != 'mlx-whisper':
            raise ValueError(f'不支持的转录引擎：{engine}')
        with exclusive_file_lock(Path(lock_path)):
            logger.debug('已取得模型使用锁 path=%s', lock_path)
            if not Path(model_path).is_dir():
                logger.warning('模型目录不存在 path=%s', model_path)
                send.send({'kind': 'unavailable'})
                return
            send.send({'kind': 'progress', 'detail': '正在读取音频。'})
            result = _transcribe_chunks(Path(audio_path), Path(model_path), send.send)
            send.send({'kind': 'result', 'result': result})
            logger.info('转录结果已发送给主进程')
    except FileLocked:
        logger.warning('模型正在被其他操作使用，等待重试')
        send.send({'kind': 'unavailable'})
    except ImportError as error:
        logger.exception('识别依赖导入失败 engine=%s', engine)
        send.send({'kind': 'error', 'detail': '识别依赖未安装或无法加载，请更新应用依赖后重试。',
                   'error': f'{type(error).__name__}: {error}'})
    except Exception as error:
        logger.exception('识别执行失败 engine=%s', engine)
        send.send({'kind': 'error', 'detail': '转录失败，请检查文件中的音轨和模型后重试。',
                   'error': f'{type(error).__name__}: {error}'})
    finally:
        logger.info('识别进程结束 elapsed=%.2fs', time.monotonic() - started)
        send.close()


def _transcribe_chunks(audio_path, model_path, emit):
    merged = ChunkResults()
    with closing(audio_chunks(audio_path, emit)) as chunks:
        for index, audio in enumerate(chunks, 1):
            started = time.monotonic()
            report = merged.progress(emit, index)
            result = _mlx(audio, model_path, report)
            merged.append(result, len(audio))
            logger.info('音频分段完成 index=%d samples=%d elapsed=%.2fs',
                        index, len(audio), time.monotonic() - started)
    return merged.result()
