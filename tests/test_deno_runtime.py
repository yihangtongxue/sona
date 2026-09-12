"""Deno lookup regressions for manual execution; no binaries are launched."""

import tempfile
import unittest
from importlib.metadata import PathDistribution
from pathlib import Path
from unittest.mock import patch

from sona.deno_runtime import find_deno_binary


class DenoRuntimeTests(unittest.TestCase):
    def test_uses_distribution_record_outside_active_python_environment(self):
        for platform, scripts, relative, executable in (
            ('darwin', 'bin', '../../../bin/deno', 'deno'),
            ('win32', 'Scripts', '../../Scripts/deno.exe', 'deno.exe'),
        ):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = root / 'project-env'
                site = project / ('Lib/site-packages' if platform == 'win32' else 'lib/python3.13/site-packages')
                metadata = site / 'deno-2.9.6.dist-info'
                metadata.mkdir(parents=True)
                (metadata / 'RECORD').write_text(f'{relative},,\n', encoding='utf-8')
                binary = project / scripts / executable
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b'placeholder; never executed')
                with (patch('sona.deno_runtime.distribution', return_value=PathDistribution(metadata)),
                      patch('sona.deno_runtime.sys.platform', platform),
                      patch('sys.prefix', str(root / 'build-overlay'))):
                    self.assertEqual(find_deno_binary(), binary.resolve())
                    binary.unlink()
                    with self.assertRaises(FileNotFoundError):
                        find_deno_binary()

    def test_missing_record_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('sona.deno_runtime.distribution', return_value=PathDistribution(Path(temporary))):
                with self.assertRaises(FileNotFoundError):
                    find_deno_binary()


if __name__ == '__main__':
    unittest.main()
