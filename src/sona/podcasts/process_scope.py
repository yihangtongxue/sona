"""Keep a YouTube worker and its JavaScript runtime in one cancellation scope."""

import os
import signal


def isolate_download():
    os.setsid()


def kill_download_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # Cancellation may precede setsid(), or the group already exited.
