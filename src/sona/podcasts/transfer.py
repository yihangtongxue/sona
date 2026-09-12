"""Keep HTTP transfer failures structured across yt-dlp's retry boundary."""

from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking.exceptions import TransportError
from yt_dlp.utils import NO_DEFAULT


class EmptyMediaResponseError(TransportError):
    """A successful HTTP response did not contain any media bytes."""

    code = 'media_empty_response'

    def __init__(self):
        super().__init__('音频服务器返回了空内容，请重新获取。')


class MediaHttpFD(HttpFD):
    def report_retry(self, err, count, retries, frag_index=NO_DEFAULT, fatal=True):
        # Upstream stringifies the error after leaving the except block. Its
        # resulting DownloadError then loses both the cause and HTTP status.
        if fatal and count > retries:
            raise err
        return super().report_retry(err, count, retries, frag_index=frag_index, fatal=fatal)

    def report_error(self, message, *args, **kwargs):
        if message == 'Did not get any data blocks':
            raise EmptyMediaResponseError()
        # FileDownloader normally binds this method directly from the parent
        # YoutubeDL instance; it does not define a superclass implementation.
        return self.ydl.report_error(message, *args, **kwargs)
