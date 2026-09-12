import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parents[1]
stage = Path(os.environ["SONA_BUILD_STAGE"])
version = os.environ["SONA_BUILD_VERSION"]
datas = [(str(root / "src/sona/web"), "sona/web"),
         (str(root / "src/sona/model_catalog.json"), "sona"),
         (str(stage / "update-public-key.json"), "sona/updates")]
binaries = []
hiddenimports = ["webview.platforms.winforms", "webview.platforms.edgechromium", "keyring.backends.Windows",
                 "clr", "pythonnet", "certifi", "cryptography"]
for package in ("faster_whisper", "ctranslate2", "av", "litellm", "tiktoken", "tiktoken_ext", "clr_loader"):
    package_data, package_bins, package_imports = collect_all(package)
    datas += package_data
    binaries += package_bins
    hiddenimports += package_imports
datas += collect_data_files("webview")
datas += collect_data_files("pythonnet")
hiddenimports += collect_submodules("sona")
for distribution in ("sona", "pywebview", "keyring", "faster-whisper", "ctranslate2", "av", "litellm", "cryptography", "pythonnet"):
    datas += copy_metadata(distribution)

a = Analysis([str(root / "packaging/macos/entry.py")], pathex=[str(root / "src")],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest", "mlx", "mlx_whisper"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Sona", console=False,
          debug=False, strip=False, upx=False, icon=str(stage / "Sona.ico"),
          contents_directory="_internal")
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Sona")
