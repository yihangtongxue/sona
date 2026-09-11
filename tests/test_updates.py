"""Mocked update requests and disposable files; no installs or signing tools."""

import base64
import hashlib
import io
import json
import plistlib
import socket
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sona.activity import ActivityGate
from sona.database import ModelRepository
from sona.paths import AppPaths
from sona.updates.macos import extract_app, installed_bundle, signing_requirement, validate_app
from sona.updates.protocol import Release, UpdateError, download, fetch_manifest, parse_manifest, public_url, version_tuple
from sona.updates.service import UpdateService
from sona.updates.signatures import SignatureError, artifact_payload, verify_signature, verify_archive
from sona.version import BUNDLE_ID, UPDATE_MANIFEST_URL, VERSION


# Disposable deterministic TEST key, never included in packaging inputs.
TEST_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
TEST_PUBLIC = TEST_KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign_asset(version, asset):
    payload = json.dumps(artifact_payload(version, asset), sort_keys=True, separators=(",", ":")).encode()
    asset["updateSignature"] = {"algorithm": "ed25519", "keyId": hashlib.sha256(TEST_PUBLIC).hexdigest(),
        "payload": base64.b64encode(payload).decode(), "signature": base64.b64encode(TEST_KEY.sign(payload)).decode()}
    return asset


