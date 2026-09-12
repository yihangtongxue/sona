"""Bounded subtitle parsing and a durable, text-only import payload."""

import html
import json
import math
import re
from pathlib import Path

from yt_dlp import webvtt


MAX_SUBTITLE_BYTES = 8 * 1024**2
MAX_SEGMENTS = 100000


class SubtitleError(ValueError):
    """Fixed messages only; subtitle payloads never belong in error logs."""


def clean_text(text, markup=False):
    # Strip markup before decoding entities so spoken literal <...> survives.
    if markup:
        text = re.sub(r'<[^>]*>', '', text)
    return html.unescape(text).replace('\x00', '').strip()


def parse_subtitles(payload, extension, automatic=False):
    if not payload or len(payload) > MAX_SUBTITLE_BYTES:
        raise SubtitleError('字幕为空或超过大小限制。')
    cues = []
    try:
        if extension == 'json3':
            data = json.loads(payload)
            for event in data['events']:
                parts = event.get('segs')
                if not parts:
                    continue  # Window/style events carry no spoken content.
                text = ''.join(part.get('utf8', '') for part in parts)
                if not text.strip():
                    continue
                start = float(event['tStartMs']) / 1000
                end = start + float(event['dDurationMs']) / 1000
                cues.append({'start': start, 'end': end, 'text': clean_text(text)})
        elif extension == 'vtt':
            for block in webvtt.parse_fragment(payload):
                if isinstance(block, webvtt.CueBlock):
                    cues.append({'start': block.start / 90000, 'end': block.end / 90000,
                                 'text': clean_text(block.text, markup=True)})
        else:
            raise SubtitleError('不支持该字幕格式。')
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, webvtt.ParseError):
        raise SubtitleError('字幕内容无法解析。') from None
    if len(cues) > MAX_SEGMENTS:
        raise SubtitleError('字幕段落过多。')
    segments = []
    for cue in sorted(cues, key=lambda item: item['start']):
        start, end, text = cue['start'], cue['end'], cue['text']
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
            raise SubtitleError('字幕时间戳无效。')
        if not text:
            continue
        if segments:
            previous = segments[-1]
            # Only overlapping automatic display updates are deduplicated.
            # Repeated speech at later timestamps and repetition within a cue
            # remain intact. Manual subtitles keep their authored wording.
            overlap = start < previous['end']
            if text == previous['text'] and (start == previous['start'] or (automatic and overlap)):
                previous['end'] = max(previous['end'], end)
                continue
            if automatic and overlap:
                if text.startswith(previous['text'] + ' ') or text.startswith(previous['text'] + '\n'):
                    previous.update(text=text, end=max(previous['end'], end))
                    continue
                old_lines, new_lines = previous['text'].splitlines(), text.splitlines()
                for count in range(min(len(old_lines), len(new_lines)), 0, -1):
                    if old_lines[-count:] == new_lines[:count]:
                        text = '\n'.join(new_lines[count:]).strip()
                        break
                if not text:
                    continue
        segments.append({**cue, 'text': text})
    if not segments:
        raise SubtitleError('字幕没有可用正文。')
    return segments


def validate_result(result):
    """Validate both freshly parsed subtitles and a crash-recovery payload."""
    try:
        if result['source_kind'] not in ('manual_subtitles', 'automatic_subtitles'):
            raise ValueError()
        if not isinstance(result['language'], str) or not re.fullmatch(r'[A-Za-z0-9-]{1,40}', result['language']):
            raise ValueError()
        duration = result['duration']
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
            raise ValueError()
        segments = result['segments']
        if not isinstance(segments, list) or not 0 < len(segments) <= MAX_SEGMENTS:
            raise ValueError()
        last = -1
        for cue in segments:
            start, end, text = cue['start'], cue['end'], cue['text']
            if not (isinstance(start, (int, float)) and isinstance(end, (int, float))
                    and math.isfinite(start) and math.isfinite(end) and 0 <= start < end and start >= last
                    and isinstance(text, str) and text.strip()):
                raise ValueError()
            last = start
        if result['text'] != '\n'.join(cue['text'] for cue in segments):
            raise ValueError()
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_SUBTITLE_BYTES:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise SubtitleError('字幕结果不完整或格式无效，请重新获取。') from None
    return result


def save_payload(directory, result):
    validate_result(result)
    target = Path(directory) / 'subtitles.json'
    pending = target.with_suffix('.tmp')
    pending.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    pending.replace(target)


def load_payload(directory):
    path = Path(directory) / 'subtitles.json'
    with path.open('rb') as stream:
        payload = stream.read(MAX_SUBTITLE_BYTES + 1)
    if len(payload) > MAX_SUBTITLE_BYTES:
        raise SubtitleError('字幕结果超过大小限制。')
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeError):
        raise SubtitleError('字幕结果损坏，请重新获取。') from None
    return validate_result(result)
