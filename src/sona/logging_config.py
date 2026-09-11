"""Console logging shared by the desktop process and spawned inference workers."""

import logging
import os
import sys


class TaskContext(logging.Filter):
    def __init__(self, task_id):
        super().__init__()
        self.task_id = task_id

    def filter(self, record):
        if not hasattr(record, 'task_id'):
            record.task_id = self.task_id
        return True


def configure_logging(task_id='-'):
    name = os.environ.get('SONA_LOG_LEVEL', 'INFO').upper()
    level = {'DEBUG': logging.DEBUG, 'INFO': logging.INFO, 'WARNING': logging.WARNING,
             'ERROR': logging.ERROR, 'CRITICAL': logging.CRITICAL}.get(name, logging.INFO)
    logger = logging.getLogger('sona')
    logger.setLevel(level)
    logger.propagate = False
    # Only replace our own handler. Reinitialization must not duplicate output.
    for handler in tuple(logger.handlers):
        if getattr(handler, '_sona_console', False):
            logger.removeHandler(handler)
            handler.close()
    stream = sys.stderr or sys.__stderr__
    handler = logging.StreamHandler(stream) if stream is not None else logging.NullHandler()
    handler._sona_console = True
    handler.addFilter(TaskContext(task_id))
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-7s [%(processName)s:%(process)d] '
        '[task=%(task_id)s] %(name)s | %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(handler)