def manifest(version="1.1.0"):
    result = {"schemaVersion": 1, "channel": "stable", "version": version, "tag": f"v{version}", "notes": "更新说明",
            "assets": [{"fileName": "Sona.zip", "platform": "macos", "architecture": "arm64", "packageType": "zip",
                        "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
                        "downloadUrl": "https://gitee.com/example/releases/download/v1.1.0/Sona.zip"}]}
    sign_asset(version, result["assets"][0])
    return result


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        mocked_key = patch("sona.updates.signatures.read_public_key", return_value=TEST_PUBLIC)
        mocked_key.start()
        self.addCleanup(mocked_key.stop)

    def test_identity_and_numeric_version_order(self):
        self.assertEqual(VERSION, "1.0.0")
        self.assertIn("sona-releases", UPDATE_MANIFEST_URL)
        self.assertTrue(UPDATE_MANIFEST_URL.endswith("?ref=main"))
        self.assertGreater(version_tuple("1.10.0"), version_tuple("1.9.0"))
        for value in ("v1.0.0", "1.0", "01.0.0", "1.0.0-beta", "1.0.0\n", None):
            with self.subTest(value=value), self.assertRaises(UpdateError):
                version_tuple(value)

    def test_exact_target_and_explicit_universal_fallback(self):
        data = manifest()
        universal = dict(data["assets"][0], architecture="universal", fileName="universal.zip")
        sign_asset(data["version"], universal)
        data["assets"].insert(0, universal)
        self.assertEqual(parse_manifest(data)[1].architecture, "arm64")
        data["assets"].pop()
        self.assertEqual(parse_manifest(data)[1].architecture, "universal")
        data["assets"][0]["architecture"] = "x64"
        self.assertIsNone(parse_manifest(data)[1])

    def test_reject_bad_schema_channel_duplicate_or_digest(self):
        for change in (lambda m: m.update(schemaVersion=2), lambda m: m.update(channel="beta"),
                       lambda m: m["assets"].append(dict(m["assets"][0])),
                       lambda m: m["assets"][0].update(sha256="bad"),
                       lambda m: m["assets"][0].update(size=True),
                       lambda m: m["assets"][0].update(fileName="../Sona.zip")):
            data = manifest()
            change(data)
            with self.assertRaises(UpdateError):
                parse_manifest(data)

    def test_untrusted_urls_are_rejected(self):
        for url in ("http://gitee.com/a", "file:///tmp/app", "https://user:secret@gitee.com/a",
                    "https://127.0.0.1/a", "https://10.0.0.1/a", "https://[::1]/a",
                    "https://host.local/a", "https://gitee.com/a?access_token=secret"):
            with self.subTest(url=url), self.assertRaises(UpdateError):
                public_url(url)
        with patch("sona.updates.protocol.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]):
            with self.assertRaises(UpdateError):
                public_url("https://example.com/app", resolve=True)

    def test_contents_decoding_and_unpublished_gitee_response(self):
        envelope = {"content": base64.b64encode(json.dumps(manifest()).encode()).decode()}
        with patch("sona.updates.protocol.open_public", return_value=io.BytesIO(json.dumps(envelope).encode())):
            self.assertEqual(fetch_manifest(), manifest())
        with patch("sona.updates.protocol.open_public", return_value=io.BytesIO(b"[]")):
            self.assertIsNone(fetch_manifest())
        with patch("sona.updates.protocol.open_public", return_value=io.BytesIO(b'{}')):
            with self.assertRaises(UpdateError):
                fetch_manifest()

    def test_proxy_dns_is_allowed_only_for_hostnames(self):
        for address in ("198.18.0.129", "198.19.255.254"):
            with self.subTest(address=address):
                with patch("sona.updates.protocol.socket.getaddrinfo",
                           return_value=[(0, 0, 0, "", (address, 443))]):
                    self.assertEqual(public_url(UPDATE_MANIFEST_URL, resolve=True), UPDATE_MANIFEST_URL)
                with self.assertRaises(UpdateError):
                    public_url(f"https://{address}/app", resolve=True)

    def test_private_dns_remains_blocked_including_mixed_answers(self):
        for address in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "fc00::1"):
            answers = [(0, 0, 0, "", ("198.18.0.129", 443)), (0, 0, 0, "", (address, 443))]
            with self.subTest(address=address), patch(
                "sona.updates.protocol.socket.getaddrinfo", return_value=answers,
            ):
                with self.assertRaisesRegex(UpdateError, "非公网地址"):
                    public_url(UPDATE_MANIFEST_URL, resolve=True)

    def test_dns_failure_has_a_network_message(self):
        with patch("sona.updates.protocol.socket.getaddrinfo", side_effect=socket.gaierror()):
            with self.assertRaisesRegex(UpdateError, "无法解析更新服务器"):
                public_url(UPDATE_MANIFEST_URL, resolve=True)
        with patch("sona.updates.protocol.socket.getaddrinfo", return_value=[]):
            with self.assertRaisesRegex(UpdateError, "无法解析更新服务器"):
                public_url(UPDATE_MANIFEST_URL, resolve=True)

    def test_download_requires_exact_size_and_hash_and_removes_partial(self):
        release = parse_manifest(manifest())[1]
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "update.zip"
            with patch("sona.updates.protocol.open_public", return_value=io.BytesIO(b"abc")):
                download(release, destination, lambda: False, Mock())
            self.assertEqual(destination.read_bytes(), b"abc")
            destination.unlink()
            for response in (b"ab", b"abcd", b"xyz"):
                with patch("sona.updates.protocol.open_public", return_value=io.BytesIO(response)):
                    with self.assertRaises(UpdateError):
                        download(release, destination, lambda: False, Mock())
                self.assertFalse(destination.exists())

    def test_missing_signature_and_wrong_publisher_are_rejected(self):
        data = manifest()
        data["assets"][0].pop("updateSignature")
        with self.assertRaisesRegex(UpdateError, "缺少发布者签名"):
            parse_manifest(data)
        asset = manifest()["assets"][0]
        with self.assertRaises(SignatureError):
            verify_signature("1.1.0", asset, public_key=bytes(reversed(TEST_PUBLIC)))

    def test_signed_metadata_cannot_be_changed(self):
        for field, value in (("fileName", "other.zip"), ("size", 4), ("sha256", "0" * 64),
                             ("platform", "windows"), ("architecture", "universal"), ("packageType", "dmg")):
            asset = manifest()["assets"][0]
            asset[field] = value
            with self.subTest(field=field), self.assertRaises(SignatureError):
                verify_signature("1.1.0", asset)
        with self.assertRaises(SignatureError):
            verify_signature("1.2.0", manifest()["assets"][0])

    def test_changing_signed_bytes_or_using_another_app_is_rejected(self):
        asset = manifest()["assets"][0]
        asset["updateSignature"]["payload"] = base64.b64encode(b"{}").decode()
        with self.assertRaises(SignatureError):
            verify_signature("1.1.0", asset)
        asset = manifest()["assets"][0]
        payload = artifact_payload("1.1.0", asset)
        payload["appId"] = "another.app"
        raw = json.dumps(payload).encode()
        asset["updateSignature"].update(payload=base64.b64encode(raw).decode(),
            signature=base64.b64encode(TEST_KEY.sign(raw)).decode())
        with self.assertRaises(SignatureError):
            verify_signature("1.1.0", asset)

    def test_helper_rechecks_archive_not_just_cached_signature(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / "update.zip"
            archive.write_bytes(b"abc")
            verify_archive(archive, "1.1.0", manifest()["assets"][0])
            archive.write_bytes(b"xyz")
            with self.assertRaises(SignatureError):
                verify_archive(archive, "1.1.0", manifest()["assets"][0])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "archive.zip"

    def zip(self, entries):
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, content, mode in entries:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, content)

    def test_preserves_executable_bits_and_internal_framework_links(self):
        self.zip([
            ("Sona.app/Contents/MacOS/Sona", b"binary", stat.S_IFREG | 0o755),
            ("Sona.app/Contents/Frameworks/F.framework/Versions/A/F", b"library", stat.S_IFREG | 0o755),
            ("Sona.app/Contents/Frameworks/F.framework/Versions/Current", b"A", stat.S_IFLNK | 0o777),
            ("Sona.app/Contents/Frameworks/F.framework/F", b"Versions/Current/F", stat.S_IFLNK | 0o777),
        ])
        app = extract_app(self.archive, self.root / "unpacked")
        self.assertTrue((app / "Contents/MacOS/Sona").stat().st_mode & 0o111)
        self.assertEqual((app / "Contents/Frameworks/F.framework/F").read_bytes(), b"library")

    def test_path_traversal_and_external_links_are_rejected(self):
        for index, entry in enumerate([
            ("Sona.app/../../outside", b"bad", stat.S_IFREG | 0o644),
            ("/Sona.app/Contents/a", b"bad", stat.S_IFREG | 0o644),
            ("Sona.app/Contents/link", b"../../../outside", stat.S_IFLNK | 0o777),
        ]):
            self.zip([entry])
            with self.assertRaises(UpdateError):
                extract_app(self.archive, self.root / f"case-{index}")
        self.assertFalse((self.root / "outside").exists())

    def test_link_parent_cannot_redirect_later_link_writes(self):
        self.zip([
            ("Sona.app/Contents/link", b"../real", stat.S_IFLNK | 0o777),
            ("Sona.app/Contents/link/nested", b"target", stat.S_IFLNK | 0o777),
        ])
        with self.assertRaises(UpdateError):
            extract_app(self.archive, self.root / "unpacked")

    def test_unsigned_or_wrong_version_app_is_rejected(self):
        app = self.root / "Sona.app"
        (app / "Contents").mkdir(parents=True)
        with (app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": "Sona",
                          "CFBundleShortVersionString": "1.0.0", "CFBundleVersion": "1.0.0"}, stream)
        with patch("sona.updates.macos.run_tool", side_effect=UpdateError("签名不匹配")):
            with self.assertRaises(UpdateError):
                validate_app(app, "requirement", "1.1.0")
        with patch("sona.updates.macos.run_tool"):
            with self.assertRaisesRegex(UpdateError, "内部版本"):
                validate_app(app, "requirement", "1.1.0")

    def test_ad_hoc_integrity_does_not_require_apple_membership(self):
        with patch("sona.updates.macos.app_info", return_value={"CFBundleShortVersionString": VERSION}), \
             patch("sona.updates.macos.run_tool", return_value=SimpleNamespace(stdout="", stderr="")) as tool:
            requirement = signing_requirement(self.root / "Sona.app")
        self.assertEqual(requirement, f'identifier "{BUNDLE_ID}"')
        self.assertEqual(tool.call_count, 1)
        self.assertIn("--verify", tool.call_args.args[0])


