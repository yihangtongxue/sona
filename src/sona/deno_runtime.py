"""Locate the pinned Deno executable in its owning Python distribution."""

import sys
from importlib.metadata import distribution
from pathlib import Path


def find_deno_binary() -> Path:
    executable = 'deno.exe' if sys.platform == 'win32' else 'deno'
    installed = distribution('deno')
    # uv --with runs Python in an overlay while Deno lives in the project
    # environment. RECORD paths belong to that distribution, unlike sys.prefix
    # and sysconfig's scripts directory, which point to the overlay.
    for entry in installed.files or ():
        if entry.name == executable:
            binary = Path(installed.locate_file(entry)).resolve()
            if binary.is_file():
                return binary
    raise FileNotFoundError(f'已安装的 deno 包中未找到 {executable}，请重新同步项目依赖。')
