"""Bilibili policy around yt-dlp extraction and PyAV MP3 conversion."""

import re
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.utils import ContentTooShortError, DownloadError, determine_ext, determine_protocol
from yt_dlp.networking.exceptions import RequestError

from .extractors import objects
from .network import MAX_AUDIO_BYTES, MediaRequestError, public_media_url
from .urls import normalize_episode


# Prefer a complete video containing sound. DASH videos may only offer separate
# streams; in that case download the audio stream directly for MP3 conversion.
# Video resolution is irrelevant to transcription, so avoid a large video file.
BILIBILI_FORMAT = 'worst[ext=mp4]/worst/bestaudio[acodec^=mp4a]/bestaudio'


class MediaImportError(ValueError):
    """Application-owned, safe user-facing messages only."""


class SonaBiliBiliIE(BiliBiliIE):
    def extract_formats(self, play_info):
        # Keep upstream parsing and WBI signing. The pinned yt-dlp drops backup
        # URLs, so preserve the API's mirrors for the exact same media stream.
        formats = super().extract_formats(play_info)
        mirrors = {}
        for item in objects(play_info):
            url = item.get('baseUrl') or item.get('base_url') or item.get('url')
            backups = item.get('backupUrl') or item.get('backup_url')
            if isinstance(url, str) and isinstance(backups, list):
                mirrors[url] = [value for value in backups if isinstance(value, str)]
        for fmt in formats:
            fmt['sona_backup_urls'] = mirrors.get(fmt.get('url'), [])
        return formats

    def report_warning(self, message, *args, **kwargs):
        # The pinned extractor otherwise returns a supporter-only preview as a
        # successful video. Never silently transcribe a preview as the full work.
        if 'supporter-only video' in message:
            raise MediaImportError('该视频仅提供试看内容，暂不支持需要登录或充电权限的视频。')
        return super().report_warning(message, *args, **kwargs)

    def _download_playinfo(self, bvid, cid, headers=None, query=None, fatal=True):
        if not cid:
            raise MediaImportError('无法找到指定分P，请检查链接中的 p 参数。')
        # A failed part-specific API request must not fall back to page playinfo,
        # which may describe the first part rather than the requested one.
        return super()._download_playinfo(bvid, cid, headers=headers, query=query, fatal=True)


def extract_video(downloader, source_url):
    _, _, url = normalize_episode(source_url)
    if urlsplit(url).hostname == 'b23.tv':
        response = downloader.urlopen(url)
        try:
            platform, episode, resolved = normalize_episode(response.url)
        finally:
            response.close()
        if platform != 'bilibili' or episode.startswith('short:'):
            raise MediaImportError('短链接未指向受支持的 B站视频，请复制 BV/AV 视频链接。')
        # An explicitly selected part on a short link wins over redirect defaults.
        part = parse_qs(urlsplit(url).query).get('p')
        url = resolved.split('?')[0] + f'?p={part[0]}' if part else resolved
    info = SonaBiliBiliIE(downloader).extract(url)
    if not isinstance(info, dict) or info.get('_type', 'video') != 'video':
        raise MediaImportError('暂仅支持 B站普通单视频或指定分P，不支持番剧、互动视频或旧式分段文件。')
    match = re.fullmatch(r'(BV[a-zA-Z0-9]{10})(?:_p([0-9]+))?', str(info.get('id', '')))
    part = int(parse_qs(urlsplit(url).query)['p'][0])
    if not match or int(match[2] or 1) != part:
        raise MediaImportError('无法确认指定分P，已停止获取，请检查视频链接。')
    canonical = f'https://www.bilibili.com/video/{match[1]}?p={part}'
    # Selection/sorting belongs to yt-dlp. Restrict the candidates to complete
    # HTTP files, so no external downloader, playlist or fragment join is needed.
    formats = []
    for fmt in info.get('formats', []):
        if not fmt.get('url') or fmt.get('fragments') or fmt.get('has_drm'):
            continue
        # Legacy complete-video entries have no ext until yt-dlp normalizes
        # them. Infer it with the upstream helper instead of discarding them.
        extension = fmt.get('ext') or determine_ext(fmt['url'], default_ext=None)
        if (determine_protocol(fmt) in ('http', 'https')
                and extension in ('m4a', 'mp4', 'flv', 'webm', 'mp3', 'flac', 'ogg', 'opus')):
            formats.append({**fmt, 'ext': extension})
    if not formats:
        raise MediaImportError('该视频没有可获取的完整音频或视频文件，暂不支持分片流。')
    info.update(formats=formats, extractor='BiliBili', extractor_key='BiliBili', webpage_url=canonical)
    selected = downloader.process_video_result(info, download=False)
    if not selected.get('url') or selected.get('acodec') == 'none':
        raise MediaImportError('该视频没有可读取的音轨。')
    return selected, canonical


def media_candidates(info):
    urls = []
    for url in [info['url'], *(info.get('sona_backup_urls') or [])]:
        try:
            public_media_url(url)
        except RequestError:
            continue
        if url not in urls:
            urls.append(url)
    if not urls:
        # Preserve the precise guard error if every candidate is unusable.
        public_media_url(info['url'])
        raise MediaImportError('该视频没有可用的下载地址，请重新获取。')
    # Prefer standard HTTPS endpoints over nonstandard edge-node ports. These
    # URLs come from Bilibili's response; never rewrite their host or signature.
    urls.sort(key=lambda url: not (urlsplit(url).scheme == 'https' and urlsplit(url).port in (None, 443)))
    return urls[:4]


