"""Inference is imported only in a spawned child, never in the GUI process."""

from __future__ import annotations

import multiprocessing
import logging
import os
import threading
import time
import signal
import sys
from pathlib import Path

from ..file_lock import FileLocked, exclusive_file_lock
from ..logging_config import configure_logging


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


def _decode(path):
    import av
    import numpy as np

    blocks = []
    resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError("文件中没有可识别的音轨。")
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                blocks.append(converted.to_ndarray().reshape(-1))
        for converted in resampler.resample(None):
            blocks.append(converted.to_ndarray().reshape(-1))
    if not blocks:
        raise ValueError("音频解码后为空。")
    return np.concatenate(blocks).astype(np.float32, copy=False)


def _faster(audio, model_path, emit, force_cpu, device_index=0):
    logger.info('加载识别依赖 engine=faster-whisper')
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except (ImportError, OSError, RuntimeError) as error:
        if not force_cpu:
            logger.exception('显卡识别依赖加载失败，切换 CPU')
            emit({'kind': 'fallback', 'error': str(error)})
            return None
        raise

    device = 'cpu'
    if not force_cpu:
        try:
            count = ctranslate2.get_cuda_device_count()
            logger.info('CUDA 设备检测 count=%d', count)
            if count > device_index:
                device = 'cuda'
            else:
                raise RuntimeError('Previously verified CUDA device is no longer available')
        except (RuntimeError, OSError) as error:
            logger.warning('CUDA 设备检测失败，使用新 CPU 进程', exc_info=True)
            emit({'kind': 'fallback', 'error': str(error)})
            return None
    else:
        logger.info('本次使用 CPU，跳过 CUDA 检测')
    try:
        supported = ctranslate2.get_supported_compute_types(device, device_index if device == 'cuda' else 0)
        logger.info('设备支持的计算精度 device=%s types=%s', device, ','.join(sorted(supported)))
        choices = ('float16', 'int8_float16', 'float32') if device == 'cuda' else ('int8', 'int8_float32', 'float32')
        compute = next((kind for kind in choices if kind in supported), 'float32')
        label = f"{'NVIDIA GPU' if device == 'cuda' else 'CPU'} · {compute}"
        emit({'kind': 'progress', 'detail': '正在加载模型。', 'device': label})
        logger.info('加载模型 device=%s compute=%s model_path=%s', device, compute, model_path)
        load_started = time.monotonic()
        model = WhisperModel(str(model_path), device=device, compute_type=compute,
                             device_index=device_index if device == 'cuda' else 0,
                             cpu_threads=max(1, min(8, (os.cpu_count() or 2) // 2)),
                             num_workers=1, local_files_only=True)
        logger.info('模型加载完成 elapsed=%.2fs', time.monotonic() - load_started)
        emit({'kind': 'progress', 'detail': '正在转录。', 'device': label})
        infer_started = time.monotonic()
        logger.info('开始转录 duration=%.2fs beam_size=5 vad=true', len(audio) / 16000)
        segments, info = model.transcribe(audio, task='transcribe', language=None,
                                         beam_size=5, vad_filter=True, condition_on_previous_text=False)
        result = []
        last = 0.0
        last_log = infer_started
        for segment in segments:
            result.append({'start': float(segment.start), 'end': float(segment.end), 'text': segment.text})
            now = time.monotonic()
            if now - last >= 1:
                emit({'kind': 'progress', 'detail': f'已处理至 {int(segment.end // 60)}分{int(segment.end % 60)}秒。',
                      'device': label})
                last = now
            if now - last_log >= 10:
                logger.info('转录进度 processed_until=%.2fs segments=%d elapsed=%.2fs',
                            segment.end, len(result), now - infer_started)
                last_log = now
        logger.info('转录完成 language=%s segments=%d elapsed=%.2fs',
                    info.language, len(result), time.monotonic() - infer_started)
        return {'text': ''.join(s['text'] for s in result).strip(), 'segments': result,
                'language': info.language, 'duration': len(audio) / 16000, 'device': label}
    except (RuntimeError, OSError) as error:
        # A fresh CPU process is required: deleting a Python reference may not
        # release CUDA allocations after an OOM or failed runtime initialization.
        gpu_error = any(word in str(error).lower() for word in ('cuda', 'cublas', 'cudnn', 'out of memory', 'gpu'))
        if device == 'cuda' and gpu_error:
            logger.warning('GPU 调用失败，请求以 CPU 重新转录', exc_info=True)
            emit({'kind': 'fallback', 'error': str(error)})
            return None
        raise


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


def run_worker(send, audio_path, model_path, lock_path, engine, force_cpu=False, task_id='-', runtime=None,
               locale='zh-CN', work_dir=None):
    native = engine == 'apple-speech' and sys.platform == 'darwin'
    if native:
        # The supervisor can cancel Python, xcrun/Swift and the native helper
        # together, including when the Python worker exits unexpectedly.
        os.setsid()
    configure_logging(task_id)
    started = time.monotonic()
    logger.info('识别进程启动 engine=%s force_cpu=%s', engine, force_cpu)
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
        if engine not in {'mlx-whisper', 'faster-whisper'}:
            raise ValueError(f'不支持的转录引擎：{engine}')
        if runtime and not force_cpu:
            try:
                from ..acceleration.probe import bootstrap
                bootstrap(runtime['path'])
            except Exception as error:
                logger.exception('应用加速组件加载失败，切换 CPU')
                send.send({'kind': 'fallback', 'error': str(error)})
                return
        with exclusive_file_lock(Path(lock_path)):
            logger.debug('已取得模型使用锁 path=%s', lock_path)
            if not Path(model_path).is_dir():
                logger.warning('模型目录不存在 path=%s', model_path)
                send.send({'kind': 'unavailable'})
                return
            send.send({'kind': 'progress', 'detail': '正在读取音频。'})
            decode_started = time.monotonic()
            logger.info('开始解码音频 suffix=%s', Path(audio_path).suffix)
            audio = _decode(Path(audio_path))
            logger.info('音频解码完成 duration=%.2fs samples=%d elapsed=%.2fs',
                        len(audio) / 16000, len(audio), time.monotonic() - decode_started)
            result = (_mlx(audio, Path(model_path), send.send) if engine == 'mlx-whisper'
                      else _faster(audio, Path(model_path), send.send, force_cpu,
                                   runtime['device_index'] if runtime else 0))
            if result is not None:
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
        send.send({'kind': 'error', 'detail': '转录失败，请检查音频和模型后重试。',
                   'error': f'{type(error).__name__}: {error}'})
    finally:
        logger.info('识别进程结束 elapsed=%.2fs', time.monotonic() - started)
        send.close()
