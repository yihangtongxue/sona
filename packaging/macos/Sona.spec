# Invoked by scripts/build_macos.py, never directly with production secrets.
import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parents[1]
stage = Path(os.environ["SONA_BUILD_STAGE"])
version = os.environ["SONA_BUILD_VERSION"]
bundle_id = os.environ["SONA_BUILD_BUNDLE_ID"]
minimum = "26.0"  # Current locked mlx-metal wheel targets macOS 26.

datas = [(str(root / "src/sona/web"), "sona/web"),
         (str(root / "src/sona/model_catalog.json"), "sona"),
         (str(root / "assets/Sona.icns"), "sona/assets"),
         (str(stage / "update-public-key.json"), "sona/updates")]
binaries = [(str(stage / "speech_asset_manager"), "sona/native")]
hiddenimports = ["webview.platforms.cocoa", "keyring.backends.macOS", "certifi", "cryptography"]
# Dynamic imports and model/tokenizer resources cannot all be inferred from the
# GUI entry point. Collect installed packages only; never collect the workspace.
for package in ("mlx", "mlx_whisper", "litellm", "tiktoken", "tiktoken_ext"):
    package_data, package_bins, package_imports = collect_all(package)
    datas += package_data
    binaries += package_bins
    hiddenimports += package_imports
datas += collect_data_files("webview")
hiddenimports += collect_submodules("sona")
for distribution in ("sona", "pywebview", "keyring", "mlx", "mlx-metal", "mlx-whisper", "litellm", "cryptography"):
    datas += copy_metadata(distribution)

a = Analysis([str(root / "packaging/macos/entry.py")], pathex=[str(root / "src")],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Sona", console=False,
          debug=False, strip=False, upx=False, argv_emulation=False, target_arch="arm64",
          codesign_identity=None)
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Sona")
app = BUNDLE(collection, name="Sona.app", icon=str(root / "assets/Sona.icns"),
             bundle_identifier=bundle_id, version=version,
             info_plist={"CFBundleName": "Sona", "CFBundleDisplayName": "Sona",
                         "CFBundleVersion": version, "CFBundleShortVersionString": version,
                         "SonaUpdateManifestURL": os.environ["SONA_UPDATE_MANIFEST_URL"],
                         "SonaReleaseRepository": os.environ["SONA_RELEASE_REPOSITORY"],
                         "LSMinimumSystemVersion": minimum,
                         "NSHighResolutionCapable": True,
                         "NSSpeechRecognitionUsageDescription": "用于将你选择的音频转录为文字。",
                         "NSHumanReadableCopyright": "一航同学YIHANG · https://maxcosmos.top",
                         "SonaUpdateKeyId": json.loads((stage / "update-public-key.json").read_text())["keyId"]})