def download_media(downloader, info, source, progress, on_retry=None):
    from .transfer import MediaHttpFD

    source = Path(source)
    urls = media_candidates(info)
    for index, url in enumerate(urls):
        # No partial bytes may be mixed between CDN responses. Continue/retry
        # within a single endpoint is still handled by yt-dlp's HTTP downloader.
        source.unlink(missing_ok=True)
        Path(str(source) + '.part').unlink(missing_ok=True)
        try:
            transfer = MediaHttpFD(downloader, downloader.params)
            transfer.add_progress_hook(progress)
            success, _ = transfer.download(str(source), {**info, 'url': url})
            if not success or not source.is_file() or source.stat().st_size == 0:
                raise MediaImportError('音频未下载完整，请重试。')
            if source.stat().st_size > MAX_AUDIO_BYTES:
                raise MediaImportError('音频超过 2 GB，暂不支持导入。')
            return source
        except (RequestError, DownloadError, ContentTooShortError, TimeoutError, ConnectionError) as error:
            # Size limits and local filesystem errors will not improve on a
            # different server. Only network/download failures use another CDN.
            from .errors import failure_metadata

            diagnostic = failure_metadata(error)
            if (diagnostic['code'] == 'media_response_too_large'
                    or (isinstance(error, DownloadError) and diagnostic['error_type'] in
                        ('PermissionError', 'FileNotFoundError', 'IsADirectoryError', 'OSError'))):
                raise
            if index + 1 == len(urls):
                raise
            if on_retry:
                on_retry(error, index + 1)


def prepare_audio(source, directory, extension):
    """Decode the downloaded file incrementally and encode a real MP3."""
    import av

    containers = {'mp4': 'mov', 'm4a': 'mov', 'webm': 'matroska', 'flv': 'flv',
                  'mp3': 'mp3', 'flac': 'flac', 'ogg': 'ogg', 'opus': 'ogg'}
    try:
        av.codec.Codec('libmp3lame', 'w')
    except ValueError:
        raise MediaImportError('当前音频组件缺少 MP3 编码器，请重新安装完整版本的 Sona。') from None
    target = Path(directory) / 'audio.mp3'
    sample_rate = 44100
    samples = 0
    with av.open(str(source), format=containers[extension]) as incoming:
        if not incoming.streams.audio:
            raise MediaImportError('下载内容中没有可读取的音轨。')
        with av.open(str(target), 'w', format='mp3') as outgoing:
            audio = outgoing.add_stream('libmp3lame', rate=sample_rate)
            audio.layout = 'mono'
            audio.format = 's16p'
            audio.bit_rate = 128000
            resampler = av.AudioResampler(format='s16p', layout='mono', rate=sample_rate)

            def mux(packets):
                for packet in packets:
                    outgoing.mux(packet)
                    if target.stat().st_size > MAX_AUDIO_BYTES:
                        raise MediaImportError('音频超过 2 GB，暂不支持导入。')

            def encode(frames):
                nonlocal samples
                for frame in frames:
                    frame.pts = samples
                    frame.time_base = Fraction(1, sample_rate)
                    samples += frame.samples
                    mux(audio.encode(frame))

            for frame in incoming.decode(audio=0):
                # Like transcription, preserve decoded samples without carrying
                # container-specific start offsets into the output timestamps.
                frame.pts = None
                encode(resampler.resample(frame))
            encode(resampler.resample(None))
            mux(audio.encode(None))  # Flush delayed MP3 packets, including the tail.
    if not samples:
        raise MediaImportError('下载内容中的音轨为空。')
    if not 0 < target.stat().st_size <= MAX_AUDIO_BYTES:
        raise MediaImportError('音频转换后为空或超过 2 GB，暂不支持导入。')
    return target


def error_detail(error, stage):
    if isinstance(error, (MediaImportError, MediaRequestError)):
        return str(error)
    from .errors import failure_metadata

    diagnostic = failure_metadata(error)
    if diagnostic['code'] in MediaRequestError.MESSAGES:
        return MediaRequestError.MESSAGES[diagnostic['code']]
    if diagnostic['code'] == 'media_empty_response':
        return 'B站下载节点返回了空内容，已尝试可用的备用节点，请稍后重新获取。'
    if diagnostic['http_status'] == 403:
        return 'B站音频服务器拒绝了下载请求，请稍后重新获取，或检查网络和代理设置。'
    if diagnostic['error_type'] in ('SSLError', 'SSLCertVerificationError', 'CertificateVerifyError'):
        return '与 B站音频服务器建立安全连接失败，请检查网络和代理设置后重试。'
    # Inspect upstream messages only for classification; never expose their
    # contents (which may include signed CDN URLs) to the UI or application log.
    message = str(error).lower()
    if any(word in message for word in ('rate limit', '412', '429', 'risk control', 'video info: 352', 'video info: 401')):
        return 'B站暂时限制了请求，请稍后重试，或检查网络和代理设置。'
    if any(word in message for word in ('login required', 'log in', 'sign in', 'supporter-only', 'premium', '试看')):
        return '该视频需要登录或访问权限，暂仅支持公开可完整播放的视频。'
    if any(word in message for word in ('deleted', 'geo-restricted', '404')):
        return '该视频可能已下架或存在地区限制，请确认链接可以公开播放。'
    if any(word in message for word in ('timed out', 'proxy', 'connection', 'resolve', '403')):
        return '无法连接 B站或音频服务器，请检查网络和代理设置后重试。'
    if stage == 'processing':
        return 'MP3 转换失败，下载内容可能不完整或音频编码暂不受支持。'
    return ('无法解析 B站视频，请确认链接公开有效；也可能是平台页面发生变化。'
            if stage == 'resolving' else '下载或读取音频失败，请检查网络和可用磁盘空间后重试。')
