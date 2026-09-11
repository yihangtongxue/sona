import sys
import multiprocessing
import logging
import platform
from pathlib import Path

import webview

from .api import AppApi
from .ai_model_service import AIModelService
from .acceleration.service import AccelerationService
from .audio_library import AudioLibrary
from .database import ModelRepository
from .model_service import ModelService
from .manuscripts import ManuscriptService
from .models import BUILTIN_MODELS, transcription_engine
from .logging_config import configure_logging
from .paths import get_app_paths
from .providers.apple_speech import AppleSpeechProvider
from .providers.whisper_bundle import WhisperBundleProvider
from .transcription.service import TranscriptionService


def main() -> None:
    multiprocessing.freeze_support()
    configure_logging()
    logger = logging.getLogger(__name__)
    paths = get_app_paths()
    logger.info('应用启动 platform=%s arch=%s python=%s environment=%s engine=%s data_dir=%s',
                sys.platform, platform.machine(), platform.python_version(), paths.environment,
                transcription_engine(), paths.data_dir)
    repository = ModelRepository(paths.database, BUILTIN_MODELS)
    audio_library = AudioLibrary(paths.database, paths.audio_dir)
    whisper_provider = WhisperBundleProvider(paths.models_dir, paths.downloads_dir)
    apple_provider = AppleSpeechProvider()
    model_service = ModelService(repository, {
        "apple-speech": apple_provider,
        "whisper": whisper_provider,
    }, BUILTIN_MODELS)
    ai_model_service = AIModelService(paths.database)
    acceleration = AccelerationService(paths)
    transcription = TranscriptionService(paths, whisper_provider, BUILTIN_MODELS, acceleration,
                                         apple_provider=apple_provider, audio_library=audio_library)
    manuscripts = ManuscriptService(paths.database, ai_model_service)
    web_root = Path(__file__).with_name("web")
    icon_path = Path(__file__).resolve().parents[2] / "assets" / "Sona.icns"
    try:
        webview.create_window(
            "Sona", str(web_root / "index.html"),
            width=960, height=640, min_size=(720, 480),
            js_api=AppApi(model_service, audio_library, transcription, acceleration, ai_model_service, manuscripts),
        )
        # pywebview's Windows backend requires an .ico file; passing the macOS
        # .icns asset makes System.Drawing fail before the window is shown.
        start_options: dict[str, object] = {"http_server": True}
        if sys.platform == "darwin" and icon_path.is_file():
            start_options["icon"] = str(icon_path)
        webview.start(**start_options)
    finally:
        logger.info('应用关闭，正在停止后台任务')
        manuscripts.close()
        try:
            transcription.close()
        finally:
            try:
                acceleration.close()
            finally:
                try:
                    audio_library.close()
                finally:
                    model_service.close()
        logger.info('后台任务停止请求已发送，应用退出')
