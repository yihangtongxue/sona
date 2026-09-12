"""Offline HTTP transfer regressions; execute manually with the project tests."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yt_dlp import YoutubeDL
from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import NO_DEFAULT

from sona.podcasts.errors import failure_metadata
from sona.podcasts.transfer import EmptyMediaResponseError, MediaHttpFD


MEDIA_URL = 'https://cdn.example/audio.m4a?token=private'


class MediaTransferTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = str(Path(self.directory.name) / 'source.media')
        self.downloader = YoutubeDL({
            'quiet': True, 'no_warnings': True, 'noprogress': True,
            'logger': Mock(), 'cachedir': False, 'js_runtimes': {},
            'retries': 2, 'continuedl': False,
        }, auto_init=False)
        self.addCleanup(self.downloader.close)
        self.transfer = MediaHttpFD(self.downloader, self.downloader.params)
        self.info = {'id': 'offline', 'title': 'Offline media', 'url': MEDIA_URL,
                     'ext': 'm4a', 'http_headers': {}}

    def test_http_retry_exhaustion_preserves_original_error_and_status(self):
        error = HTTPError(SimpleNamespace(status=503, reason='Unavailable ' + MEDIA_URL))
        with (patch.object(self.downloader, 'urlopen', side_effect=error) as request,
              self.assertRaises(HTTPError) as raised):
            self.transfer.download(self.target, self.info)
        self.assertIs(raised.exception, error)
        self.assertEqual(request.call_count, 3)
        diagnostic = failure_metadata(raised.exception)
        self.assertEqual(diagnostic['http_status'], 503)
        self.assertNotIn('private', str(diagnostic))

    def test_transport_retry_exhaustion_preserves_nested_timeout(self):
        timeout = TimeoutError('Timeout requesting ' + MEDIA_URL)
        error = TransportError(cause=timeout)
        with (patch.object(self.downloader, 'urlopen', side_effect=error) as request,
              self.assertRaises(TransportError) as raised):
            self.transfer.download(self.target, self.info)
        self.assertIs(raised.exception, error)
        self.assertIs(raised.exception.cause, timeout)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(failure_metadata(raised.exception)['error_type'], 'TimeoutError')

    def test_empty_http_body_has_fixed_error_instead_of_generic_download_error(self):
        response = SimpleNamespace(headers={'Content-Length': '0'},
                                   read=Mock(return_value=b''), close=Mock())
        with (patch.object(self.downloader, 'urlopen', return_value=response),
              self.assertRaises(EmptyMediaResponseError) as raised):
            self.transfer.download(self.target, self.info)
        self.assertEqual(raised.exception.code, 'media_empty_response')
        self.assertEqual(str(raised.exception), '音频服务器返回了空内容，请重新获取。')
        self.assertNotIn('private', str(raised.exception))

    def test_zero_retries_preserves_first_failure(self):
        error = TransportError('Connection refused')
        self.transfer.params['retries'] = 0
        with (patch.object(self.downloader, 'urlopen', side_effect=error) as request,
              self.assertRaises(TransportError) as raised):
            self.transfer.download(self.target, self.info)
        self.assertIs(raised.exception, error)
        request.assert_called_once()

    def test_nonfatal_retry_reporting_retains_upstream_behavior(self):
        error = TransportError('Connection refused')
        with patch.object(HttpFD, 'report_retry') as upstream:
            self.transfer.report_retry(error, 3, 2, fatal=False)
        upstream.assert_called_once_with(error, 3, 2, frag_index=NO_DEFAULT, fatal=False)

    def test_unrelated_errors_use_parent_reporting(self):
        with patch.object(self.downloader, 'report_error') as upstream:
            self.transfer.report_error('unable to open for writing', tb=False)
        upstream.assert_called_once_with('unable to open for writing', tb=False)


if __name__ == '__main__':
    unittest.main()
