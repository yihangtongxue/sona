"""YouTube regressions for manual execution; no network, app or speech model."""

import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import TransportError

from sona.audio_library import AudioLibrary
from sona.database import ModelRepository, SCHEMA, SCHEMA_VERSION
from sona.manuscripts import ManuscriptRepository
from sona.podcasts.captions import SubtitleError, load_payload, parse_subtitles, save_payload
from sona.podcasts.repository import ImportRepository
from sona.podcasts.service import PodcastService
from sona.podcasts.urls import normalize_episode
from sona.podcasts.youtube import acquire, extract_video, fetch_subtitles, subtitle_candidates
from sona.transcription.repository import TaskRepository


VIDEO_ID = 'BaW_jenozKc'
URL = f'https://www.youtube.com/watch?v={VIDEO_ID}'


def track(language='en', automatic=False, translated=False):
    return {'url': f'https://www.youtube.com/api/timedtext?lang={language}'
                   + ('&kind=asr' if automatic else '') + ('&tlang=zh' if translated else ''), 'ext': 'json3'}


def subtitle_result():
    return {'text': 'Hello world', 'segments': [{'start': 0.0, 'end': 2.0, 'text': 'Hello world'}],
            'language': 'en', 'duration': 2.0, 'source_kind': 'manual_subtitles'}


def payload(text='Hello world'):
    return json.dumps({'events': [{'tStartMs': 0, 'dDurationMs': 2000, 'segs': [{'utf8': text}]}]}).encode()


