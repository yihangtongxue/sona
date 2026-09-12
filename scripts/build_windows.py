"""Build a per-user Windows x64 installer and a complete signed-update payload."""

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from release_support import PUBLIC_KEY, ROOT, check_build
from sona.version import BUNDLE_ID, RELEASE_REPOSITORY, UPDATE_MANIFEST_URL, VERSION


def run(arguments, **kwargs):
    subprocess.run([str(item) for item in arguments], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-key", type=Path, default=PUBLIC_KEY)
    parser.add_argument("--confirm-version", required=True)
    parser.add_argument("--iscc", type=Path, default=Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe")
    parser.add_argument("--webview-bootstrapper", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"} or struct.calcsize("P") != 8:
        raise ValueError("当前打包配置需要 Windows x64 和 64 位 Python。")
    record = check_build(args.confirm_version, args.public_key.expanduser().resolve())
    if not args.iscc.is_file() or not args.webview_bootstrapper.is_file():
        raise ValueError("需要 Inno Setup 6 和微软 WebView2 安装引导程序。")
    # The installer executes this file on machines missing WebView2. Authenticate
    # Microsoft's bootstrapper again here, including for manually invoked builds.
    env = {**os.environ, "SONA_WEBVIEW_BOOTSTRAPPER": str(args.webview_bootstrapper.resolve())}
    run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "$signature = Get-AuthenticodeSignature -LiteralPath $env:SONA_WEBVIEW_BOOTSTRAPPER; "
         "if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(,|$)') { exit 1 }"], env=env)
    destination = ROOT / "dist" / f"Sona-{VERSION}-windows-x64"
    if destination.exists():
        raise ValueError(f"输出目录已存在，不覆盖已有产物：{destination}")
    destination.mkdir(parents=True)
    (ROOT / "build").mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="windows-", dir=ROOT / "build"))
    (stage / "update-public-key.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    # ICO can contain the existing 256px PNG directly; no image tooling needed.
    png = (ROOT / "assets/Sona.iconset/icon_256x256.png").read_bytes()
    (stage / "Sona.ico").write_bytes(struct.pack("<HHH", 0, 1, 1) +
        struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22) + png)
    env.update(SONA_BUILD_STAGE=str(stage), SONA_BUILD_VERSION=VERSION, LITELLM_LOCAL_MODEL_COST_MAP="True")
    run([sys.executable, "-m", "PyInstaller", ROOT / "packaging/windows/Sona.spec",
         "--distpath", stage / "dist", "--workpath", stage / "work"], cwd=ROOT, env=env)
    app = stage / "dist/Sona"
    info = {"appId": BUNDLE_ID, "version": VERSION, "platform": "windows", "architecture": "x64",
            "executable": "Sona.exe", "releaseRepository": RELEASE_REPOSITORY,
            "updateManifestURL": UPDATE_MANIFEST_URL, "keyId": record["keyId"]}
    (app / "release-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    from sona.updates.windows import validate_app
    from sona.updates.signatures import read_public_key
    validate_app(app, read_public_key(), VERSION)
    shutil.make_archive(str(destination / f"Sona-{VERSION}-windows-x64"), "zip", app.parent, "Sona")
    run([args.iscc, f"/DAppVersion={VERSION}", f"/DSourceDir={app}", f"/DOutputDir={destination}",
         f"/DAppIcon={stage / 'Sona.ico'}", f"/DWebViewBootstrapper={args.webview_bootstrapper.resolve()}",
         ROOT / "packaging/windows/Sona.iss"])
    info.update(python=platform.python_version(), signing="unsigned-authenticode")
    (destination / "build-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"打包产物：{destination}；尚未签名更新包或发布。")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, importlib.metadata.PackageNotFoundError) as error:
        raise SystemExit(f"打包未完成：{error}") from None
