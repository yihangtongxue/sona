"""Podcast import regressions for manual execution; no network or inference."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sona.audio_library import AudioLibrary
from sona.database import ModelRepository
from sona.podcasts.repository import ImportRepository
from sona.podcasts.urls import normalize_episode
from sona.transcription.repository import TaskRepository


EPISODE = '0123456789abcdef01234567'
XIAO = f'https://www.xiaoyuzhoufm.com/episode/{EPISODE}'
APPLE = 'https://podcasts.apple.com/cn/podcast/id123456?i=1000123456'


class LinkTests(unittest.TestCase):
    def test_normalizes_tracking_and_preserves_apple_episode(self):
        self.assertEqual(normalize_episode(XIAO + '?utm_source=share')[2], XIAO)
        self.assertEqual(normalize_episode(XIAO.replace('www.', '') + '/#share')[2], XIAO)
        self.assertEqual(normalize_episode('https://podcasts.apple.com/cn/podcast/中文节目/id123456?ls=1&i=1000123456')[2], APPLE)

    def test_rejects_show_pages_and_lookalike_hosts(self):
        for url in ('https://www.xiaoyuzhoufm.com/podcast/' + EPISODE,
                    'https://podcasts.apple.com/cn/podcast/id123456',
                    APPLE + '&i=999', XIAO.replace('.com/', '.com.evil.example/'),
                    XIAO.replace('https://', 'file://'), XIAO.replace('https://', 'https://user:pass@'),
                    'http://127.0.0.1/episode/' + EPISODE):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_episode(url)


class AcquisitionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'sona.sqlite3'
        ModelRepository(self.database, ())
        self.audio = AudioLibrary(self.database, self.root / 'audio')
        self.addCleanup(self.audio.close)
        self.imports = ImportRepository(self.database)
        self.tasks = TaskRepository(self.database)

    def ready_download(self):
        identifier = self.imports.create(XIAO)['id']
        directory = self.root / identifier
        directory.mkdir(exist_ok=True)
        (directory / 'audio.mp3').write_bytes(b'complete audio fixture')
        self.imports.patch(identifier, status='importing', name='单集标题', podcast_title='播客名称',
                           suffix='.mp3', downloaded_bytes=22, total_bytes=22)
        return self.imports.get(identifier), directory

    def test_early_listing_does_not_enqueue_and_duplicate_is_same_job(self):
        created = self.imports.create(XIAO)
        duplicate = self.imports.create(XIAO + '?from=share')
        self.assertEqual(created['id'], duplicate['id'])
        self.assertTrue(duplicate['existing'])
        records = self.audio.list_files()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['is_podcast_import'])
        self.assertEqual(records[0]['transcription_status'], 'waiting_fetch')
        self.assertEqual(self.tasks.pending(), [])

    def test_apple_country_variants_are_deduplicated(self):
        self.assertEqual(self.imports.create(APPLE)['id'],
                         self.imports.create(APPLE.replace('/cn/', '/us/'))['id'])

    def test_adoption_enqueues_once_and_keeps_identity_metadata_and_time(self):
        job, directory = self.ready_download()
        self.audio.adopt_download(job, directory)
        self.audio.adopt_download(job, directory)
        record, = self.audio.list_files()
        self.assertEqual(record['id'], job['id'])
        self.assertEqual(record['imported_at'], job['created_at'])
        self.assertEqual(record['podcast_title'], '播客名称')
        self.assertEqual(record['transcription_status'], 'waiting_model')
        self.assertFalse(record['is_podcast_import'])
        self.assertTrue(record['available'])
        self.assertEqual(len(self.tasks.pending()), 1)
        self.assertEqual(self.imports.get(job['id'])['status'], 'imported')

    def test_cancel_wins_over_late_progress_and_commit(self):
        job, directory = self.ready_download()
        self.imports.cancel(job['id'])
        self.assertFalse(self.imports.patch(job['id'], status='downloading', downloaded_bytes=50))
        self.audio.adopt_download(job, directory)
        self.assertEqual(self.tasks.pending(), [])
        self.assertEqual(self.imports.get(job['id'])['status'], 'cancelling')
        self.imports.settle_cancel(job['id'])
        self.imports.retry(job['id'])
        self.assertEqual(self.imports.get(job['id'])['status'], 'waiting_fetch')

    def test_cancel_after_handoff_does_not_cancel_a_different_stage(self):
        job, directory = self.ready_download()
        self.audio.adopt_download(job, directory)
        with self.assertRaises(ValueError):
            self.imports.cancel(job['id'])
        self.assertEqual(self.tasks.get(job['id'])['status'], 'waiting_model')

    def test_incomplete_download_never_enqueues(self):
        job, directory = self.ready_download()
        (directory / 'audio.mp3').write_bytes(b'short')
        with self.assertRaises(ValueError):
            self.audio.adopt_download(job, directory)
        self.assertEqual(self.tasks.pending(), [])

    def test_database_failure_restores_download_for_retry(self):
        job, directory = self.ready_download()
        with patch.object(self.audio, '_insert', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.audio.adopt_download(job, directory)
        self.assertTrue((directory / 'audio.mp3').exists())
        self.assertEqual(self.tasks.pending(), [])
        self.audio.adopt_download(job, directory)
        self.assertEqual(len(self.tasks.pending()), 1)

    def test_crash_between_rename_and_insert_recovers_one_task(self):
        job, directory = self.ready_download()
        destination = self.root / 'audio' / (job['id'] + '.mp3')
        (directory / 'audio.mp3').replace(destination)
        (self.root / 'audio' / '.pending.json').write_text(json.dumps({
            'id': job['id'], 'name': job['name'], 'suffix': '.mp3', 'size_bytes': 22,
            'imported_at': job['created_at'],
        }))
        reopened = AudioLibrary(self.database, self.root / 'audio')
        self.addCleanup(reopened.close)
        self.imports.recover()
        self.assertEqual(len(self.tasks.pending()), 1)
        self.assertEqual(self.imports.get(job['id'])['status'], 'imported')
        self.assertEqual(len(reopened.list_files()), 1)

    def test_restart_interrupts_download_but_preserves_ready_file(self):
        job, directory = self.ready_download()
        second = self.imports.create(APPLE)['id']
        self.imports.patch(second, status='downloading')
        self.imports.recover()
        self.assertEqual(self.imports.get(second)['status'], 'interrupted')
        self.assertEqual(self.imports.get(job['id'])['status'], 'importing')
        self.audio.adopt_download(job, directory)
        self.assertEqual(len(self.tasks.pending()), 1)

    def test_delete_removes_source_and_allows_new_import(self):
        job, directory = self.ready_download()
        self.audio.adopt_download(job, directory)
        self.audio.delete_file(job['id'])
        self.assertIsNone(self.imports.get(job['id']))
        self.assertEqual(self.audio.list_files(), [])
        self.assertNotEqual(self.imports.create(XIAO)['id'], job['id'])

    def test_local_import_lock_defers_adoption(self):
        job, directory = self.ready_download()
        local = self.audio.begin_import('local.wav', 3)
        with self.assertRaises(RuntimeError):
            self.audio.adopt_download(job, directory)
        self.assertTrue((directory / 'audio.mp3').exists())
        self.audio.abort_import(local)
        self.audio.adopt_download(job, directory)
        self.assertEqual(len(self.tasks.pending()), 1)


class ExtractorTests(unittest.TestCase):
    def test_xiaoyuzhou_metadata_and_signed_audio(self):
        from sona.podcasts.extractors import XiaoyuzhouIE
        extractor = XiaoyuzhouIE()
        page = ('<meta property="og:audio" content="https://cdn.example/audio.mp3?a=1&amp;b=2">'
                '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'episode': {
                    'eid': EPISODE, 'title': '单集', 'podcast': {'title': '节目'}, 'duration': 60,
                }}}}) + '</script>')
        with patch.object(extractor, '_download_webpage', return_value=page):
            result = extractor._real_extract(XIAO)
        self.assertEqual(result['url'], 'https://cdn.example/audio.mp3?a=1&b=2')
        self.assertEqual(result['series'], '节目')
        self.assertEqual(result['duration'], 60)

    def test_private_episode_does_not_download_even_with_meta_audio(self):
        from sona.podcasts.extractors import XiaoyuzhouIE
        from yt_dlp.utils import ExtractorError
        extractor = XiaoyuzhouIE()
        page = ('<meta property="og:audio" content="https://cdn.example/audio.mp3">'
                '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'episode': {
                    'eid': EPISODE, 'isPrivateMedia': True,
                }}}}) + '</script>')
        with patch.object(extractor, '_download_webpage', return_value=page), self.assertRaises(ExtractorError):
            extractor._real_extract(XIAO)

    def test_apple_country_passed_to_upstream_without_trailing_slash(self):
        from sona.podcasts.extractors import SonaApplePodcastsIE
        extractor = SonaApplePodcastsIE()
        with (patch.object(extractor, '_download_webpage', return_value='<html></html>'),
              patch.object(extractor, '_extract_podcast_from_webpage', return_value=None),
              patch.object(extractor, '_extract_podcast_from_api', return_value={'id': '1000123456'}) as fallback):
            extractor._real_extract(APPLE)
        self.assertEqual(fallback.call_args.args[2], 'cn')

    def test_redirect_guard_rejects_local_and_non_http_targets(self):
        from urllib.request import Request
        from sona.podcasts.network import PublicRequestGuard
        from yt_dlp.networking.exceptions import RequestError
        guard = PublicRequestGuard()
        for url in ('http://127.0.0.1/audio.mp3', 'http://[::1]/audio.mp3',
                    'https://localhost/audio.mp3', 'file:///tmp/audio.mp3', 'ftp://cdn.example/audio.mp3'):
            with self.subTest(url=url), self.assertRaises(RequestError):
                guard.http_request(Request(url))

    def test_network_read_enforces_limit_without_content_length(self):
        import io
        from types import SimpleNamespace
        from sona.podcasts.network import PodcastYoutubeDL
        from yt_dlp import YoutubeDL
        from yt_dlp.networking.exceptions import RequestError
        stream = io.BytesIO(b'123456789')
        response = SimpleNamespace(headers={}, read=stream.read, close=stream.close)
        downloader = object.__new__(PodcastYoutubeDL)
        with (patch.object(YoutubeDL, 'urlopen', return_value=response),
              patch('sona.podcasts.network.MAX_PAGE_BYTES', 8)):
            limited = downloader.urlopen('https://cdn.example/page')
            self.assertEqual(limited.read(4), b'1234')
            with self.assertRaises(RequestError):
                limited.read()
        self.assertTrue(stream.closed)

    def test_dns_results_cannot_point_to_private_servers(self):
        import socket
        from sona.podcasts.network import public_media_url
        from yt_dlp.networking.exceptions import RequestError
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 443))]
        with patch('socket.getaddrinfo', return_value=answers), self.assertRaises(RequestError):
            public_media_url('https://cdn.example/audio.mp3', resolve=True)

    def test_cross_origin_redirect_does_not_forward_authorization(self):
        from urllib.request import Request
        from sona.podcasts.network import PublicRedirectHandler
        request = Request('https://api.example/episode', headers={'Authorization': 'Bearer fixture'})
        redirected = PublicRedirectHandler().redirect_request(
            request, None, 302, '', {}, 'https://cdn.example/audio.mp3')
        self.assertIsNone(redirected.get_header('Authorization'))


if __name__ == '__main__':
    unittest.main()
