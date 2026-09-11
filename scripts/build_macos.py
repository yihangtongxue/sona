"""Manual build only; requires --confirm-version and an existing public key."""

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sona.updates.signatures import read_public_key
from sona.version import BUNDLE_ID, VERSION


def run(arguments, **kwargs):
    subprocess.run([str(item) for item in arguments], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--confirm-version", required=True)
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64" or int(platform.mac_ver()[0].split('.')[0]) < 26:
        raise ValueError("当前打包配置需要 Apple 芯片 Mac、macOS 26 或更高版本。")
    if args.confirm_version != VERSION:
        raise ValueError(f"请先确认打包版本，当前为 {VERSION}。")
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = next(package for package in lock["package"] if package["name"] == "sona")
    if metadata["project"]["version"] != VERSION or locked["version"] != VERSION:
        raise ValueError("源码、项目配置和锁文件版本不一致。")
    if importlib.metadata.version("pyinstaller") != "6.22.2":
        raise ValueError("请使用已选定的 PyInstaller 6.22.2。")
    public_path = args.public_key.expanduser().resolve()
    read_public_key(public_path)
    public_record = json.loads(public_path.read_text())
    # Only the public record is copied; reject a mixed file containing other keys.
    if set(public_record) != {"algorithm", "keyId", "publicKey"}:
        raise ValueError("公钥文件包含多余字段，请使用密钥工具生成的独立公钥文件。")
    destination = ROOT / "dist" / f"Sona-{VERSION}-macos-arm64"
    if destination.exists():
        raise ValueError(f"输出目录已存在，不覆盖已有产物：{destination}")
    destination.mkdir(parents=True)
    build_root = ROOT / "build"
    build_root.mkdir(exist_ok=True)
    # Preserve build diagnostics; no recursive deletion or replacement of old runs.
    stage = Path(tempfile.mkdtemp(prefix="macos-", dir=build_root))
    (stage / "update-public-key.json").write_text(json.dumps(public_record, indent=2))
    helper = stage / "speech_asset_manager"
    # Minimal embedded plist identifies the standalone speech helper for privacy prompts.
    import plistlib
    helper_info = stage / "speech-helper.plist"
    helper_info.write_bytes(plistlib.dumps({"CFBundleIdentifier": BUNDLE_ID + ".speech",
        "CFBundleName": "Sona", "NSSpeechRecognitionUsageDescription": "用于将你选择的音频转录为文字。"}))
    run(["xcrun", "swiftc", "-O", "-target", "arm64-apple-macos26.0",
         ROOT / "src/sona/native/speech_asset_manager.swift", "-o", helper,
         "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist", "-Xlinker", helper_info])
    env = {**os.environ, "SONA_BUILD_STAGE": str(stage), "SONA_BUILD_VERSION": VERSION,
           "MACOSX_DEPLOYMENT_TARGET": "26.0", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    run([sys.executable, "-m", "PyInstaller", ROOT / "packaging/macos/Sona.spec",
         "--distpath", stage / "dist", "--workpath", stage / "work"], env=env, cwd=ROOT)
    app = stage / "dist/Sona.app"
    # Ad-hoc seal, NOT Developer ID or Apple notarization. The signed ZIP provides
    # publisher authentication. Do not touch Gatekeeper/quarantine settings.
    run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", "--identifier", BUNDLE_ID, app])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
    published_app = destination / "Sona.app"
    shutil.copytree(app, published_app, symlinks=True)
    archive = destination / f"Sona-{VERSION}-macos-arm64.zip"
    run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", published_app, archive])
    image_root = stage / "dmg"
    image_root.mkdir()
    shutil.copytree(app, image_root / "Sona.app", symlinks=True)
    (image_root / "Applications").symlink_to("/Applications")
    run(["/usr/bin/hdiutil", "create", "-volname", "Sona", "-srcfolder", image_root,
         "-format", "UDZO", destination / f"Sona-{VERSION}-macos-arm64.dmg"])
    (destination / "build-info.json").write_text(json.dumps({"version": VERSION, "minimumMacOS": "26.0",
        "architecture": "arm64", "keyId": public_record["keyId"], "signing": "ad-hoc",
        "notarized": False, "python": platform.python_version()}, indent=2))
    print(f"打包产物：{destination}\n尚未生成发布签名或发布，请先验收，再运行 release_keys.py sign。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, importlib.metadata.PackageNotFoundError) as error:
        raise SystemExit(f"打包未完成：{error}") from None
