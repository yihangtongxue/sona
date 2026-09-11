"""Apple Speech bridge, imported only inside the transcription worker."""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
import wave

from ..providers.apple_speech import _helper_command, _terminate


logger = logging.getLogger(__name__)
DEVICE = 'Apple Speech · 系统引擎'


def _decode_to_wave(source, destination, emit):
    """Stream all supported imports to PCM without holding the whole file in RAM."""
    import av

    samples = 0
    last_progress = time.monotonic()
    with av.open(str(source)) as container, wave.open(str(destination), 'wb') as output:
        if not container.streams.audio:
            raise ValueError('文件中没有可识别的音轨。')
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)

        def write(frame):
            nonlocal samples
            # ndarray excludes alignment padding in an AV audio plane.
            output.writeframesraw(frame.to_ndarray().astype('<i2', copy=False).tobytes())
            samples += frame.samples

        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                write(converted)
            now = time.monotonic()
            if now - last_progress >= 2:
                emit({'kind': 'progress', 'detail': '正在读取音频。', 'device': DEVICE})
                last_progress = now
        for converted in resampler.resample(None):
            write(converted)
    if not samples:
        raise ValueError('音频解码后为空。')
    return samples / 16000


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


class TranscriptEvents:
    """Only a successful terminal event makes streamed segments a result."""

    def __init__(self, duration, emit):
        self.duration = duration
        self.emit = emit
        self.segments = []
        self.result = None
        self.last_progress = 0.0

    def accept(self, event):
        if not isinstance(event, dict) or event.get('protocol_version') != 2:
            raise ValueError('原生语音工具版本不匹配，请更新 SONA_SPEECH_HELPER 或随应用附带的工具。')
        if self.result is not None:
            raise ValueError('原生工具在完成后返回了多余的转录事件。')
        kind = event.get('kind')
        if kind == 'error':
            raise RuntimeError('\n'.join(str(event[key]) for key in ('detail', 'error') if event.get(key))
                               or 'Apple Speech 转录失败。')
        if kind == 'progress':
            if not isinstance(event.get('detail'), str):
                raise ValueError('原生工具返回了无效的转录进度。')
            self.emit({'kind': 'progress', 'detail': event['detail'], 'device': DEVICE})
        elif kind == 'segment':
            start, end, text = event.get('start'), event.get('end'), event.get('text')
            if (not _number(start) or not _number(end) or not isinstance(text, str)
                    or start < 0 or end < start or end > self.duration + 1
                    or (self.segments and start < self.segments[-1]['start'])):
                raise ValueError('原生工具返回了无效的转录片段。')
            self.segments.append({'start': min(float(start), self.duration),
                                  'end': min(float(end), self.duration), 'text': text})
            now = time.monotonic()
            if now - self.last_progress >= 1:
                self.emit({'kind': 'progress', 'detail': f'已处理至 {int(end // 60)}分{int(end % 60)}秒。',
                           'device': DEVICE})
                self.last_progress = now
        elif kind == 'result':
            duration, language = event.get('duration'), event.get('language')
            if (not _number(duration) or abs(duration - self.duration) > 0.1
                    or not isinstance(language, str) or not language.strip()):
                raise ValueError('原生工具返回了无效的转录结果。')
            self.result = {'text': ''.join(part['text'] for part in self.segments).strip(),
                           'segments': self.segments, 'language': language,
                           'duration': self.duration, 'device': DEVICE}
        else:
            raise ValueError('原生语音工具未返回转录事件，请确认工具已更新。')


def transcribe(audio_path, work_dir, locale, emit):
    command = _helper_command()
    if command is None:
        raise RuntimeError('无法启动原生语音工具，请安装 Xcode Command Line Tools 或配置 SONA_SPEECH_HELPER。')
    emit({'kind': 'progress', 'detail': '正在读取音频。', 'device': DEVICE})
    pcm_path = work_dir / 'input.wav'
    duration = _decode_to_wave(audio_path, pcm_path, emit)
    logger.info('Apple Speech 音频解码完成 duration=%.2fs locale=%s', duration, locale)
    events = TranscriptEvents(duration, emit)
    timed_out = threading.Event()
    with tempfile.TemporaryFile(mode='w+b') as errors:
        process = subprocess.Popen([*command, 'transcribe', locale, str(pcm_path)],
                                   stdout=subprocess.PIPE, stderr=errors,
                                   text=True, encoding='utf-8', errors='strict', bufsize=1)

        def timeout():
            if process.poll() is None:
                timed_out.set()
                _terminate(process)

        # Bound a stuck system request without imposing a short recording limit.
        timer = threading.Timer(max(600, duration * 4 + 300), timeout)
        timer.daemon = True
        emit({'kind': 'progress', 'detail': '正在启动 Apple Speech。', 'device': DEVICE})
        try:
            timer.start()
            assert process.stdout is not None
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Do not include the raw line: it might contain transcript text.
                    raise ValueError('原生语音工具返回了无效的数据。') from None
                events.accept(event)
            returncode = process.wait()
        finally:
            timer.cancel()
            _terminate(process)
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
        errors.seek(0, os.SEEK_END)
        errors.seek(max(0, errors.tell() - 16000))
        diagnostics = errors.read().decode('utf-8', errors='replace').strip()
    if timed_out.is_set():
        raise RuntimeError('Apple Speech 响应超时，请重试。')
    if returncode != 0:
        raise RuntimeError(f'原生语音工具退出异常（{returncode}）。{diagnostics}')
    if events.result is None:
        raise RuntimeError(f'Apple Speech 未返回完整结果，请重试。{diagnostics}')
    logger.info('Apple Speech 转录完成 duration=%.2fs segments=%d', duration, len(events.segments))
    return events.result