class UpdateStateTests(unittest.TestCase):
    def setUp(self):
        mocked_key = patch("sona.updates.signatures.read_public_key", return_value=TEST_PUBLIC)
        mocked_key.start()
        self.addCleanup(mocked_key.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = AppPaths("test", Path(temporary.name))
        ModelRepository(self.paths.database, ())
        self.activity = ActivityGate()
        with patch("sona.updates.service.installed_bundle", side_effect=UpdateError("开发模式")):
            self.service = UpdateService(self.paths, self.activity)
        self.addCleanup(self.service.close)

    def test_unpublished_does_not_mean_current_or_failure(self):
        with patch("sona.updates.service.fetch_manifest", return_value=None):
            self.service._check()
        self.assertEqual(self.service.status()["state"], "unpublished")

    def test_new_version_and_development_install_guard(self):
        with patch("sona.updates.service.fetch_manifest", return_value=manifest()):
            self.service._check()
        self.assertEqual(self.service.status()["state"], "available")
        self.assertFalse(self.service.status()["can_install"])
        with self.assertRaises(UpdateError):
            self.service.download()

    def test_no_downgrade(self):
        for version in ("0.9.0", "1.0.0"):
            with patch("sona.updates.service.fetch_manifest", return_value=manifest(version)):
                self.service._check()
            self.assertEqual(self.service.status()["state"], "current")

    def test_gate_blocks_install_while_working_then_blocks_new_jobs(self):
        with self.activity.operation():
            with self.assertRaises(ValueError):
                self.activity.freeze()
        self.activity.freeze()
        with self.assertRaises(ValueError), self.activity.operation():
            pass
        with self.activity.operation(background=True) as allowed:
            self.assertFalse(allowed)
        self.activity.thaw()
        with self.activity.operation() as allowed:
            self.assertTrue(allowed)

    def test_import_or_model_work_releases_gate_after_rejected_install(self):
        self.service._set(state="ready")
        self.service._directory = self.paths.data_dir
        self.service._quit = Mock()
        self.service._other_busy = lambda: True
        with self.assertRaises(UpdateError):
            self.service.install()
        with self.activity.operation() as allowed:
            self.assertTrue(allowed)
        self.service._quit.assert_not_called()

    def test_database_from_newer_app_is_not_downgraded(self):
        import sqlite3
        with sqlite3.connect(self.paths.database) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ValueError, "不能直接降级"):
            ModelRepository(self.paths.database, ())


if __name__ == "__main__":
    unittest.main()
