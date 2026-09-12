"""Bound audio working memory independently of the recording's total length."""

from contextlib import closing
import math
import re
import time


SAMPLE_RATE = 16000
CHUNK_SECONDS = 30 * 60
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_SECONDS


def pcm_frames(path, format):
    import av

    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError('文件中没有可识别的音轨。')
        resampler = av.AudioResampler(format=format, layout='mono', rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                yield converted.to_ndarray().reshape(-1)
        for converted in resampler.resample(None):
            yield converted.to_ndarray().reshape(-1)


def audio_chunks(path, emit, *, format='flt', chunk_samples=CHUNK_SAMPLES):
    """Yield consecutive PCM views; consume each before requesting the next.

    One reusable buffer, no whole-recording concatenation, no seek or dropped
    samples at frame boundaries. Actual decoded samples determine duration.
    """
    import numpy as np

    if chunk_samples <= 0:
        raise ValueError('音频分段长度必须大于零。')
    buffer = None
    used = 0
    total = 0
    last_progress = time.monotonic()
    with closing(pcm_frames(path, format)) as frames:
        for frame in frames:
            if not len(frame):
                continue
            if buffer is None:
                buffer = np.empty(chunk_samples, dtype=np.float32 if format == 'flt' else '<i2')
            offset = 0
            while offset < len(frame):
                count = min(chunk_samples - used, len(frame) - offset)
                buffer[used:used + count] = frame[offset:offset + count]
                used += count
                offset += count
                total += count
                now = time.monotonic()
                if now - last_progress >= 2:
                    emit({'kind': 'progress', 'detail': f'正在读取第 {(total - 1) // chunk_samples + 1} 段音频。'})
                    last_progress = now
                if used == chunk_samples:
                    yield buffer
                    used = 0
        if used:
            yield buffer[:used]
    if not total:
        raise ValueError('音频解码后为空。')


class ChunkResults:
    def __init__(self):
        self.samples = 0
        self.segments = []
        self.texts = []
        self.languages = {}
        self.device = ''
        self._last_language = ''

    def progress(self, emit, index):
        offset = self.samples / SAMPLE_RATE

        def report(event):
            if event.get('kind') == 'progress':
                detail = event.get('detail', '')
                match = re.fullmatch(r'已处理至 (\d+)分(\d+)秒。', detail)
                if match:
                    seconds = int(offset) + int(match[1]) * 60 + int(match[2])
                    detail = f'已处理至 {seconds // 60}分{seconds % 60}秒。'
                event = {**event, 'detail': f'第 {index} 段 · {detail}'}
            emit(event)

        return report

    def append(self, result, samples):
        offset = self.samples / SAMPLE_RATE
        duration = samples / SAMPLE_RATE
        for segment in result['segments']:
            start, end = float(segment['start']), float(segment['end'])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                raise ValueError('转录返回了无效的时间戳。')
            self.segments.append({**segment, 'start': offset + min(start, duration),
                                  'end': offset + min(end, duration)})
        self.samples += samples
        text = result['text'].strip()
        if text:
            self.texts.append(text)
            language = result['language']
            self.languages[language] = self.languages.get(language, 0) + samples
        self.device = result['device']
        self._last_language = result['language']

    def result(self):
        if not self.samples:
            raise ValueError('音频解码后为空。')
        return {'text': '\n'.join(self.texts), 'segments': self.segments,
                'duration': self.samples / SAMPLE_RATE, 'device': self.device,
                'language': max(self.languages, key=self.languages.get) if self.languages else self._last_language}
