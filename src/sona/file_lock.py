"""Process locks whose ownership ends automatically when a process exits."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class FileLocked(Exception):
    pass


@contextmanager
def exclusive_file_lock(path: Path):
    # Do not unlink lock files: every process must keep locking the same inode.
    with path.open("a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FileLocked() from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
