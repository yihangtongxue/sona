"""A restricted yt-dlp HTTP transport, including redirects and bounded reads.

Uses the pinned yt-dlp urllib handler so optional installed networking packages
cannot bypass the guard. TLS and the user's system proxy remain enabled.
"""

import functools
import ipaddress
import re
import socket
from urllib.parse import urlsplit
from urllib.request import BaseHandler, OpenerDirector

from yt_dlp import YoutubeDL
from yt_dlp.networking._urllib import RedirectHandler, UrllibRH
from yt_dlp.networking.exceptions import RequestError


MAX_AUDIO_BYTES = 2 * 1024**3
MAX_PAGE_BYTES = 8 * 1024**2
PROXY_DNS_RANGE = ipaddress.ip_network('198.18.0.0/15')


def public_media_url(url, *, resolve=False):
    try:
        parsed = urlsplit(url)
        if (len(url) > 16384 or parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or re.search(r'[\x00-\x20\\]', url)
                or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)):
            raise ValueError()
        host = parsed.hostname.lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise ValueError()
        if resolve:
            addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                           type=socket.SOCK_STREAM)
            if not addresses:
                raise ValueError()
            for address in addresses:
                ip = ipaddress.ip_address(address[4][0])
                # Match Sona's update transport: allow fake DNS returned by a TUN
                # proxy, but never allow that range as a literal input address.
                if not ip.is_global and not (literal is None and ip in PROXY_DNS_RANGE):
                    raise ValueError()
    except (ValueError, TypeError, OSError):
        raise RequestError('音频地址无效或无法解析到公开服务器，请检查网络或代理设置。') from None
    return url


class PublicRequestGuard(BaseHandler):
    handler_order = 100

    def http_request(self, request):
        public_media_url(request.full_url, resolve=True)
        return request

    https_request = http_request
    ftp_request = data_request = file_request = http_request


class PublicRedirectHandler(RedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, request, response, code, message, headers, newurl):
        public_media_url(newurl)
        redirected = super().redirect_request(request, response, code, message, headers, newurl)
        before, after = urlsplit(request.full_url), urlsplit(newurl)
        if (before.scheme, before.hostname, before.port) != (after.scheme, after.hostname, after.port):
            for header in ('Authorization', 'Proxy-Authorization', 'Cookie'):
                redirected.remove_header(header)
        return redirected


class PublicUrllibRH(UrllibRH):
    _SUPPORTED_URL_SCHEMES = ('http', 'https')

    def _create_instance(self, *args, **kwargs):
        original = super()._create_instance(*args, **kwargs)
        opener = OpenerDirector()
        for handler in original.handlers:
            opener.add_handler(PublicRedirectHandler() if isinstance(handler, RedirectHandler) else handler)
        opener.addheaders = []
        opener.add_handler(PublicRequestGuard())
        return opener


class PodcastYoutubeDL(YoutubeDL):
    downloading_audio = False

    @functools.cached_property
    def _request_director(self):
        return self.build_request_director([PublicUrllibRH])

    def urlopen(self, request):
        public_media_url(request if isinstance(request, str) else request.url)
        response = super().urlopen(request)
        limit = MAX_AUDIO_BYTES if self.downloading_audio else MAX_PAGE_BYTES
        try:
            if int(response.headers.get('Content-Length', '0')) > limit:
                raise RequestError('响应超过大小限制（网页 8 MB，音频 2 GB）。')
        except ValueError:
            pass
        except Exception:
            response.close()
            raise
        original_read = response.read
        consumed = 0

        def read(amount=None):
            nonlocal consumed
            # In particular, metadata read() must never allocate an unbounded page.
            requested = limit - consumed + 1
            if amount is not None and amount >= 0:
                requested = min(amount, requested)
            data = original_read(requested)
            consumed += len(data)
            if consumed > limit:
                response.close()
                raise RequestError('响应超过大小限制（网页 8 MB，音频 2 GB）。')
            return data

        response.read = read
        return response
