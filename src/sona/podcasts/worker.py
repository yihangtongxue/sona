"""One acquisition per disposable process; no yt-dlp output enters app logs."""

import math
import os
import time
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import urlsplit


def fetch_episode(job, directory, send, parent_pid):
    from ..file_lock import exclusive_file_lock
    from .process_scope import isolate_download, kill_download_group

    if job['platform'] == 'youtube':
        try:
            isolate_download()
        except OSError:
            send.send({'kind': 'error', 'stage': 'resolving', 'detail': '无法启动受管理的 YouTube 下载进程，请重试。'})
            send.close()
            return

    download_allowed = threading.Event()

    def watch_parent():
        # The parent may authorize a download. EOF means it exited, including on Windows.
        # Stop even if the main thread is stuck in DNS or a blocking network read.
        try:
            while True:
                if send.recv() == 'download':
                    download_allowed.set()
        except (EOFError, OSError):
            if job['platform'] == 'youtube':
                kill_download_group(os.getpid())
            os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        with (exclusive_file_lock(Path(directory).parent / f'{job["id"]}.lock'),
              open(os.devnull, 'w', encoding='utf-8') as output,
              redirect_stdout(output), redirect_stderr(output)):
            _fetch_episode(job, directory, send, parent_pid, download_allowed)
    finally:
        send.close()


