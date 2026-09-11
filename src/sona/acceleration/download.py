"""Bounded, resumable transfers and verified, immutable runtime installations."""

import hashlib
import json
import logging
import os
import re
import shutil
import uuid
import zipfile
from pathlib import PurePosixPath
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .catalog import ARCHIVES, BASE, BUNDLE, REQUIRED_DLLS, TOTAL_BYTES

logger = logging.getLogger(__name__)


class Paused(Exception):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Paused()


def digest(path, cancel):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            check_cancel(cancel)
            result.update(block)
    return result.hexdigest()


def downloaded_bytes(cache):
    return sum(min((cache / f'{name}.zip').stat().st_size, size)
               for name, _, size, _ in ARCHIVES if (cache / f'{name}.zip').is_file())


def transfer(path, relative, size, cancel, progress):
    offset = path.stat().st_size if path.exists() else 0
    if offset > size:
        path.unlink()
        offset = 0
    if offset == size:
        return
    check_cancel(cancel)
    headers = {'Accept-Encoding': 'identity', 'User-Agent': 'Sona/0.1'}
    if offset:
        headers['Range'] = f'bytes={offset}-'
    logger.info('下载加速组件 url=%s offset=%d size=%d', BASE + relative, offset, size)
    try:
        response = urlopen(Request(BASE + relative, headers=headers), timeout=10)
    except HTTPError as error:
        if error.code == 416:
            path.unlink(missing_ok=True)
        raise
    with response:
        if response.status == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
            if not match or tuple(map(int, match.groups())) != (offset, size - 1, size):
                raise ValueError('Unexpected Content-Range')
        elif response.status == 200:
            offset = 0  # A server may ignore Range; safely start this archive over.
        else:
            raise ValueError(f'Unexpected HTTP status {response.status}')
        length = response.headers.get('Content-Length')
        if length is not None and int(length) != size - offset:
            raise ValueError('Unexpected Content-Length')
        with path.open('ab' if offset else 'wb') as stream:
            while True:
                check_cancel(cancel)
                block = response.read1(1024 * 1024)
                if not block:
                    break
                if offset + len(block) > size:
                    raise ValueError('Archive exceeds expected size')
                stream.write(block)
                offset += len(block)
                progress()
            stream.flush()
            os.fsync(stream.fileno())
        if offset != size:
            raise OSError('Download interrupted before expected size')


def install(root, cancel, emit):
    # The caller owns the cross-process operation lock. Remove only incomplete
    # staging directories created by this installer, never published runtimes.
    for leftover in root.glob('.staging-*'):
        check_cancel(cancel)
        if (re.fullmatch(r'\.staging-[0-9a-f]{32}', leftover.name) and not leftover.is_symlink()
                and leftover.resolve().parent == root.resolve() and leftover.is_dir()):
            shutil.rmtree(leftover)
    cache = root / 'downloads' / BUNDLE
    cache.mkdir(parents=True, exist_ok=True)
    # Allow space for verified archives, selected DLLs, and staging. Old active
    # installations are immutable and excluded from this budget.
    remaining = TOTAL_BYTES - downloaded_bytes(cache)
    if shutil.disk_usage(root).free < remaining + 3 * 1024**3:
        raise OSError('Insufficient disk space: need archive remainder plus 3 GiB for extraction')
    for name, relative, size, expected in ARCHIVES:
        check_cancel(cancel)
        archive = cache / f'{name}.zip'
        transfer(archive, relative, size, cancel,
                 lambda: emit('downloading', downloaded_bytes(cache)))
        emit('preparing', downloaded_bytes(cache))
        logger.info('校验加速组件 archive=%s', name)
        if digest(archive, cancel) != expected:
            archive.unlink()  # A retry must not reuse corrupted bytes.
            raise ValueError(f'SHA-256 mismatch: {name}')
    staging = root / f'.staging-{uuid.uuid4().hex}'
    staging.mkdir()
    files = {}
    try:
        emit('preparing', TOTAL_BYTES)
        for name, _, _, _ in ARCHIVES:
            with zipfile.ZipFile(cache / f'{name}.zip') as archive:
                for entry in archive.infolist():
                    check_cancel(cancel)
                    parts = PurePosixPath(entry.filename).parts
                    if '..' in parts or entry.filename.startswith(('/', '\\')) or '\\' in entry.filename:
                        raise ValueError('Unsafe archive path')
                    if entry.is_dir():
                        continue
                    basename = parts[-1]
                    is_dll = basename.lower().endswith('.dll') and 'bin' in parts
                    is_license = basename.lower().startswith(('license', 'eula', 'notice'))
                    if not (is_dll or is_license):
                        continue
                    if ':' in basename or entry.file_size > 2 * 1024**3:
                        raise ValueError('Invalid archive entry')
                    destination = staging / (basename if is_dll else f'{name}-{basename}')
                    if destination.exists():
                        raise ValueError(f'Duplicate archive file: {basename}')
                    with archive.open(entry) as source, destination.open('wb') as target:
                        while block := source.read(1024 * 1024):
                            check_cancel(cancel)
                            target.write(block)
                    files[destination.name] = {'size': destination.stat().st_size,
                                               'sha256': digest(destination, cancel)}
        if any(name not in files for name in REQUIRED_DLLS):
            raise ValueError('Runtime archive is missing required DLLs')
        if not any(name.startswith('nvrtc-builtins') and name.endswith('.dll') for name in files):
            raise ValueError('Runtime archive is missing NVRTC builtins')
        (staging / 'manifest.json').write_text(json.dumps({'bundle': BUNDLE, 'files': files}), encoding='utf-8')
        destination = root / f'{BUNDLE}-{uuid.uuid4().hex}'
        staging.rename(destination)
        # Completed ZIPs can be removed: this installation has its own hashes.
        for name, _, _, _ in ARCHIVES:
            try:
                (cache / f'{name}.zip').unlink(missing_ok=True)
            except OSError:
                logger.warning('已安装组件的下载缓存清理失败 archive=%s', name, exc_info=True)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_files(path, cancel):
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('bundle') != BUNDLE:
        raise ValueError('Runtime bundle version mismatch')
    files = manifest.get('files', {})
    if any(name not in files for name in REQUIRED_DLLS):
        raise ValueError('Runtime manifest is incomplete')
    if not any(name.startswith('nvrtc-builtins') and name.endswith('.dll') for name in files):
        raise ValueError('Runtime manifest is missing NVRTC builtins')
    for name, info in files.items():
        if PurePosixPath(name).name != name or '\\' in name or ':' in name:
            raise ValueError('Invalid installed file path')
        file = path / name
        if not file.is_file() or file.stat().st_size != info['size'] or digest(file, cancel) != info['sha256']:
            raise ValueError(f'Installed runtime file is damaged: {name}')
