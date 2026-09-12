import sys
import multiprocessing
import logging
import platform
from pathlib import Path
from contextlib import nullcontext

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
from .activity import ActivityGate
from .file_lock import FileLocked, exclusive_file_lock
from .updates.service import UpdateService
from .consent import AIUsageConsent
from .diagnostics import export_bundle
from .podcasts.service import PodcastService
from .localization import WEBVIEW_ZH
from .window_chrome import configure_window_chrome
from .appearance import AppearanceSettings


def main() -> None:
    multiprocessing.freeze_support()
    configure_logging()
    paths = get_app_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        guard = exclusive_file_lock(paths.data_dir / '.application.lock') if getattr(sys, 'frozen', False) else nullcontext()
        with guard:
            _run_app(paths)
    except FileLocked:
        logging.getLogger(__name__).info('应用已打开或正在更新，请稍后重试。')


def _run_app(paths) -> None:
    logger = logging.getLogger(__name__)
    logger.info('应用启动 platform=%s arch=%s python=%s environment=%s engine=%s data_dir=%s',
                sys.platform, platform.machine(), platform.python_version(), paths.environment,
                transcription_engine(), paths.data_dir)
    repository = ModelRepository(paths.database, BUILTIN_MODELS)
    appearance = AppearanceSettings(paths.data_dir)
    audio_library = AudioLibrary(paths.database, paths.audio_dir)
    whisper_provider = WhisperBundleProvider(paths.models_dir, paths.downloads_dir)
    apple_provider = AppleSpeechProvider()
    model_service = ModelService(repository, {
        "apple-speech": apple_provider,
        "whisper": whisper_provider,
    }, BUILTIN_MODELS)
    ai_model_service = AIModelService(paths.database)
    acceleration = AccelerationService(paths)
    activity = ActivityGate()
    transcription = TranscriptionService(paths, whisper_provider, BUILTIN_MODELS, acceleration,
                                         apple_provider=apple_provider, audio_library=audio_library, activity=activity)
    manuscripts = ManuscriptService(paths.database, ai_model_service, activity=activity)
    podcasts = PodcastService(paths, audio_library, activity)
    updates = UpdateService(paths, activity, other_busy=lambda: audio_library.is_importing()
                            or any(record.get('active') for record in model_service.list_models()))

    def export_diagnostics():
        selected = window.create_file_dialog(webview.FileDialog.SAVE,
            save_filename="Sona-diagnostics.zip", file_types=("ZIP 文件 (*.zip)",))
        if not selected:
            return False
        destination = Path(selected if isinstance(selected, str) else selected[0])
        if destination.suffix.lower() != ".zip":
            raise ValueError("请使用 .zip 作为日志文件的后缀。")
        export_bundle(paths.data_dir / "logs", destination)
        return True

    web_root = Path(__file__).with_name("web")
    icon_path = (Path(__file__).with_name("assets") / "Sona.icns" if getattr(sys, "frozen", False)
                 else Path(__file__).resolve().parents[2] / "assets" / "Sona.icns")
    try:
        window = webview.create_window(
            "Sona", str(web_root / "index.html"),
            width=960, height=640, min_size=(720, 480),
            background_color="#141414" if appearance.get_theme() == "dark" else "#f9f9f9",
            js_api=AppApi(model_service, audio_library, transcription, acceleration, ai_model_service,
                          manuscripts, updates, activity, consent=AIUsageConsent(paths.data_dir),
                          diagnostics=export_diagnostics, podcasts=podcasts, appearance=appearance),
        )
        configure_window_chrome(window, appearance)
        updates.bind_window(window.destroy)
        updates.start_automatic_checks()
        # pywebview's Windows backend requires an .ico file; passing the macOS
        # .icns asset makes System.Drawing fail before the window is shown.
        start_options: dict[str, object] = {"http_server": True, "localization": WEBVIEW_ZH}
        if sys.platform == "win32":
            start_options["gui"] = "edgechromium"
        if sys.platform == "darwin" and icon_path.is_file():
            start_options["icon"] = str(icon_path)
        webview.start(**start_options)
    finally:
        logger.info('应用关闭，正在停止后台任务')
        updates.close()
        podcasts.close()
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