def _fetch_episode(job, directory, send, parent_pid, download_allowed):
    # Heavy dependencies live in the spawned process, never the UI bridge thread.
    stage = 'resolving'
    media_error_detail = None
    try:
        import av
        from yt_dlp.globals import plugin_dirs

        # Only application-owned extractors; do not load user yt-dlp plugins.
        plugin_dirs.value = []
        from yt_dlp.utils import determine_ext

        from .extractors import SonaApplePodcastsIE, XiaoyuzhouIE
        from .network import MAX_AUDIO_BYTES, PodcastYoutubeDL, public_media_url
        from ..audio_library import AUDIO_SUFFIXES

        class QuietLogger:
            def debug(self, message):
                pass
            warning = error = info = debug

        def emit(event):
            if os.getppid() != parent_pid:
                raise InterruptedError()
            send.send(event)

        last_progress = 0.0

        def progress(event):
            nonlocal last_progress
            received = int(event.get('downloaded_bytes') or 0)
            total = int(event.get('total_bytes') or 0)
            if max(received, total) > MAX_AUDIO_BYTES:
                raise ValueError('音频超过 2 GB，暂不支持导入。')
            if time.monotonic() - last_progress >= 0.4 or event.get('status') == 'finished':
                emit({'kind': 'progress', 'downloaded_bytes': received, 'total_bytes': total})
                last_progress = time.monotonic()

        options = {
            'quiet': True, 'no_warnings': True, 'logger': QuietLogger(), 'noprogress': True,
            'noplaylist': True, 'cachedir': False, 'socket_timeout': 20,
            'retries': 2, 'extractor_retries': 2, 'continuedl': False,
            'max_filesize': MAX_AUDIO_BYTES, 'progress_hooks': [progress],
            'postprocessors': [], 'fixup': 'never', 'js_runtimes': {},
            'geo_bypass': False,
            'http_headers': {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'},
        }
        if job['platform'] == 'bilibili':
            from .bilibili import BILIBILI_FORMAT

            options['format'] = BILIBILI_FORMAT
        downloader_type = PodcastYoutubeDL
        if job['platform'] == 'youtube':
            from .youtube import runtime_options, error_detail
            from .youtube_network import YoutubeYoutubeDL

            downloader_type = YoutubeYoutubeDL
            media_error_detail = error_detail
            options.update(runtime_options())
        with downloader_type(options, auto_init=False) as downloader:
            if job['platform'] == 'youtube':
                from .youtube import acquire, extract_video

                info = extract_video(downloader, job['source_url'])
                emit({'kind': 'metadata', 'name': str(info.get('title') or 'YouTube 视频')[:500],
                      'source_url': info['webpage_url'], 'podcast_title': str(info.get('uploader') or '')[:500],
                      'cover_url': str(info.get('thumbnail') or '')[:4096], 'duration': info['duration']})
                download_allowed.wait()

                def youtube_event(event):
                    nonlocal stage
                    if event['kind'] in ('subtitles', 'processing'):
                        stage = event['kind']
                    elif event['kind'] == 'audio_fallback':
                        stage = 'downloading'
                    emit(event)

                acquire(downloader, info, job, directory, youtube_event, progress)
                return
            if job['platform'] == 'bilibili':
                from .bilibili import extract_video, download_media, prepare_audio, error_detail

                media_error_detail = error_detail
                info, canonical = extract_video(downloader, job['source_url'])
                title = str(info.get('title') or 'B站视频').strip()[:500]
                duration = info.get('duration')
                duration = float(duration) if isinstance(duration, (int, float)) else 0
                if not math.isfinite(duration) or duration < 0:
                    duration = 0
                emit({'kind': 'metadata', 'name': title, 'source_url': canonical,
                      'podcast_title': str(info.get('uploader') or '')[:500],
                      'cover_url': str(info.get('thumbnail') or '')[:4096], 'duration': duration})
                # The parent binds the canonical BV/part and checks aliases before
                # allowing bytes to download. Duplicates are merged without fetching.
                download_allowed.wait()
                stage = 'downloading'
                downloader.downloading_audio = True
                source = Path(directory) / 'source.media'

                def retry_node(error, attempt):
                    from .errors import failure_metadata

                    emit({'kind': 'retrying_download', 'attempt': attempt, 'diagnostic': failure_metadata(error)})

                download_media(downloader, info, source, progress, retry_node)
                stage = 'processing'
                emit({'kind': 'processing'})
                target = prepare_audio(source, directory, info['ext'])
                if not target.is_file() or not 0 < target.stat().st_size <= MAX_AUDIO_BYTES:
                    raise ValueError('音频转换后为空或超过 2 GB，暂不支持导入。')
                with av.open(str(target)) as container:
                    if not container.streams.audio or next(container.decode(audio=0), None) is None:
                        raise ValueError('下载内容中没有可读取的音轨。')
                source.unlink()
                emit({'kind': 'complete', 'suffix': target.suffix, 'size_bytes': target.stat().st_size})
                return
            extractor = XiaoyuzhouIE(downloader) if job['platform'] == 'xiaoyuzhou' else SonaApplePodcastsIE(downloader)
            info = extractor.extract(job['source_url'])
            if not isinstance(info, dict) or not info.get('url'):
                raise ValueError('该单集没有公开的音频下载地址。')
            media_url = public_media_url(info['url'])
            suffix = '.' + determine_ext(media_url, default_ext='').lower()
            if suffix not in AUDIO_SUFFIXES:
                # Some CDNs omit extensions. Read headers without consuming audio.
                downloader.downloading_audio = True
                response = downloader.urlopen(media_url)
                try:
                    mime = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                finally:
                    response.close()
                suffix = {'audio/mpeg': '.mp3', 'audio/mp3': '.mp3', 'audio/mp4': '.m4a',
                          'audio/x-m4a': '.m4a', 'audio/aac': '.aac', 'audio/ogg': '.ogg',
                          'audio/flac': '.flac', 'audio/wav': '.wav', 'audio/x-wav': '.wav'}.get(mime, '')
            if suffix not in AUDIO_SUFFIXES or '.m3u8' in urlsplit(media_url).path.lower():
                raise ValueError('该单集未提供受支持的音频文件，暂不支持 HLS 流或受保护内容。')
            title = str(info.get('title') or '播客单集').strip()[:500]
            duration = info.get('duration')
            duration = float(duration) if isinstance(duration, (int, float)) else 0
            if not math.isfinite(duration) or duration < 0:
                duration = 0
            emit({'kind': 'metadata', 'name': title, 'podcast_title': str(info.get('series') or '')[:500],
                  'cover_url': str(info.get('thumbnail') or '')[:4096], 'duration': duration})
            stage = 'downloading'
            downloader.downloading_audio = True
            target = Path(directory) / ('audio' + suffix)
            # A direct HTTP file only: no external downloader, playlist, executable,
            # conversion, thumbnail write, or filename derived from remote metadata.
            from .transfer import MediaHttpFD
            transfer = MediaHttpFD(downloader, downloader.params)
            transfer.add_progress_hook(progress)
            success, _ = transfer.download(str(target), {
                'id': job['episode_id'], 'title': title, 'url': media_url,
                'ext': suffix[1:], 'http_headers': {'Referer': job['source_url'], **(info.get('http_headers') or {})},
            })
            if not success or not target.is_file() or not 0 < target.stat().st_size <= MAX_AUDIO_BYTES:
                raise ValueError('音频未下载完整，请重试。')
            # Inspect and decode one frame; transcription performs full decoding later.
            with av.open(str(target)) as container:
                if not container.streams.audio or next(container.decode(audio=0), None) is None:
                    raise ValueError('下载内容中没有可读取的音轨。')
            emit({'kind': 'complete', 'suffix': suffix, 'size_bytes': target.stat().st_size})
    except (BrokenPipeError, EOFError, InterruptedError):
        pass
    except Exception as error:
        from .errors import failure_metadata

        # Only our fixed messages / expected Xiaoyuzhou errors are user-facing.
        # Network/extractor tracebacks may include expiring URLs and credentials.
        detail = ('无法解析单集，请检查链接是否公开有效；也可能是平台页面发生变化。'
                  if stage == 'resolving' else '下载或读取音频失败，请检查网络和可用磁盘空间后重试。')
        if media_error_detail:
            detail = media_error_detail(error, stage)
        if isinstance(error, ModuleNotFoundError):
            detail = '缺少链接导入组件，请同步项目依赖或重新安装完整版本的 Sona。'
        if isinstance(error, ValueError) and str(error).startswith(('该单集', '音频', '下载内容')):
            detail = str(error)
        if type(error).__name__ == 'ExtractorError' and getattr(error, 'expected', False):
            original = str(error)
            if original.startswith(('该单集需要访问权限', '公开页面没有可下载')):
                detail = original
        try:
            send.send({'kind': 'error', 'stage': stage, 'detail': detail,
                       'diagnostic': failure_metadata(error)})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send.close()
