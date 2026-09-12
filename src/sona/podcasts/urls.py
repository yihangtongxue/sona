"""Normalize supported media links without making a network request."""

import re
from urllib.parse import parse_qs, urlsplit


def source_link(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('请粘贴支持的平台链接，长度不能超过 4096 字符。')
    value = value.strip()
    if not value.startswith(('https://', 'http://')):
        links = re.findall(r'https?://[^\s<>"\u3000]+', value)
        if len(links) == 1:
            link = links[0].rstrip('。！？，；：、）】》」』)]')
            if urlsplit(link).hostname in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'):
                return link
    return value


def normalize_episode(value):
    value = source_link(value)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('https', 'http') or parsed.username or parsed.password
                or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)
                or re.search(r'[\s\x00-\x1f\\]', value)):
            raise ValueError()
        host = (parsed.hostname or '').lower()
        if host in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'):
            parts = parse_qs(parsed.query, keep_blank_values=True).get('p', [])
            if parts and (len(parts) != 1 or not re.fullmatch(r'[0-9]{1,5}', parts[0]) or int(parts[0]) < 1):
                raise ValueError('请复制有效的 B站分P链接，p 必须是一个正整数。')
            part = int(parts[0]) if parts else 1
            if host == 'b23.tv':
                match = re.fullmatch(r'/([a-zA-Z0-9]{1,64})/?', parsed.path)
                if not match:
                    raise ValueError('请复制 B站视频的完整 b23.tv 短链接。')
                query = f'?p={part}' if parts else ''
                return 'bilibili', f'short:{match[1]}{query}', f'https://b23.tv/{match[1]}{query}'
            match = re.fullmatch(r'/video/((?i:BV)[a-zA-Z0-9]{10}|(?i:av)[0-9]{1,20})/?', parsed.path)
            if not match:
                raise ValueError('请复制 B站 BV/AV 视频链接，暂不支持主页、合集、番剧或直播。')
            video = match[1]
            video = 'BV' + video[2:] if video[:2].lower() == 'bv' else 'av' + str(int(video[2:]))
            return 'bilibili', f'{video}:p{part}', f'https://www.bilibili.com/video/{video}?p={part}'
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
        raise ValueError('链接格式无效，请粘贴完整的单集或视频链接。') from None
    raise ValueError('暂支持小宇宙、Apple Podcasts 的公开单集和 B站公开视频链接。')
