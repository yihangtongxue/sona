from pathlib import Path

import webview

from .database import ModelRepository
from .model_service import ModelService
from .models import BUILTIN_MODELS
from .paths import get_app_paths
from .speech_assets import AppleSpeechProvider


class AppApi:
    def __init__(self, model_service: ModelService) -> None:
        self._model_service = model_service

    def list_models(self) -> list[dict[str, object]]:
        return self._model_service.list_models()

    def refresh_models(self) -> list[dict[str, object]]:
        return self._model_service.refresh_all()

    def refresh_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.start(model_id, "status")

    def download_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.start(model_id, "download")

    def select_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.start(model_id, "select")


def main() -> None:
    paths = get_app_paths()
    repository = ModelRepository(paths.database, BUILTIN_MODELS)
    model_service = ModelService(repository, {"apple-speech": AppleSpeechProvider()})
    web_root = Path(__file__).with_name("web")
    icon_path = Path(__file__).resolve().parents[2] / "assets" / "Sona.icns"
    webview.create_window(
        "Sona",
        str(web_root / "index.html"),
        width=960,
        height=640,
        min_size=(960, 640),
        js_api=AppApi(model_service),
    )
    try:
        webview.start(http_server=True, icon=str(icon_path))
    finally:
        model_service.close()