class YoutubeLinkTests(unittest.TestCase):
    def test_single_video_variants_share_identity(self):
        for value in (URL, URL + '&list=PL123&t=30', f'https://youtu.be/{VIDEO_ID}?si=share',
                      f'https://m.youtube.com/watch?v={VIDEO_ID}', f'https://www.youtube.com/shorts/{VIDEO_ID}',
                      f'https://www.youtube.com/embed/{VIDEO_ID}', f'视频分享 {URL}。'):
            self.assertEqual(normalize_episode(value), ('youtube', VIDEO_ID, URL))

    def test_invalid_and_nonvideo_links_are_rejected(self):
        for value in (URL + '&v=12345678901', URL.replace(VIDEO_ID, 'short'),
                      URL.replace('youtube.com', 'youtube.com.evil.example'),
                      URL.replace('https://', 'https://user:pass@'), URL.replace('.com/', '.com:8080/'),
                      'https://www.youtube.com/playlist?list=PL123', 'https://www.youtube.com/@channel',
                      f'https://www.youtube.com/live/{VIDEO_ID}', f'视频 {URL} https://youtu.be/{VIDEO_ID}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_episode(value)


class SubtitleChoiceTests(unittest.TestCase):
    def test_original_manual_precedes_asr_and_never_uses_auto_translation(self):
        info = {'language': 'en', 'subtitles': {'en': [track()], 'zh': [track('zh')]},
                'automatic_captions': {'en-orig': [track(automatic=True)], 'zh': [track(automatic=True, translated=True)]}}
        selected = subtitle_candidates(info)
        self.assertEqual([item['source_kind'] for item in selected], ['manual_subtitles', 'automatic_subtitles'])
        self.assertTrue(all(item['language'] == 'en' for item in selected))
        chinese = subtitle_candidates(info, 'zh')
        self.assertEqual(len(chinese), 1)
        self.assertEqual(chinese[0]['source_kind'], 'manual_subtitles')

    def test_original_asr_marker_identifies_language_without_metadata(self):
        selected = subtitle_candidates({'automatic_captions': {'ja-orig': [track('ja', automatic=True)]}})
        self.assertEqual(selected[0]['language'], 'ja')

    def test_ambiguous_language_and_chat_are_not_randomly_imported(self):
        self.assertEqual(subtitle_candidates({'subtitles': {'en': [track()], 'ja': [track('ja')],
                                                           'live_chat': [track()]}}), [])

    def test_regional_language_matches_and_private_caption_url_is_rejected(self):
        info = {'subtitles': {'zh-Hans': [track('zh-Hans')], 'zh': [{'url': 'http://127.0.0.1/captions', 'ext': 'json3'}]}}
        self.assertEqual([item['language'] for item in subtitle_candidates(info, 'zh')], ['zh-Hans'])


class SubtitleParsingTests(unittest.TestCase):
    def test_json3_rollup_dedup_keeps_repeated_speech_at_later_times(self):
        data = {'events': [{'tStartMs': start, 'dDurationMs': length, 'segs': [{'utf8': text}]}
                           for start, length, text in [(0, 2000, 'Hello'), (1000, 2000, 'Hello world'),
                                                       (4000, 2000, 'Hello world'), (6000, 2000, 'yes yes')]]}
        segments = parse_subtitles(json.dumps(data).encode(), 'json3', automatic=True)
        self.assertEqual([cue['text'] for cue in segments], ['Hello world', 'Hello world', 'yes yes'])
        self.assertEqual((segments[0]['start'], segments[0]['end']), (0, 3))

    def test_vtt_uses_upstream_parser_and_removes_display_markup(self):
        data = b'WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<c>Hello</c> &amp; world\n\n'
        self.assertEqual(parse_subtitles(data, 'vtt'), [{'start': 1, 'end': 3, 'text': 'Hello & world'}])

    def test_invalid_empty_and_negative_time_subtitles_are_not_success(self):
        invalid = [b'<html>Not captions</html>', b'{"events": []}',
                   b'{"events":[{"tStartMs":-1,"dDurationMs":1000,"segs":[{"utf8":"text"}]}]}']
        for data in invalid:
            with self.assertRaises(SubtitleError):
                parse_subtitles(data, 'json3')

    def test_json3_preserves_literal_angle_brackets(self):
        self.assertEqual(parse_subtitles(payload('a < b > c'), 'json3')[0]['text'], 'a < b > c')


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.info = {'language': 'en', 'duration': 2, 'subtitles': {'en': [track()]}}
        self.emit = Mock()
        self.downloader = Mock()

    def response(self):
        return Response(io.BytesIO(payload()), 'https://www.youtube.com/api/timedtext', {'Content-Length': str(len(payload()))})

    def test_subtitle_success_never_downloads_or_converts_audio(self):
        self.downloader.urlopen.return_value = self.response()
        with patch('sona.podcasts.youtube.download_audio') as audio, patch('sona.podcasts.youtube.prepare_audio') as conversion:
            acquire(self.downloader, self.info, {}, self.directory, self.emit, Mock())
        audio.assert_not_called()
        conversion.assert_not_called()
        self.assertEqual(load_payload(self.directory), subtitle_result())
        self.assertEqual(self.emit.call_args.args[0]['result_kind'], 'subtitles')

    def test_caption_network_failure_is_distinguished_from_missing(self):
        self.downloader.urlopen.side_effect = TransportError('network')
        self.assertEqual(fetch_subtitles(self.downloader, self.info, self.directory, 'original', self.emit), 'failed')
        self.assertEqual(fetch_subtitles(self.downloader, {}, self.directory, 'original', self.emit), 'missing')

    def test_failed_captions_fall_back_to_audio_and_complete(self):
        source, target = self.directory / 'source.media', self.directory / 'audio.mp3'
        source.write_bytes(b'fixture')
        target.write_bytes(b'converted')
        with (patch('sona.podcasts.youtube.fetch_subtitles', return_value='failed'),
              patch('sona.podcasts.youtube.download_audio', return_value=(source, 'm4a')) as audio,
              patch('sona.podcasts.youtube.prepare_audio', return_value=target)):
            acquire(self.downloader, self.info, {}, self.directory, self.emit, Mock())
        audio.assert_called_once()
        self.assertIn('字幕获取失败', self.emit.call_args_list[0].args[0]['detail'])
        self.assertEqual(self.emit.call_args.args[0]['suffix'], '.mp3')
        self.assertFalse(source.exists())

    def test_transcribe_option_skips_caption_requests(self):
        with (patch('sona.podcasts.youtube.fetch_subtitles') as captions,
              patch('sona.podcasts.youtube.download_audio', side_effect=ValueError('stop before network')),
              self.assertRaises(ValueError)):
            acquire(self.downloader, self.info, {'strategy': 'transcribe'}, self.directory, self.emit, Mock())
        captions.assert_not_called()

    def test_metadata_failure_never_becomes_missing_subtitles(self):
        with patch('sona.podcasts.youtube.YoutubeIE.extract', side_effect=TransportError('metadata')):
            with self.assertRaises(TransportError):
                extract_video(self.downloader, URL)

    def test_no_media_formats_can_still_have_subtitles(self):
        info = {**self.info, 'id': VIDEO_ID, 'formats': []}
        with patch('sona.podcasts.youtube.YoutubeIE.extract', return_value=info):
            self.assertTrue(extract_video(self.downloader, URL)['subtitles'])

    def test_original_audio_language_is_read_before_subtitle_selection(self):
        info = {'id': VIDEO_ID, 'duration': 2, 'formats': [
            {'acodec': 'opus', 'language': 'en', 'language_preference': 10},
            {'acodec': 'mp4a', 'language': 'zh', 'language_preference': 5}],
            'subtitles': {'en': [track()], 'zh': [track('zh')]}}
        with patch('sona.podcasts.youtube.YoutubeIE.extract', return_value=info):
            selected = subtitle_candidates(extract_video(self.downloader, URL))
        self.assertEqual([item['language'] for item in selected], ['en'])

    def test_subtitle_request_honors_upstream_impersonation(self):
        self.info['subtitles']['en'][0]['impersonate'] = True
        self.downloader._parse_impersonate_targets.return_value = ('fixture-target', [])
        self.downloader.urlopen.return_value = self.response()
        fetch_subtitles(self.downloader, self.info, self.directory, 'original', self.emit)
        request = self.downloader.urlopen.call_args.args[0]
        self.assertEqual(request.extensions['impersonate'], 'fixture-target')


class SubtitleStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'sona.sqlite3'
        ModelRepository(self.database, ())
        self.imports = ImportRepository(self.database)
        self.tasks = TaskRepository(self.database)
        self.library = AudioLibrary(self.database, self.root / 'audio')
        self.addCleanup(self.library.close)
        self.identifier = self.imports.create(URL)['id']
        self.imports.patch(self.identifier, status='importing', stage='subtitles', name='Video')

    def test_text_only_result_is_visible_without_model_or_audio(self):
        self.assertTrue(self.imports.save_subtitles(self.identifier, subtitle_result()))
        record, = self.library.list_files()
        self.assertEqual(record['transcription_status'], 'completed')
        self.assertFalse(record['available'])
        self.assertEqual(record['source_kind'], 'manual_subtitles')
        self.assertEqual(self.tasks.result(self.identifier)['text'], 'Hello world')
        self.assertEqual(self.tasks.pending(), [])
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM audio_files').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM transcription_tasks').fetchone()[0], 0)

    def test_cancellation_wins_over_late_subtitle_commit(self):
        self.imports.cancel(self.identifier)
        self.assertFalse(self.imports.save_subtitles(self.identifier, subtitle_result()))
        self.assertEqual(self.library.list_files()[0]['transcription_status'], 'cancelling')

    def test_restart_commits_ready_subtitles_without_audio_adoption(self):
        service = PodcastService.__new__(PodcastService)
        service._mutation, service._stop = threading.RLock(), threading.Event()
        service.repository, service.library, service.root = self.imports, Mock(), self.root / 'downloads'
        directory = service._directory(self.identifier)
        directory.mkdir(parents=True)
        save_payload(directory, subtitle_result())
        self.imports.recover()
        service._commit(self.imports.get(self.identifier))
        service.library.adopt_download.assert_not_called()
        self.assertEqual(self.tasks.result(self.identifier)['text'], 'Hello world')
        self.assertFalse(directory.exists())
        self.imports.recover()
        self.assertEqual(self.imports.get(self.identifier)['status'], 'imported')

    def test_deletion_cascades_subtitles_but_preserves_created_manuscript(self):
        self.imports.save_subtitles(self.identifier, subtitle_result())
        manuscripts = ManuscriptRepository(self.database)
        manuscript_id = manuscripts.create(self.identifier, {})
        self.assertTrue(self.imports.delete_subtitles(self.identifier))
        self.assertEqual(self.library.list_files(), [])
        with self.assertRaises(ValueError):
            self.tasks.result(self.identifier)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT source_text FROM manuscripts WHERE id=?', (manuscript_id,)).fetchone()[0], 'Hello world')
        self.assertFalse(self.imports.create(URL)['existing'])

    def test_retry_keeps_language_and_strategy(self):
        identifier = self.imports.create(URL.replace(VIDEO_ID, 'abcdefghijk'), 'transcribe', 'ja')['id']
        self.imports.patch(identifier, status='failed')
        self.imports.retry(identifier)
        result = self.imports.get(identifier)
        self.assertEqual((result['strategy'], result['subtitle_language']), ('transcribe', 'ja'))


