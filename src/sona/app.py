import sys
import multiprocessing
import logging
import platform
from pathlib import Path
from contextlib import nullcontext

import webview

from .api import AppApi
from .ai_model_service import AIModelService
from .audio_library import AudioLibrary
from .database import ModelRepository
from .model_service import ModelService
from .manuscripts import ManuscriptService
from .manuscript_export import FORMAT_LABELS, FORMATS, export_txt, manuscript_filename, render, write_export
from .codex_config import codex_config
from .mcp_server import MCPService
from .mcp_tools import SonaTools
from .search import search as search_index
from .transcription_settings import TranscriptionSettings
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
from .window_lifecycle import ServiceShutdown, WindowLifecycle


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
    transcription_settings = TranscriptionSettings(paths.data_dir)
    audio_library = AudioLibrary(paths.database, paths.audio_dir)
    whisper_provider = WhisperBundleProvider(paths.models_dir, paths.downloads_dir)
    apple_provider = AppleSpeechProvider()
    model_service = ModelService(repository, {
        "apple-speech": apple_provider,
        "whisper": whisper_provider,
    }, BUILTIN_MODELS)
    ai_model_service = AIModelService(paths.database)
    activity = ActivityGate()
    transcription = TranscriptionService(paths, whisper_provider, BUILTIN_MODELS,
                                         apple_provider=apple_provider, audio_library=audio_library, activity=activity,
                                         language_settings=transcription_settings)
    manuscripts = ManuscriptService(paths.database, ai_model_service, activity=activity)
    podcasts = PodcastService(paths, audio_library, activity)
    updates = UpdateService(paths, activity, other_busy=lambda: audio_library.is_importing()
                            or any(record.get('active') for record in model_service.list_models()))
    tools = SonaTools(paths, audio_library, podcasts, transcription, activity)
    mcp = MCPService(paths.data_dir, tools)
    mcp.start()
    shutdown = ServiceShutdown(mcp, updates, podcasts, manuscripts, transcription,
                               audio_library, model_service)

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

    def save_export(document, export_format):
        title = document.get('title') or document.get('name') or '文稿'
        suffix = FORMATS[export_format]
        selected = window.create_file_dialog(webview.FileDialog.SAVE,
            save_filename=manuscript_filename(title, export_format),
            file_types=(f"{FORMAT_LABELS[export_format]} (*{suffix})",))
        if not selected:
            return False
        destination = Path(selected if isinstance(selected, str) else selected[0])
        if export_format == 'txt' and 'body' in document:
            export_txt(document, destination)
        else:
            write_export(render(document, export_format), destination, export_format)
        return True

    def export_manuscript(manuscript, export_format='txt'):
        return save_export(manuscript, export_format)

    def export_transcription(result, export_format='txt'):
        if export_format == 'txt':
            return save_export({'title': result['name'], 'body': result['text']}, 'txt')
        return save_export({**result, 'title': result['name']}, export_format)

    web_root = Path(__file__).with_name("web")
    icon_path = (Path(__file__).with_name("assets") / "Sona.icns" if getattr(sys, "frozen", False)
                 else Path(__file__).resolve().parents[2] / "assets" / "Sona.icns")
    try:
        window = webview.create_window(
            "Sona", str(web_root / "index.html"),
            width=960, height=640, min_size=(720, 480),
            background_color="#141414" if appearance.get_theme() == "dark" else "#f9f9f9",
            js_api=AppApi(model_service, audio_library, transcription, ai_model_service,
                          manuscripts, updates, activity, consent=AIUsageConsent(paths.data_dir),
                          diagnostics=export_diagnostics, podcasts=podcasts, appearance=appearance,
                          manuscript_export=export_manuscript, transcription_export=export_transcription,
                          transcription_settings=transcription_settings, mcp=mcp, codex_config=codex_config,
                          search=lambda query: search_index(paths.database, query, 30)),
        )
        configure_window_chrome(window, appearance)
        lifecycle = WindowLifecycle(window, shutdown)
        if sys.platform == "darwin":
            def install_menu_bar():
                try:
                    from .menu_bar import MenuBar

                    def open_window():
                        if lifecycle.native is not None:
                            lifecycle.native.restore()
                        else:
                            window.show()

                    lifecycle.menu_bar = MenuBar(mcp=mcp, tools=tools, codex_config=codex_config,
                                                 open_window=open_window, quit_app=lifecycle.request_quit)
                except Exception:
                    logging.getLogger(__name__).exception("无法创建菜单栏图标")
            window.events.before_show += install_menu_bar
        updates.bind_window(lifecycle.request_quit)
        updates.start_automatic_checks()
        start_options: dict[str, object] = {"http_server": True, "localization": WEBVIEW_ZH}
        if sys.platform == "darwin" and icon_path.is_file():
            start_options["icon"] = str(icon_path)
        webview.start(**start_options)
    finally:
        shutdown()
