"""Build macOS release artifacts locally or in GitHub Actions, without secrets."""

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from release_support import PUBLIC_KEY, check_build
from sona.version import BUNDLE_ID, RELEASE_REPOSITORY, UPDATE_MANIFEST_URL, VERSION


def run(arguments, **kwargs):
    subprocess.run([str(item) for item in arguments], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-key", type=Path, default=PUBLIC_KEY)
    parser.add_argument("--confirm-version", required=True)
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64" or int(platform.mac_ver()[0].split('.')[0]) < 26:
        raise ValueError("当前打包配置需要 Apple 芯片 Mac、macOS 26 或更高版本。")
    public_path = args.public_key.expanduser().resolve()
    public_record = check_build(args.confirm_version, public_path)
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
           "SONA_BUILD_BUNDLE_ID": BUNDLE_ID, "SONA_UPDATE_MANIFEST_URL": UPDATE_MANIFEST_URL,
           "SONA_RELEASE_REPOSITORY": RELEASE_REPOSITORY,
           "MACOSX_DEPLOYMENT_TARGET": "26.0", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    run([sys.executable, "-m", "PyInstaller", ROOT / "packaging/macos/Sona.spec",
         "--distpath", stage / "dist", "--workpath", stage / "work"], env=env, cwd=ROOT)
    app = stage / "dist/Sona.app"
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if (info.get("CFBundleIdentifier") != BUNDLE_ID or info.get("CFBundleVersion") != VERSION
            or info.get("CFBundleShortVersionString") != VERSION
            or info.get("SonaUpdateManifestURL") != UPDATE_MANIFEST_URL
            or info.get("SonaReleaseRepository") != RELEASE_REPOSITORY):
        raise ValueError("打包后的应用标识、版本或更新地址不一致，已停止生成发布包。")
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
    (destination / "build-info.json").write_text(json.dumps({"version": VERSION, "appId": BUNDLE_ID,
        "releaseRepository": RELEASE_REPOSITORY, "updateManifestURL": UPDATE_MANIFEST_URL, "minimumMacOS": "26.0",
        "architecture": "arm64", "keyId": public_record["keyId"], "signing": "ad-hoc",
        "notarized": False, "python": platform.python_version()}, indent=2))
    print(f"打包产物：{destination}\n尚未生成发布签名或发布，请先验收，再运行 release_keys.py sign。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, importlib.metadata.PackageNotFoundError) as error:
        raise SystemExit(f"打包未完成：{error}") from None
