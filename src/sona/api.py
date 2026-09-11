from .audio_library import AudioLibrary
from .model_service import ModelService


class AppApi:
    """Methods exposed to the desktop webview."""

    def __init__(self, model_service: ModelService, audio_library: AudioLibrary) -> None:
        self._model_service = model_service
        self._audio_library = audio_library

    def list_audio(self) -> list[dict]:
        return self._audio_library.list_files()

    def begin_audio_import(self, name: str, size: int) -> str:
        return self._audio_library.begin_import(name, size)

    def append_audio_chunk(self, identifier: str, offset: int, data: str) -> None:
        self._audio_library.append_chunk(identifier, offset, data)

    def finish_audio_import(self, identifier: str) -> dict:
        return self._audio_library.finish_import(identifier)

    def abort_audio_import(self, identifier: str) -> None:
        self._audio_library.abort_import(identifier)

    def open_audio(self, identifier: str) -> None:
        self._audio_library.open_file(identifier)

    def delete_audio(self, identifier: str) -> None:
        self._audio_library.delete_file(identifier)

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
