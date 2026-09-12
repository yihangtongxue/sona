"""Normalize supported episode links without making a network request."""

import re
from urllib.parse import parse_qs, urlsplit


def normalize_episode(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('请粘贴小宇宙或 Apple Podcasts 的单集链接。')
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('https', 'http') or parsed.username or parsed.password
                or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)
                or re.search(r'[\s\x00-\x1f\\]', value)):
            raise ValueError()
        host = (parsed.hostname or '').lower()
        if host in ('www.xiaoyuzhoufm.com', 'xiaoyuzhoufm.com'):
            match = re.fullmatch(r'/episode/([a-fA-F0-9]{24})/?', parsed.path)
            if not match:
                raise ValueError('请复制小宇宙的单集链接，暂不支持整个播客的主页。')
            episode = match[1].lower()
            return 'xiaoyuzhou', episode, f'https://www.xiaoyuzhoufm.com/episode/{episode}'
        if host == 'podcasts.apple.com':
            match = re.fullmatch(r'/(?:(?P<country>[a-zA-Z]{2})/)?podcast/(?:[^/]+/)?id(?P<show>\d+)/?', parsed.path)
            episodes = parse_qs(parsed.query).get('i', [])
            if not match or len(episodes) != 1 or not re.fullmatch(r'\d{1,24}', episodes[0]):
                raise ValueError('请复制 Apple Podcasts 的单集链接（包含 i=单集编号），暂不支持节目主页。')
            country = (match['country'] or 'us').lower()
            return 'apple', episodes[0], f'https://podcasts.apple.com/{country}/podcast/id{match["show"]}?i={episodes[0]}'
    except ValueError as error:
        if str(error).startswith('请复制'):
            raise
        raise ValueError('链接格式无效，请粘贴完整的播客单集链接。') from None
    raise ValueError('暂仅支持小宇宙和 Apple Podcasts 的公开单集链接。')
