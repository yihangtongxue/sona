"""Disposable ZIPs and mocked process/filesystem calls; never installs an app."""

import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from sona.updates import windows
from sona.updates.common import verify_job_archive
from sona.updates.protocol import UpdateError, current_target, parse_manifest
from sona.version import VERSION
from test_updates import TEST_PUBLIC, manifest, sign_asset


class WindowsProtocolTests(unittest.TestCase):
    def test_selects_windows_zip_and_preserves_platform_in_signature(self):
        data = manifest()
        windows_asset = dict(data["assets"][0], platform="windows", architecture="x64", fileName="Sona-windows.zip")
        sign_asset(data["version"], windows_asset)
        data["assets"].append(windows_asset)
        with patch("sona.updates.signatures.read_public_key", return_value=TEST_PUBLIC):
            release = parse_manifest(data, "x64", platform="windows")[1]
            self.assertEqual(release.platform, "windows")
            self.assertEqual(release.signed_asset(), {key: windows_asset[key] for key in release.signed_asset()})
            self.assertIsNone(parse_manifest(data, "arm64", platform="windows")[1])

    def test_windows_never_uses_macos_or_universal_package(self):
        data = manifest()
        self.assertIsNone(parse_manifest(data, "x64", platform="windows")[1])
        asset = dict(data["assets"][0], platform="windows", architecture="universal")
        sign_asset(data["version"], asset)
        data["assets"].append(asset)
        self.assertIsNone(parse_manifest(data, "x64", platform="windows")[1])

    def test_target_normalizes_windows_amd64(self):
        with patch("sona.updates.protocol.sys.platform", "win32"), patch("sona.updates.protocol.platform.machine", return_value="AMD64"):
            self.assertEqual(current_target(), ("windows", "x64"))

    def test_helper_rejects_valid_package_for_wrong_platform_before_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "release.json").write_text(json.dumps({"version": "1.1.0", "asset": manifest()["assets"][0]}))
            with patch("sona.updates.common.verify_archive") as verify, self.assertRaises(UpdateError):
                verify_job_archive(directory, "1.1.0", platform="windows", architecture="x64")
            verify.assert_not_called()


class WindowsArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "update.zip"

    def zip(self, names):
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name in names:
                archive.writestr(name, b"content")

    def test_extracts_application_directory(self):
        self.zip(["Sona/Sona.exe", "Sona/_internal/package/file.py"])
        app = windows.extract_app(self.archive, self.root / "unpacked")
        self.assertEqual((app / "Sona.exe").read_bytes(), b"content")

    def test_windows_path_aliases_traversal_and_streams_rejected(self):
        for index, name in enumerate(("Sona/../outside", "/Sona/file", "Sona/a:stream", "Sona/CON.txt",
                "Sona/LPT1", "Sona/file.", "Sona/file ", "Sona/a\\b", "Sona/./file", "Sona//file")):
            with self.subTest(name=name):
                self.zip([name])
                with self.assertRaises(UpdateError):
                    windows.extract_app(self.archive, self.root / f"unpacked-{index}")

    def test_case_alias_and_symlinks_rejected(self):
        self.zip(["Sona/Sona.exe", "Sona/sona.exe"])
        with self.assertRaises(UpdateError):
            windows.extract_app(self.archive, self.root / "case-alias")
        with zipfile.ZipFile(self.archive, "w") as archive:
            link = zipfile.ZipInfo("Sona/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../outside")
        with self.assertRaises(UpdateError):
            windows.extract_app(self.archive, self.root / "link")

    def test_waits_for_transient_windows_file_locks(self):
        with patch("sona.updates.windows.os.replace", side_effect=[PermissionError(), None]) as replace, \
             patch("sona.updates.windows.time.sleep"):
            windows.move_directory(self.root / "old", self.root / "new")
        self.assertEqual(replace.call_count, 2)

    def test_replacement_failure_restores_old_application(self):
        # Mock the helper boundaries; exercise the real replacement/rollback branch.
        import uuid
        from types import SimpleNamespace
        directory = self.root / "updates" / str(uuid.uuid4())
        directory.mkdir(parents=True)
        target = self.root / "install/app"
        target.mkdir(parents=True)
        (target / "Sona.exe").write_bytes(b"old")
        (directory / "install.json").write_text(json.dumps({"target": str(target), "version": "9.0.0", "parent_pid": 12345}))
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "Sona.exe").write_bytes(b"new")
        import os
        real_replace = os.replace

        def replace(source, destination):
            if Path(source).name == "app" and Path(source).parent.name.startswith(".sona-update-"):
                raise OSError("replacement failed")
            real_replace(source, destination)

        with patch("sona.updates.windows.sys.platform", "win32"), \
             patch("sona.updates.windows.sys.frozen", True, create=True), \
             patch("sona.updates.windows.sys.executable", str(directory / "runner/Sona/Sona.exe")), \
             patch("sona.updates.windows.get_app_paths", return_value=SimpleNamespace(data_dir=self.root)), \
             patch("sona.updates.windows.check_target"), patch("sona.updates.windows.process_running", return_value=False), \
             patch("sona.updates.windows.signing_requirement"), patch("sona.updates.windows.verify_job_archive"), \
             patch("sona.updates.windows.validate_app"), patch("sona.updates.windows.extract_app", return_value=staged), \
             patch("sona.updates.windows.move_directory", side_effect=replace), patch("sona.updates.windows.launch"):
            windows.apply_update(str(directory))
        self.assertEqual((target / "Sona.exe").read_bytes(), b"old")
        result = json.loads((directory / "result.json").read_text())
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["replaced"])


if __name__ == "__main__":
    unittest.main()
