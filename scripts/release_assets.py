"""Describe release artifacts and check packaged identity before signing."""

import hashlib
import json
import plistlib
import zipfile

from release_support import PUBLIC_KEY
from sona.version import BUNDLE_ID, RELEASE_REPOSITORY, UPDATE_MANIFEST_URL, VERSION


def expected_assets():
    return {
        f"Sona-{VERSION}-macos-arm64.zip": ("macos", "arm64", "zip"),
        f"Sona-{VERSION}-macos-arm64.dmg": ("macos", "arm64", "dmg"),
        f"Sona-{VERSION}-windows-x64.zip": ("windows", "x64", "zip"),
        f"Sona-{VERSION}-windows-x64-setup.exe": ("windows", "x64", "exe"),
    }


def validate_zip(path, platform, public_record):
    with zipfile.ZipFile(path) as archive:
        # Bound metadata before decompression and reject duplicate entries.
        if platform == "macos":
            info_name = "Sona.app/Contents/Info.plist"
            key_name = "Sona.app/Contents/Resources/sona/updates/update-public-key.json"
        else:
            info_name = "Sona/release-info.json"
            key_name = "Sona/_internal/sona/updates/update-public-key.json"
        for name in (info_name, key_name):
            matches = [entry for entry in archive.infolist() if entry.filename == name]
            if len(matches) != 1 or matches[0].file_size > 32768:
                raise ValueError("ZIP 内发行元数据缺失、重复或过大。")
        bundled_key = json.loads(archive.read(key_name))
        if platform == "macos":
            info = plistlib.loads(archive.read(info_name))
            valid = (info.get("CFBundleIdentifier") == BUNDLE_ID and info.get("CFBundleVersion") == VERSION
                     and info.get("CFBundleShortVersionString") == VERSION
                     and info.get("SonaUpdateManifestURL") == UPDATE_MANIFEST_URL
                     and info.get("SonaReleaseRepository") == RELEASE_REPOSITORY)
        else:
            info = json.loads(archive.read(info_name))
            valid = (info.get("appId") == BUNDLE_ID and info.get("version") == VERSION
                     and info.get("platform") == "windows" and info.get("architecture") == "x64"
                     and info.get("executable") == "Sona.exe"
                     and info.get("updateManifestURL") == UPDATE_MANIFEST_URL
                     and info.get("releaseRepository") == RELEASE_REPOSITORY)
        if not valid or bundled_key != public_record:
            raise ValueError("ZIP 内的应用身份、版本、GitHub 更新源或公钥不匹配。")


def describe_asset(path):
    target = expected_assets().get(path.name)
    if target is None or not path.is_file() or path.is_symlink():
        raise ValueError(f"不是当前版本的受支持发布产物：{path.name}")
    if target[2] == "zip":
        validate_zip(path, target[0], json.loads(PUBLIC_KEY.read_text(encoding="utf-8")))
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    # GitHub Releases limits each individual release asset to under 2 GiB.
    if not 0 < size < 2 * 1024**3:
        raise ValueError("GitHub Release 单个安装包必须小于 2 GiB。")
    return {"platform": target[0], "architecture": target[1], "packageType": target[2],
            "fileName": path.name, "size": size, "sha256": digest.hexdigest()}
