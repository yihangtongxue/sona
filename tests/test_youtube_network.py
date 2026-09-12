"""Offline transport checks; run manually after synchronizing dependencies."""

import io
import unittest
from unittest.mock import Mock, patch

from yt_dlp.networking import Request, Response
from yt_dlp.networking._curlcffi import CurlCFFIRH
from yt_dlp.networking.exceptions import HTTPError

from sona.podcasts.network import MediaRequestError, public_media_url
from sona.podcasts.youtube_network import PublicCurlCFFIRH


class YoutubeNetworkTests(unittest.TestCase):
    def setUp(self):
        self.handler = PublicCurlCFFIRH(logger=Mock())
        self.addCleanup(self.handler.close)
        # DNS is intentionally never queried by these offline fixtures.
        self.guard = patch('sona.podcasts.youtube_network.public_media_url',
                           side_effect=lambda url, **kwargs: public_media_url(url))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def redirect(self, location):
        return Response(io.BytesIO(), 'https://www.youtube.com/captions', {'Location': location}, status=302)

    def test_curl_cannot_automatically_follow_unchecked_redirects(self):
        session = Mock()
        original = session.request
        with patch.object(CurlCFFIRH, '_create_instance', return_value=session):
            wrapped = self.handler._create_instance()
        wrapped.request(url='https://www.youtube.com/captions', allow_redirects=True)
        self.assertFalse(original.call_args.kwargs['allow_redirects'])

    def test_cross_origin_redirect_drops_credentials_and_revalidates(self):
        seen = []
        redirect = self.redirect('https://captions.example/subtitles')
        result = Response(io.BytesIO(b'WEBVTT'), 'https://captions.example/subtitles', {})
        self.addCleanup(result.close)

        def send(request):
            seen.append((request.url, dict(request.headers)))
            if len(seen) == 1:
                raise HTTPError(redirect)
            return result

        original = Request('https://www.youtube.com/captions', headers={
            'Cookie': 'fixture', 'Authorization': 'fixture', 'Referer': 'https://www.youtube.com/'})
        with patch.object(CurlCFFIRH, '_send', side_effect=send):
            self.assertIs(self.handler._send(original), result)
        self.assertEqual(len(seen), 2)
        self.assertNotIn('Cookie', seen[1][1])
        self.assertNotIn('Authorization', seen[1][1])
        self.assertEqual(seen[1][1]['Referer'], 'https://www.youtube.com/')
        self.assertEqual(original.headers['Cookie'], 'fixture')
        self.assertTrue(redirect.closed)

    def test_private_redirect_never_reaches_transport(self):
        response = self.redirect('http://127.0.0.1/private')
        with (patch.object(CurlCFFIRH, '_send', side_effect=HTTPError(response)) as send,
              self.assertRaises(MediaRequestError)):
            self.handler._send(Request('https://www.youtube.com/captions'))
        send.assert_called_once()
        self.assertTrue(response.closed)

    def test_redirect_loop_is_bounded(self):
        responses = []

        def send(request):
            response = self.redirect('/captions')
            responses.append(response)
            raise HTTPError(response)

        with patch.object(CurlCFFIRH, '_send', side_effect=send), self.assertRaises(HTTPError):
            self.handler._send(Request('https://www.youtube.com/captions'))
        self.assertEqual(len(responses), 6)
        self.assertTrue(all(response.closed for response in responses))


if __name__ == '__main__':
    unittest.main()
