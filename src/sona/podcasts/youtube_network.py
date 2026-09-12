"""Honor yt-dlp subtitle impersonation without bypassing redirect guards."""

import functools
from urllib.parse import urljoin, urlsplit

from yt_dlp.networking._curlcffi import CurlCFFIRH
from yt_dlp.networking.exceptions import HTTPError

from .network import PodcastYoutubeDL, PublicUrllibRH, public_media_url


class PublicCurlCFFIRH(CurlCFFIRH):
    def _create_instance(self, *args, **kwargs):
        session = super()._create_instance(*args, **kwargs)
        original = session.request

        def request(*args, **kwargs):
            # Curl must not follow a redirect before we have validated its URL.
            kwargs['allow_redirects'] = False
            return original(*args, **kwargs)

        session.request = request
        return session

    def _send(self, request):
        request = request.copy()
        for attempt in range(6):
            public_media_url(request.url, resolve=True)
            try:
                return super()._send(request)
            except HTTPError as error:
                response = error.response
                location = response.headers.get('Location')
                if response.status not in (301, 302, 303, 307, 308) or not location:
                    raise
                if attempt == 5:
                    response.close()
                    raise HTTPError(response, redirect_loop=True) from None
                destination = urljoin(request.url, location)
                response.close()
                public_media_url(destination)
                before, after = urlsplit(request.url), urlsplit(destination)
                if (before.scheme, before.hostname, before.port) != (after.scheme, after.hostname, after.port):
                    for header in ('Authorization', 'Proxy-Authorization', 'Cookie'):
                        request.headers.pop(header, None)
                if ((response.status in (301, 302) and request.method == 'POST')
                        or (response.status == 303 and request.method != 'HEAD')):
                    request.method = 'GET'
                    request.data = None
                    for header in ('Content-Type', 'Content-Length'):
                        request.headers.pop(header, None)
                request.url = destination


class YoutubeYoutubeDL(PodcastYoutubeDL):
    @functools.cached_property
    def _request_director(self):
        # Plain metadata/media requests still prefer the existing urllib path.
        # curl_cffi is available for the upstream-requested impersonation only.
        return self.build_request_director([PublicUrllibRH, PublicCurlCFFIRH], preferences={
            lambda handler, request: -100 if isinstance(handler, PublicCurlCFFIRH) else 0,
        })
