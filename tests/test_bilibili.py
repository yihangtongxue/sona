"""Bilibili import regressions for manual execution; no network or inference."""

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sona.database import ModelRepository, SCHEMA, SCHEMA_VERSION
from sona.podcasts.bilibili import (BILIBILI_FORMAT, MediaImportError, SonaBiliBiliIE,
                                   download_media, extract_video, media_candidates, prepare_audio, error_detail)
from sona.podcasts.network import PodcastYoutubeDL
from sona.podcasts.errors import failure_metadata
from sona.podcasts.repository import ImportRepository
from sona.podcasts.urls import normalize_episode


BV = 'BV1xx411c7mD'
VIDEO = f'https://www.bilibili.com/video/{BV}?p=1'
SHORT = 'https://b23.tv/AbCd123'
AV = 'https://www.bilibili.com/video/av123456'


class LinkTests(unittest.TestCase):
    def test_part_identity_and_share_variants(self):
        expected = ('bilibili', f'{BV}:p1', VIDEO)
        for value in (VIDEO, VIDEO.split('?')[0], VIDEO + '&spm_id_from=share',
                      VIDEO.replace('www.', 'm.'), VIDEO.replace('BV', 'bv'),
                      f'【分享视频】 {VIDEO}。'):
            with self.subTest(value=value):
                self.assertEqual(normalize_episode(value), expected)
        self.assertEqual(normalize_episode(VIDEO.replace('p=1', 'p=02')),
                         ('bilibili', f'{BV}:p2', VIDEO.replace('p=1', 'p=2')))
        self.assertEqual(normalize_episode(AV.replace('av123456', 'AV00123456'))[1], 'av123456:p1')
        self.assertEqual(normalize_episode(f'分享视频 {SHORT}?share_source=copy')[2], SHORT)

    def test_rejects_invalid_parts_and_unsupported_sources(self):
        for value in (VIDEO + '&p=2', VIDEO.replace('p=1', 'p=0'), VIDEO.replace('p=1', 'p=-1'),
                      VIDEO.replace('p=1', 'p='), VIDEO.replace('p=1', 'p=1.5'),
                      VIDEO.replace('bilibili.com', 'bilibili.com.evil.example'),
                      VIDEO.replace('https://', 'https://user:pass@'),
                      VIDEO.replace('https://', 'file://'),
                      VIDEO.replace('www.bilibili.com', 'www.bilibili.com:8443'),
                      'https://space.bilibili.com/123', 'https://live.bilibili.com/123',
                      'https://www.bilibili.com/bangumi/play/ep123',
                      f'两个链接 {SHORT} {VIDEO}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_episode(value)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'sona.sqlite3'
        ModelRepository(self.database, ())
        self.repository = ImportRepository(self.database)

    def resolving(self, url):
        identifier = self.repository.create(url)['id']
        self.repository.patch(identifier, status='resolving')
        return identifier

    def test_short_and_av_merge_before_download_and_keep_aliases(self):
        target = self.resolving(VIDEO)
        for url in (SHORT, AV):
            candidate = self.resolving(url)
            self.assertEqual(self.repository.resolve_source(candidate, VIDEO), target)
            self.assertIsNone(self.repository.get(candidate))
            self.assertEqual(self.repository.create(url), {'id': target, 'existing': True})
        self.assertNotEqual(self.repository.create(VIDEO.replace('p=1', 'p=2'))['id'], target)

    def test_first_short_link_keeps_job_id_after_resolution_and_restart(self):
        identifier = self.resolving(SHORT)
        self.assertEqual(self.repository.resolve_source(identifier, VIDEO), identifier)
        self.assertEqual(self.repository.get(identifier)['source_url'], VIDEO)
        ModelRepository(self.database, ())
        reopened = ImportRepository(self.database)
        self.assertEqual(reopened.create(SHORT)['id'], identifier)
        self.assertEqual(reopened.create(VIDEO)['id'], identifier)

    def test_cancelled_resolution_cannot_merge_or_revive_job(self):
        identifier = self.resolving(SHORT)
        self.repository.cancel(identifier)
        self.assertIsNone(self.repository.resolve_source(identifier, VIDEO))
        self.assertEqual(self.repository.get(identifier)['status'], 'cancelling')

    def test_delete_clears_aliases_and_allows_import_again(self):
        identifier = self.resolving(SHORT)
        self.repository.resolve_source(identifier, VIDEO)
        self.repository.patch(identifier, status='failed')
        self.repository.delete_pending(identifier)
        self.assertFalse(self.repository.create(SHORT)['existing'])
        self.assertFalse(self.repository.create(VIDEO)['existing'])


class MigrationTests(unittest.TestCase):
    def test_v7_upgrade_preserves_podcast_audio_and_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'sona.sqlite3'
            with sqlite3.connect(database) as db:
                for statement in SCHEMA:
                    if not any(f'CREATE TABLE IF NOT EXISTS {table}' in statement
                               for table in ('media_import_aliases', 'subtitle_results')):
                        statement = statement.replace("'xiaoyuzhou','apple','bilibili','youtube'", "'xiaoyuzhou','apple'")
                        statement = '\n'.join(line for line in statement.splitlines()
                                              if 'strategy TEXT' not in line and 'subtitle_language TEXT' not in line)
                        db.execute(statement)
                db.execute('PRAGMA user_version=7')
                db.execute("INSERT INTO podcast_imports(id,platform,episode_id,source_url,status,name) "
                           "VALUES ('saved','apple','123','https://podcasts.apple.com/cn/podcast/id1?i=123','imported','保留标题')")
                db.execute("INSERT INTO audio_files(id,name,suffix,size_bytes) VALUES ('saved','保留标题','.mp3',42)")
                db.execute("UPDATE transcription_tasks SET status='completed',detail='保留转录状态' WHERE audio_id='saved'")
                db.execute("INSERT INTO manuscripts(id,title,source_text,model_json,body) "
                           "VALUES ('manuscript','保留文稿','原文','{}','正文')")
            ModelRepository(database, ())
            ModelRepository(database, ())  # Migration must be idempotent.
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(db.execute('SELECT name FROM podcast_imports').fetchone()[0], '保留标题')
                self.assertEqual(db.execute('SELECT size_bytes FROM audio_files').fetchone()[0], 42)
                self.assertEqual(db.execute('SELECT status FROM transcription_tasks').fetchone()[0], 'completed')
                self.assertEqual(db.execute('SELECT body FROM manuscripts').fetchone()[0], '正文')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertFalse(ImportRepository(database).create(VIDEO)['existing'])


class ExtractionTests(unittest.TestCase):
    def downloader(self):
        downloader = PodcastYoutubeDL({'quiet': True, 'no_warnings': True, 'cachedir': False,
                                      'noplaylist': True, 'js_runtimes': {},
                                      'format': BILIBILI_FORMAT}, auto_init=False)
        self.addCleanup(downloader.close)
        return downloader

    def fixture(self):
        return {'id': BV + '_p1', 'title': '视频标题', 'uploader': '作者',
                'http_headers': {'Referer': VIDEO}, 'formats': [
                    {'format_id': 'video', 'url': 'https://cdn.example/video.m4s', 'ext': 'mp4',
                     'vcodec': 'avc1', 'acodec': 'none', 'height': 1080},
                    {'format_id': 'aac', 'url': 'https://cdn.example/audio.m4s', 'ext': 'm4a',
                     'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 128},
                ]}

    def test_upstream_selects_audio_and_retains_referer(self):
        downloader = self.downloader()
        with patch.object(SonaBiliBiliIE, 'extract', return_value=self.fixture()):
            selected, canonical = extract_video(downloader, AV)
        self.assertEqual(canonical, VIDEO)
        self.assertEqual(selected['format_id'], 'aac')
        self.assertEqual(selected['http_headers']['Referer'], VIDEO)

    def test_prefers_complete_video_when_audio_and_video_are_both_present(self):
        info = self.fixture()
        info['formats'].extend([
            {'format_id': 'combined360', 'url': 'https://cdn.example/360.mp4', 'ext': 'mp4',
             'vcodec': 'avc1', 'acodec': 'mp4a.40.2', 'height': 360},
            {'format_id': 'combined720', 'url': 'https://cdn.example/720.mp4', 'ext': 'mp4',
             'vcodec': 'avc1', 'acodec': 'mp4a.40.2', 'height': 720},
        ])
        with patch.object(SonaBiliBiliIE, 'extract', return_value=info):
            selected, _ = extract_video(self.downloader(), VIDEO)
        self.assertEqual(selected['format_id'], 'combined360')

    def test_complete_video_without_ext_is_normalized_before_selection(self):
        info = self.fixture()
        info['formats'].append({'format_id': 'legacy', 'url': 'https://cdn.example/video.mp4',
                                'vcodec': 'avc1', 'acodec': 'mp4a.40.2'})
        with patch.object(SonaBiliBiliIE, 'extract', return_value=info):
            selected, _ = extract_video(self.downloader(), VIDEO)
        self.assertEqual(selected['format_id'], 'legacy')
        self.assertEqual(selected['ext'], 'mp4')

    def test_selected_audio_preserves_only_its_own_backup_urls(self):
        from yt_dlp.extractor.bilibili import BiliBiliIE

        info = self.fixture()
        upstream_formats = info['formats']
        video, audio = upstream_formats
        play_info = {'dash': {
            'video': [{'baseUrl': video['url'], 'backupUrl': ['https://backup.example/video.m4s']}],
            'audio': [{'base_url': audio['url'], 'backup_url': ['https://backup.example/audio.m4s']}],
        }}
        with patch.object(BiliBiliIE, 'extract_formats', return_value=upstream_formats):
            info['formats'] = SonaBiliBiliIE().extract_formats(play_info)
        with patch.object(SonaBiliBiliIE, 'extract', return_value=info):
            selected, _ = extract_video(self.downloader(), VIDEO)
        self.assertEqual(selected['sona_backup_urls'], ['https://backup.example/audio.m4s'])

    def test_short_redirect_preserves_selected_part_and_closes_response(self):
        downloader = self.downloader()
        response = SimpleNamespace(url=VIDEO, close=Mock())
        info = self.fixture()
        info['id'] = BV + '_p2'
        with (patch.object(downloader, 'urlopen', return_value=response),
              patch.object(SonaBiliBiliIE, 'extract', return_value=info) as extractor):
            _, canonical = extract_video(downloader, SHORT + '?p=2')
        self.assertEqual(canonical, VIDEO.replace('p=1', 'p=2'))
        extractor.assert_called_once_with(canonical)
        response.close.assert_called_once()

    def test_short_redirect_never_extracts_another_platform(self):
        downloader = self.downloader()
        response = SimpleNamespace(url='https://podcasts.apple.com/cn/podcast/id1?i=123', close=Mock())
        with (patch.object(downloader, 'urlopen', return_value=response),
              patch.object(SonaBiliBiliIE, 'extract') as extractor,
              self.assertRaises(MediaImportError)):
            extract_video(downloader, SHORT)
        extractor.assert_not_called()
        response.close.assert_called_once()

    def test_playlists_wrong_part_and_fragment_only_video_are_rejected(self):
        fixtures = [{'_type': 'playlist', 'entries': []}, {'_type': 'multi_video', 'entries': []},
                    {**self.fixture(), 'id': BV + '_p2'}]
        fragmented = copy.deepcopy(self.fixture())
        for fmt in fragmented['formats']:
            fmt['fragments'] = [{'url': fmt['url']}]
        fixtures.append(fragmented)
        for info in fixtures:
            with (self.subTest(info=info), patch.object(SonaBiliBiliIE, 'extract', return_value=info),
                  self.assertRaises(MediaImportError)):
                extract_video(self.downloader(), VIDEO)

    def test_preview_warning_cannot_become_successful_download(self):
        with self.assertRaises(MediaImportError):
            SonaBiliBiliIE().report_warning('This is a supporter-only video, only the preview will be extracted')
        with self.assertRaises(MediaImportError):
            SonaBiliBiliIE()._download_playinfo(BV, None)

    def test_part_api_failure_cannot_fall_back_to_first_part(self):
        from yt_dlp.extractor.bilibili import BiliBiliIE
        from yt_dlp.utils import ExtractorError

        with (patch.object(BiliBiliIE, '_download_playinfo', side_effect=ExtractorError('API unavailable')) as upstream,
              self.assertRaises(ExtractorError)):
            SonaBiliBiliIE()._download_playinfo(BV, 123, fatal=False)
        self.assertTrue(upstream.call_args.kwargs['fatal'])

    def test_error_messages_never_expose_signed_urls(self):
        for message in ('HTTP Error 412', 'Login required', 'deleted', 'Connection refused', 'unknown'):
            detail = error_detail(RuntimeError(message + ' https://cdn.example/?secret=fixture'), 'resolving')
            self.assertNotIn('secret', detail)
            self.assertNotIn('https://', detail)

    def test_wrapped_transport_error_keeps_safe_diagnostic_reason(self):
        from sona.podcasts.network import MediaRequestError
        from yt_dlp.networking.exceptions import NoSupportingHandlers

        error = NoSupportingHandlers([], [MediaRequestError('media_unsupported_port')])
        diagnostic = failure_metadata(error)
        self.assertEqual(diagnostic['code'], 'media_unsupported_port')
        self.assertEqual(diagnostic['error_type'], 'MediaRequestError')
        self.assertEqual(error_detail(error, 'downloading'), MediaRequestError.MESSAGES['media_unsupported_port'])

    def test_diagnostics_capture_http_status_without_url_or_message(self):
        from yt_dlp.networking.exceptions import HTTPError
        from yt_dlp.utils import DownloadError

        response = SimpleNamespace(status=403, reason='signed https://cdn.example/?token=private')
        wrapped = DownloadError('must not be logged', exc_info=(HTTPError, HTTPError(response), None))
        diagnostic = failure_metadata(wrapped)
        self.assertEqual(diagnostic['http_status'], 403)
        self.assertEqual(diagnostic['error_type'], 'HTTPError')
        self.assertNotIn('private', str(diagnostic))
        self.assertNotIn('https://', str(diagnostic))
        self.assertNotIn('must not be logged', str(diagnostic))

    def test_diagnostics_keep_code_location_without_exception_text(self):
        try:
            raise KeyError('https://cdn.example/?token=private')
        except KeyError as error:
            diagnostic = failure_metadata(error)
        self.assertEqual(diagnostic['error_type'], 'KeyError')
        self.assertEqual(diagnostic['function'], 'test_diagnostics_keep_code_location_without_exception_text')
        self.assertGreater(diagnostic['line'], 0)
        self.assertNotIn('private', str(diagnostic))

    def test_downloaded_mp4_becomes_real_mp3_without_losing_tail(self):
        import av
        import numpy as np

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.media'
            with av.open(str(source), 'w', format='mp4') as output:
                stream = output.add_stream('aac', rate=48000)
                stream.layout = 'mono'
                # Not an encoder-frame multiple: check that delayed samples flush.
                frame = av.AudioFrame.from_ndarray(np.zeros((1, 49920), dtype=np.float32), format='fltp', layout='mono')
                frame.sample_rate = 48000
                for packet in stream.encode(frame):
                    output.mux(packet)
                for packet in stream.encode(None):
                    output.mux(packet)
            result = prepare_audio(source, directory, 'mp4')
            self.assertEqual(result.suffix, '.mp3')
            with av.open(str(source)) as audio:
                source_duration = sum(frame.samples / frame.sample_rate for frame in audio.decode(audio=0))
            with av.open(str(result)) as audio:
                self.assertEqual(audio.streams.audio[0].codec_context.codec.canonical_name, 'mp3')
                self.assertEqual(audio.streams.audio[0].sample_rate, 44100)
                output_duration = sum(frame.samples / frame.sample_rate for frame in audio.decode(audio=0))
                self.assertAlmostEqual(output_duration, source_duration, delta=0.05)

    def test_mp3_conversion_reports_missing_encoder(self):
        import av

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(av.codec, 'Codec', side_effect=ValueError('unavailable')), self.assertRaisesRegex(MediaImportError, 'MP3 编码器'):
                prepare_audio(Path(directory) / 'source.media', directory, 'mp4')


class DownloadFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source.media'
        self.partial = Path(str(self.source) + '.part')
        self.info = {'url': 'https://primary.example/audio.m4s', 'ext': 'm4a',
                     'sona_backup_urls': ['https://backup.example/audio.m4s'],
                     'http_headers': {'Referer': VIDEO}}
        self.downloader = SimpleNamespace(params={})

    def test_candidates_prefer_standard_https_and_remove_duplicates_and_private_urls(self):
        edge = 'https://edge.example:8082/audio.m4s?signature=keep'
        normal = 'https://backup.example/audio.m4s?signature=keep'
        self.assertEqual(media_candidates({'url': edge, 'sona_backup_urls': [
            edge, 'http://127.0.0.1/audio', normal, normal, 'https://cdn.example:0/audio',
        ]}), [normal, edge])

    def test_failed_node_cannot_mix_partial_bytes_into_backup(self):
        from sona.podcasts.transfer import EmptyMediaResponseError

        calls = []

        def download(filename, info):
            self.assertFalse(self.source.exists())
            self.assertFalse(self.partial.exists())
            self.assertEqual(info['http_headers']['Referer'], VIDEO)
            calls.append(info['url'])
            if len(calls) == 1:
                self.source.write_bytes(b'old-complete')
                self.partial.write_bytes(b'old-partial')
                raise EmptyMediaResponseError()
            Path(filename).write_bytes(b'new-media')
            return True, True

        retry = Mock()
        with patch('sona.podcasts.transfer.MediaHttpFD') as transfer:
            transfer.return_value.download.side_effect = download
            result = download_media(self.downloader, self.info, self.source, Mock(), retry)
        self.assertEqual(calls, [self.info['url'], self.info['sona_backup_urls'][0]])
        self.assertEqual(result.read_bytes(), b'new-media')
        self.assertEqual(retry.call_count, 1)

    def test_wrapped_network_oserror_can_switch_nodes(self):
        from yt_dlp.networking.exceptions import TransportError

        def success(filename, info):
            Path(filename).write_bytes(b'new-media')
            return True, True

        with patch('sona.podcasts.transfer.MediaHttpFD') as transfer:
            # Write the successful payload only when the second node starts.
            first = Mock()
            first.download.side_effect = TransportError(cause=OSError('network'))
            second = Mock()
            second.download.side_effect = success
            transfer.side_effect = [first, second]
            self.assertEqual(download_media(self.downloader, self.info, self.source, Mock()), self.source)
        self.assertEqual(transfer.call_count, 2)

    def test_size_limit_and_local_disk_failures_do_not_switch_nodes(self):
        from sona.podcasts.network import MediaRequestError
        from yt_dlp.utils import DownloadError

        errors = [MediaRequestError('media_response_too_large'),
                  DownloadError('disk', exc_info=(OSError, OSError(28, 'disk full'), None))]
        for error in errors:
            with (self.subTest(error=type(error).__name__),
                  patch('sona.podcasts.transfer.MediaHttpFD') as transfer,
                  self.assertRaises(type(error))):
                transfer.return_value.download.side_effect = error
                download_media(self.downloader, self.info, self.source, Mock())
            self.assertEqual(transfer.call_count, 1)

    def test_all_nodes_failed_preserves_last_error(self):
        from sona.podcasts.transfer import EmptyMediaResponseError

        error = EmptyMediaResponseError()
        with (patch('sona.podcasts.transfer.MediaHttpFD') as transfer,
              self.assertRaises(EmptyMediaResponseError) as raised):
            transfer.return_value.download.side_effect = error
            download_media(self.downloader, self.info, self.source, Mock())
        self.assertIs(raised.exception, error)
        self.assertEqual(transfer.call_count, 2)


if __name__ == '__main__':
    unittest.main()
