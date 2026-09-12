"""YouTube subtitles first; yt-dlp owns extraction and native media downloads."""

import math
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.utils import DownloadError, determine_protocol

from .bilibili import MediaImportError, prepare_audio
from .captions import MAX_SUBTITLE_BYTES, SubtitleError, parse_subtitles, save_payload
from .errors import failure_metadata
from .network import MAX_AUDIO_BYTES, public_media_url
from .transfer import MediaHttpFD
from .urls import normalize_episode


YOUTUBE_FORMAT = 'bestaudio[ext=m4a]/bestaudio/best[ext=mp4]/best'


def runtime_options():
    # EJS is installed with a matching pinned version, never fetched at runtime.
    import yt_dlp_ejs  # noqa: F401

    if getattr(sys, 'frozen', False):
        binary = Path(sys._MEIPASS) / 'sona' / 'native' / ('deno.exe' if sys.platform == 'win32' else 'deno')
    else:
        import deno

        binary = Path(deno.find_deno_bin())
    if not binary.is_file():
        raise MediaImportError('缺少 YouTube 获取组件，请同步项目依赖或重新安装完整版本的 Sona。')
    return {'js_runtimes': {'deno': {'path': str(binary)}}, 'remote_components': [],
            'http_headers': {'Accept-Encoding': 'identity'},
            'format': YOUTUBE_FORMAT, 'ignore_no_formats_error': True,
            'skip_unavailable_fragments': False, 'fragment_retries': 2, 'concurrent_fragment_downloads': 1,
            'extractor_args': {'youtube': {'skip': ['translated_subs']}},
            'writesubtitles': False, 'writeautomaticsub': False}


def extract_video(downloader, source_url):
    platform, identifier, canonical = normalize_episode(source_url)
    if platform != 'youtube':
        raise MediaImportError('请粘贴 YouTube 视频链接。')
    # Extraction must succeed even if there is no downloadable audio format:
    # subtitles may still be available, without requiring a speech model.
    info = YoutubeIE(downloader).extract(canonical)
    if not isinstance(info, dict) or info.get('_type', 'video') != 'video' or info.get('id') != identifier:
        raise MediaImportError('无法确认 YouTube 单视频内容，暂不支持播放列表。')
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
        raise MediaImportError('暂不支持直播、预告或仍在处理的直播回放。')
    if info.get('availability') in ('private', 'premium_only', 'subscriber_only', 'needs_auth') or info.get('has_drm'):
        raise MediaImportError('该视频需要登录、付费或访问权限，暂仅支持公开视频。')
    duration = info.get('duration')
    info['duration'] = float(duration) if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else 0
    # The raw extractor annotates formats before process_video_result promotes
    # their language to top-level metadata. Prefer its explicit original track.
    audio_formats = [fmt for fmt in info.get('formats', []) if fmt.get('acodec') != 'none' and fmt.get('language')]
    original_languages = {fmt['language'] for fmt in audio_formats if fmt.get('language_preference') == 10}
    if len(original_languages) == 1:
        info['language'] = next(iter(original_languages))
    elif not info.get('language'):
        languages = {fmt['language'] for fmt in audio_formats if not fmt['language'].endswith('-desc')}
        if len(languages) == 1:
            info['language'] = next(iter(languages))
    info.update(webpage_url=canonical, extractor='youtube', extractor_key='Youtube')
    return info


def subtitle_candidates(info, language='original'):
    tracks = []
    for field, kind in (('subtitles', 'manual_subtitles'), ('automatic_captions', 'automatic_subtitles')):
        for code, formats in (info.get(field) or {}).items():
            if code == 'live_chat':
                continue
            for fmt in formats:
                url = fmt.get('url')
                if not url or fmt.get('ext') not in ('json3', 'vtt'):
                    continue
                try:
                    public_media_url(url)
                except RequestError:
                    continue
                query = parse_qs(urlsplit(url).query)
                # automatic_captions contains machine translations as well as
                # original ASR. Never silently mistake a translation for ASR.
                if query.get('tlang'):
                    continue
                track_language = query.get('lang', [code.removesuffix('-orig')])[0]
                tracks.append({**fmt, 'language': track_language, 'source_kind': kind,
                               'original': code.endswith('-orig')})
    if language == 'original':
        originals = {item['language'] for item in tracks if item['original']}
        language = info.get('language')
        if not language and len(originals) == 1:
            language = next(iter(originals))
        if not language:
            languages = {item['language'] for item in tracks}
            language = next(iter(languages)) if len(languages) == 1 else None
        if not language:
            return []  # Ambiguous language: use original audio, not a random translation.
    base = language.lower().split('-')[0]
    tracks = [item for item in tracks if item['language'].lower().split('-')[0] == base]
    tracks.sort(key=lambda item: (item['source_kind'] != 'manual_subtitles',
                                  item['language'].lower() != language.lower(), item['ext'] != 'json3'))
    unique = {}
    for item in tracks:
        unique.setdefault(item['url'], item)
    return list(unique.values())[:8]


