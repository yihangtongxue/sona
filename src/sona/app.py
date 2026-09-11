import sys
from pathlib import Path

import webview

from .api import AppApi
from .audio_library import AudioLibrary
from .database import ModelRepository
from .model_service import ModelService
from .models import BUILTIN_MODELS
from .paths import get_app_paths
from .providers.apple_speech import AppleSpeechProvider
from .providers.whisper import WhisperProvider


def main() -> None:
    paths = get_app_paths()
    repository = ModelRepository(paths.database, BUILTIN_MODELS)
    audio_library = AudioLibrary(paths.database, paths.audio_dir)
    model_service = ModelService(repository, {
        "apple-speech": AppleSpeechProvider(),
        "whisper": WhisperProvider(paths.models_dir, paths.downloads_dir),
    }, BUILTIN_MODELS)
    web_root = Path(__file__).with_name("web")
    icon_path = Path(__file__).resolve().parents[2] / "assets" / "Sona.icns"
    try:
        webview.create_window(
            "Sona", str(web_root / "index.html"),
            width=960, height=640, min_size=(720, 480),
            js_api=AppApi(model_service, audio_library),
        )
        # pywebview's Windows backend requires an .ico file; passing the macOS
        # .icns asset makes System.Drawing fail before the window is shown.
        start_options: dict[str, object] = {"http_server": True}
        if sys.platform == "darwin" and icon_path.is_file():
            start_options["icon"] = str(icon_path)
        webview.start(**start_options)
    finally:
        try:
            audio_library.close()
        finally:
            model_service.close()
