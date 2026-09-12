import logging
import time
from functools import wraps
from inspect import signature

from .audio_library import AudioLibrary
from .ai_model_service import AIModelService
from .model_service import ModelService
from .activity import ActivityGate


logger = logging.getLogger(__name__)


def log_api_call(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        started = time.monotonic()
        method = function.__name__
        quiet = method == 'append_audio_chunk'
        # Never log upload bytes, file display names, or returned transcript text.
        reference = args[0] if args and method != 'begin_audio_import' else '-'
        if not quiet:
            logger.info('调用 %s ref=%s', method, reference)
        try:
            with self._activity.operation():
                result = function(self, *args, **kwargs)
        except Exception:
            logger.exception('调用失败 %s ref=%s elapsed=%.2fs', method, reference, time.monotonic() - started)
            raise
        if not quiet:
            if method == 'begin_audio_import':
                reference = result
            logger.info('调用完成 %s ref=%s elapsed=%.2fs', method, reference, time.monotonic() - started)
        return result
    # pywebview inspects getfullargspec rather than following __wrapped__.
    # Preserve the explicit signature so JavaScript receives the real arguments.
    call.__signature__ = signature(function)
    return call


class AppApi:
    """Methods exposed to the desktop webview."""

    def __init__(self, model_service: ModelService, audio_library: AudioLibrary, transcription,
                 acceleration=None, ai_model_service: AIModelService | None = None,
                 manuscripts=None, updates=None, activity=None, consent=None, diagnostics=None) -> None:
        self._model_service = model_service
        self._audio_library = audio_library
        self._transcription = transcription
        self._acceleration = acceleration
        self._ai_models = ai_model_service
        self._manuscripts = manuscripts
        self._updates = updates
        self._activity = activity or ActivityGate()
        self._consent = consent
        self._diagnostics = diagnostics

    def ai_usage_notice_required(self) -> bool:
        # Resolve setup problems before asking the user to approve a request
        # that cannot run. This does not contact the model endpoint.
        self._ai_models.default_generation_profile()
        return self._consent.required()

    @log_api_call
    def export_diagnostics(self) -> bool:
        return self._diagnostics()

    def update_status(self) -> dict:
        return self._updates.status()

    def check_update(self) -> dict:
        return self._updates.check()

    def download_update(self) -> dict:
        return self._updates.download()

    def cancel_update_download(self) -> dict:
        return self._updates.cancel()

    def install_update(self) -> dict:
        return self._updates.install()

    @log_api_call
    def optimize_transcription(self, identifier: str, confirmed: bool = False) -> str:
        self._consent.require(confirmed)
        return self._manuscripts.create(identifier)

    def list_manuscripts(self) -> list[dict]:
        return self._manuscripts.repository.list_all()

    @log_api_call
    def get_manuscript(self, identifier: str) -> dict:
        return self._manuscripts.repository.result(identifier)

    @log_api_call
    def retry_manuscript(self, identifier: str, confirmed: bool = False) -> None:
        self._consent.require(confirmed)
        self._manuscripts.retry(identifier)

    @log_api_call
    def delete_manuscript(self, identifier: str) -> None:
        self._manuscripts.repository.delete(identifier)

    def list_ai_models(self) -> list[dict[str, object]]:
        if self._ai_models is None:
            return []
        return self._ai_models.list_models()

    def get_ai_model_api_key(self, identifier: str) -> str:
        return self._ai_models.get_model_api_key(identifier)

    @log_api_call
    def add_ai_model(self, name: str, provider: str, model_name: str,
                     base_url: str = "", api_key: str = "", config: dict | None = None) -> list[dict[str, object]]:
        return self._ai_models.add_model(name, provider, model_name, base_url, api_key, config or {})

    @log_api_call
    def update_ai_model(self, identifier: str, name: str, provider: str, model_name: str,
                        base_url: str = "", api_key: str = "", config: dict | None = None) -> list[dict[str, object]]:
        return self._ai_models.update_model(identifier, name, provider, model_name,
                                             base_url, api_key, config or {})

    @log_api_call
    def test_ai_model(self, identifier: str) -> list[dict[str, object]]:
        return self._ai_models.test_model(identifier)

    @log_api_call
    def select_ai_model(self, identifier: str) -> list[dict[str, object]]:
        return self._ai_models.select_model(identifier)

    @log_api_call
    def delete_ai_model(self, identifier: str) -> list[dict[str, object]]:
        return self._ai_models.delete_model(identifier)

    def acceleration_status(self) -> dict:
        return self._acceleration.status()

    @log_api_call
    def acceleration_action(self, action: str) -> dict:
        return self._acceleration.action(action)

    @log_api_call
    def cancel_transcription(self, identifier: str) -> None:
        self._transcription.repository.cancel(identifier)

    @log_api_call
    def retry_transcription(self, identifier: str) -> None:
        self._transcription.repository.retry(identifier)

    @log_api_call
    def get_transcription(self, identifier: str) -> dict:
        return self._transcription.repository.result(identifier)

    def list_audio(self) -> list[dict]:
        return self._audio_library.list_files()

    @log_api_call
    def begin_audio_import(self, name: str, size: int) -> str:
        return self._audio_library.begin_import(name, size)

    @log_api_call
    def append_audio_chunk(self, identifier: str, offset: int, data: str) -> None:
        self._audio_library.append_chunk(identifier, offset, data)

    @log_api_call
    def finish_audio_import(self, identifier: str) -> dict:
        return self._audio_library.finish_import(identifier)

    @log_api_call
    def abort_audio_import(self, identifier: str) -> None:
        self._audio_library.abort_import(identifier)

    @log_api_call
    def delete_audio(self, identifier: str) -> None:
        self._audio_library.delete_file(identifier)

    def list_models(self) -> list[dict[str, object]]:
        return self._model_service.list_models()

    @log_api_call
    def refresh_models(self) -> list[dict[str, object]]:
        return self._model_service.refresh_all()

    @log_api_call
    def refresh_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.start(model_id, "status")

    @log_api_call
    def download_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.start(model_id, "download")

    @log_api_call
    def pause_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.pause(model_id)

    @log_api_call
    def select_model(self, model_id: str) -> list[dict[str, object]]:
        model = next((item for item in self._model_service.list_models() if item['id'] == model_id), None)
        if not model or not model.get('can_transcribe'):
            raise ValueError("该模型尚不支持转录，请选择可用的音频转文字模型。")
        return self._model_service.start(model_id, "select")

    @log_api_call
    def delete_model(self, model_id: str) -> list[dict[str, object]]:
        return self._model_service.start(model_id, "delete")