class MigrationTests(unittest.TestCase):
    def test_v8_migration_preserves_aliases_audio_results_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'sona.sqlite3'
            with sqlite3.connect(database) as db:
                for statement in SCHEMA:
                    if 'CREATE TABLE IF NOT EXISTS subtitle_results' in statement:
                        continue
                    statement = statement.replace("'bilibili','youtube'", "'bilibili'")
                    statement = '\n'.join(line for line in statement.splitlines()
                                          if 'strategy TEXT' not in line and 'subtitle_language TEXT' not in line)
                    db.execute(statement)
                db.execute('PRAGMA user_version=8')
                db.execute("INSERT INTO podcast_imports(id,platform,episode_id,source_url,status) VALUES ('old','bilibili','BV1xx411c7mD:p1','https://www.bilibili.com/video/BV1xx411c7mD?p=1','imported')")
                db.execute("INSERT INTO media_import_aliases VALUES ('bilibili','short:Alias','old')")
                db.execute("INSERT INTO audio_files(id,name,suffix,size_bytes) VALUES ('old','保留标题','.mp3',42)")
                db.execute("UPDATE transcription_tasks SET status='completed' WHERE audio_id='old'")
                db.execute("INSERT INTO transcription_results(audio_id,text,segments_json,language,duration,model_id,engine,model_revision,device) VALUES ('old','保留结果','[]','zh',2,'model','engine','rev','CPU')")
            ModelRepository(database, ())
            ModelRepository(database, ())
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(db.execute('SELECT import_id FROM media_import_aliases').fetchone()[0], 'old')
                self.assertEqual(db.execute('SELECT text FROM transcription_results').fetchone()[0], '保留结果')
                self.assertEqual(db.execute('SELECT strategy FROM podcast_imports').fetchone()[0], 'subtitle_first')
            self.assertFalse(ImportRepository(database).create(URL)['existing'])


if __name__ == '__main__':
    unittest.main()
