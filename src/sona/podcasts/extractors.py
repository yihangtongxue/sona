"""Small Xiaoyuzhou adapter; Apple Podcasts uses yt-dlp's maintained extractor."""

import json
from html import unescape
from html.parser import HTMLParser

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.applepodcasts import ApplePodcastsIE
from yt_dlp.utils import ExtractorError


class SonaApplePodcastsIE(ApplePodcastsIE):
    # The pinned upstream regex includes '/' in country, which is interpolated
    # into its API URL. Keep the same extraction, with a clean country capture.
    _VALID_URL = r'https://podcasts\.apple\.com/(?:(?P<country>[a-z]{2})/)?podcast/id\d+\?i=(?P<id>\d+)'


class EpisodePage(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.documents = []
        self.audio = ''
        self._script = None
        self._parts = []
        self.feed(page)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'meta':
            self.meta[attrs.get('property') or attrs.get('name')] = attrs.get('content') or ''
        elif tag == 'script' and (attrs.get('id') == '__NEXT_DATA__'
                                  or attrs.get('type') == 'application/ld+json'):
            self._script = attrs.get('id') or 'jsonld'
            self._parts = []
        elif tag in ('audio', 'source') and attrs.get('src') and not self.audio:
            self.audio = attrs['src']

    def handle_data(self, data):
        if self._script:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._script:
            try:
                self.documents.append((self._script, json.loads(''.join(self._parts))))
            except (ValueError, RecursionError):
                pass
            self._script = None
            self._parts = []


def objects(value):
    # Iterative traversal avoids deep JSON exhausting the Python call stack.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            yield item
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def nested(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


class XiaoyuzhouIE(InfoExtractor):
    IE_NAME = 'sona:xiaoyuzhou'
    _VALID_URL = r'https://www\.xiaoyuzhoufm\.com/episode/(?P<id>[a-f0-9]{24})'

    def _real_extract(self, url):
        identifier = self._match_id(url)
        page = EpisodePage(self._download_webpage(url, identifier))
        episode = {}
        linked = {}
        for kind, document in page.documents:
            if kind == '__NEXT_DATA__':
                candidate = nested(document, 'props', 'pageProps', 'episode')
                if isinstance(candidate, dict) and candidate.get('eid', identifier) == identifier:
                    episode = candidate
                for candidate in objects(document):
                    if candidate.get('eid') == identifier and ('media' in candidate or 'title' in candidate):
                        episode = candidate
                        break
            else:
                candidates = [item for item in objects(document)
                              if item.get('@type') == 'PodcastEpisode']
                matches = [item for item in candidates if identifier in str(item.get('url', ''))]
                if matches:
                    linked = matches[0]
                elif len(candidates) == 1 and not candidates[0].get('url'):
                    linked = candidates[0]
        if (episode.get('isPrivateMedia') or episode.get('mode') == 'PRIVATE'
                or nested(episode, 'media', 'isPrivateMedia')):
            raise ExtractorError('该单集需要访问权限，暂不支持获取。', expected=True)
        audio = (page.meta.get('og:audio') or page.meta.get('og:audio:url')
                 or nested(episode, 'media', 'source', 'url')
                 or nested(episode, 'enclosure', 'url')
                 or nested(linked, 'associatedMedia', 'contentUrl') or page.audio)
        if not isinstance(audio, str) or not audio:
            raise ExtractorError('公开页面没有可下载的音频地址，单集可能已下架或页面结构已变化。', expected=True)
        return {
            'id': identifier, 'url': unescape(audio), 'vcodec': 'none',
            'title': episode.get('title') or linked.get('name') or page.meta.get('og:title') or '小宇宙单集',
            'series': nested(episode, 'podcast', 'title') or nested(linked, 'partOfSeries', 'name') or '',
            'thumbnail': page.meta.get('og:image') or nested(episode, 'image', 'picUrl') or '',
            'duration': episode.get('duration'),
        }
