"""Small, message-free failure diagnostics for the acquisition process boundary."""

import re


def failure_metadata(error):
    # yt-dlp may wrap HTTP/transport errors in DownloadError.exc_info, or
    # NoSupportingHandlers.unexpected_errors. Do not stringify any exception.
    pending = [error]
    seen = set()
    result = {'error_type': type(error).__name__, 'code': 'media_unknown',
              'http_status': None, 'function': '-', 'line': 0}
    while pending and len(seen) < 12:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        name = type(current).__name__
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name):
            result['error_type'] = name
        code = getattr(current, 'code', None)
        if code in ('media_invalid_url', 'media_unsupported_port', 'media_private_address',
                    'media_dns_failed', 'media_response_too_large', 'media_empty_response'):
            result['code'] = code
        status = getattr(current, 'status', None)
        if type(status) is int and 100 <= status <= 599:
            result['http_status'] = status
        trace = current.__traceback__
        while trace is not None:
            function = trace.tb_frame.f_code.co_name
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', function):
                result.update(function=function, line=trace.tb_lineno)
            trace = trace.tb_next
        pending.extend((current.__cause__, current.__context__, getattr(current, 'cause', None)))
        wrapped = getattr(current, 'exc_info', None)
        if isinstance(wrapped, tuple) and len(wrapped) == 3:
            pending.append(wrapped[1])
        unexpected = getattr(current, 'unexpected_errors', None)
        if isinstance(unexpected, list):
            pending.extend(unexpected[:12])
    return result
