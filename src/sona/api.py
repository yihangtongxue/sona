from .model_service import ModelService


class AppApi:
    """Methods exposed to the desktop webview."""

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

    def pause_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.pause(model_id)

    def select_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.start(model_id, "select")

    def delete_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.start(model_id, "delete")