def fetch_subtitles(downloader, info, directory, language, emit):
    candidates = subtitle_candidates(info, language)
    if not candidates:
        return 'missing'
    emit({'kind': 'subtitles'})
    # At most four requests per import. Try a different encoding/client or ASR
    # on failure; do not hammer a rate-limited subtitle endpoint.
    manual = [track for track in candidates if track['source_kind'] == 'manual_subtitles']
    automatic = [track for track in candidates if track['source_kind'] == 'automatic_subtitles']
    attempts = manual[:2] + automatic[:2] if manual and automatic else candidates[:4]
    for track in attempts:
        try:
            extensions = {}
            if track.get('impersonate'):
                target, _ = downloader._parse_impersonate_targets(track['impersonate'])
                if target is not None:
                    extensions['impersonate'] = target
            headers = {'Referer': info.get('webpage_url', 'https://www.youtube.com/'), **(info.get('http_headers') or {})}
            with downloader.urlopen(Request(track['url'], headers=headers, extensions=extensions)) as response:
                payload = response.read(MAX_SUBTITLE_BYTES + 1)
            segments = parse_subtitles(payload, track['ext'], track['source_kind'] == 'automatic_subtitles')
            result = {'text': '\n'.join(cue['text'] for cue in segments), 'segments': segments,
                      'language': track['language'], 'source_kind': track['source_kind'],
                      'duration': info['duration'] or max(cue['end'] for cue in segments)}
            save_payload(directory, result)
            emit({'kind': 'complete', 'result_kind': 'subtitles', 'suffix': '',
                  'size_bytes': len(result['text'].encode('utf-8'))})
            return 'complete'
        except (RequestError, DownloadError, SubtitleError) as error:
            emit({'kind': 'subtitle_retry', 'diagnostic': failure_metadata(error)})
            if failure_metadata(error)['http_status'] == 429:
                break
    return 'failed'


def download_audio(downloader, info, directory, progress):
    formats = [fmt for fmt in info.get('formats', []) if fmt.get('url') and not fmt.get('has_drm')
               and fmt.get('acodec') != 'none' and fmt.get('ext') in ('m4a', 'mp4', 'webm', 'opus', 'ogg')
               and determine_protocol(fmt) in ('http', 'https', 'http_dash_segments')]
    original = [fmt for fmt in formats if fmt.get('language_preference') == 10]
    if original:
        formats = original
    elif info.get('language'):
        language = info['language'].lower().split('-')[0]
        matching = [fmt for fmt in formats if fmt.get('language')
                    and fmt['language'].lower().split('-')[0] == language and not fmt['language'].endswith('-desc')]
        formats = matching or [fmt for fmt in formats if not fmt.get('language')]
    if not formats:
        raise MediaImportError('未取得可下载的 YouTube 音轨，可能受到平台验证或网络限制，请稍后重新获取。')
    selected = downloader.process_video_result({**info, 'formats': formats}, download=False)
    if not selected.get('url') or selected.get('acodec') == 'none':
        raise MediaImportError('该视频没有可用音轨。')
    protocol = determine_protocol(selected)
    downloader.downloading_audio = True
    source = Path(directory) / 'source.media'
    # Only yt-dlp native HTTP/DASH downloaders, never an external executable.
    transfer_type = MediaHttpFD if protocol in ('http', 'https') else get_suitable_downloader(selected, downloader.params)
    from yt_dlp.downloader.dash import DashSegmentsFD

    if transfer_type not in (MediaHttpFD, DashSegmentsFD):
        raise MediaImportError('该视频音轨使用了暂不支持的下载协议。')
    transfer = transfer_type(downloader, downloader.params)
    transfer.add_progress_hook(progress)
    success, _ = transfer.download(str(source), selected)
    if not success or not source.is_file() or not 0 < source.stat().st_size <= MAX_AUDIO_BYTES:
        raise MediaImportError('音频未下载完整或超过 2 GB，请重新获取。')
    return source, selected['ext']


def acquire(downloader, info, job, directory, emit, progress):
    if job.get('strategy', 'subtitle_first') != 'transcribe':
        outcome = fetch_subtitles(downloader, info, directory, job.get('subtitle_language', 'original'), emit)
        if outcome == 'complete':
            return
        reason = '字幕获取失败' if outcome == 'failed' else '没有可用的所选语言字幕'
        emit({'kind': 'audio_fallback', 'detail': reason + '，正在下载原音轨并转录。'})
    else:
        emit({'kind': 'audio_fallback', 'detail': '已选择本地转录，正在下载原音轨。'})
    source, extension = download_audio(downloader, info, directory, progress)
    emit({'kind': 'processing'})
    target = prepare_audio(source, directory, extension)
    source.unlink()
    emit({'kind': 'complete', 'suffix': target.suffix, 'size_bytes': target.stat().st_size})


def error_detail(error, stage):
    if isinstance(error, (MediaImportError, SubtitleError)):
        return str(error)
    diagnostic = failure_metadata(error)
    from .network import MediaRequestError

    if diagnostic['code'] in MediaRequestError.MESSAGES:
        return MediaRequestError.MESSAGES[diagnostic['code']]
    if diagnostic['http_status'] in (403, 429) or any(word in str(error).lower() for word in ('sign in', 'bot', 'po token')):
        return 'YouTube 拒绝了请求或要求验证，请检查网络和代理设置，稍后重新获取。'
    if stage == 'processing':
        return 'YouTube 音轨转换失败，下载内容可能不完整。'
    return ('无法获取 YouTube 视频信息，请确认视频公开可访问，并检查网络和代理设置。'
            if stage == 'resolving' else 'YouTube 音轨下载失败，请检查网络和可用磁盘空间后重新获取。')
