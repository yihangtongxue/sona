from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, replace

from .database import ModelRepository
from .models import ModelDefinition, ModelEvent, ModelProvider


PROGRESS_SAVE_INTERVAL = 3.0
SHUTDOWN_TIMEOUT = 11.0


class ModelService:
    def __init__(
        self, repository: ModelRepository, providers: dict[str, ModelProvider],
        models: Sequence[ModelDefinition],
    ) -> None:
        self._repository = repository
        self._providers = providers
        self._models = {model.id: model for model in models}
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, object]] = {}
        self._workers: set[threading.Thread] = set()
        self._closed = False

    def list_models(self) -> list[dict[str, object]]:
        # Persisted installation records are a cache, never proof of availability.
        with self._lock:
            records = self._repository.list_models()
            result = []
            for record in records:
                model_id = record["id"]
                if model_id not in self._models:
                    continue
                model = self._models[model_id]
                job = self._jobs.get(model_id, {
                    "status": "unchecked", "detail": "尚未确认本机资源状态。",
                    "progress": None, "error": "", "active": False, "action": None,
                }).copy()
                if job.get("active"):
                    job["elapsed"] = int(time.monotonic() - job["started_at"])
                job.pop("started_at", None)
                result.append({**record, "description": model.description, **job})
            return result

    def refresh_all(self) -> list[dict[str, object]]:
        for model_id in self._models:
            self._start_job(model_id, "status")
        return self.list_models()

    def start(self, model_id: str, action: str) -> list[dict[str, object]]:
        self._start_job(model_id, action)
        return self.list_models()

    def _start_job(self, model_id: str, action: str) -> None:
        if model_id not in self._models:
            raise ValueError("模型不存在。")
        if action not in {"status", "download", "select", "delete"}:
            raise ValueError("不支持的模型操作。")
        with self._lock:
            if self._closed:
                raise RuntimeError("应用正在关闭。")
            current = self._jobs.get(model_id)
            if action == "select" and any(
                job["active"] and job["action"] == "select"
                and self._models[other_id].kind == self._models[model_id].kind
                for other_id, job in self._jobs.items()
            ):
                return
            if not current or not current["active"]:
                self._jobs[model_id] = {
                    **{key: current[key] for key in (
                        "downloaded_bytes", "total_bytes", "artifact_version", "has_files",
                    ) if current and key in current},
                    "status": "starting", "detail": "正在启动模型任务。",
                    "active": True, "action": action, "started_at": time.monotonic(),
                    "progress": None, "error": "",
                    "cancelling": False,
                }
                worker = threading.Thread(target=self._run, args=(model_id, action), daemon=True)
                self._workers.add(worker)
                try:
                    worker.start()
                except Exception:
                    self._workers.discard(worker)
                    self._jobs[model_id].update(active=False, status="unknown", detail="无法启动模型任务。")
                    raise

    def pause(self, model_id: str) -> list[dict[str, object]]:
        if model_id not in self._models:
            raise ValueError("模型不存在。")
        model = self._models[model_id]
        if model.storage != "managed":
            raise ValueError("该模型由系统管理，无法在应用内暂停。")
        with self._lock:
            if self._closed:
                raise RuntimeError("应用正在关闭。")
            job = self._jobs.get(model_id)
            if job and job["active"] and job["action"] == "download":
                self._providers[model.provider].cancel(model_id)
                job["cancelling"] = True
        return self.list_models()

    def _run(self, model_id: str, action: str) -> None:
        model = self._models[model_id]
        last_saved_at = 0.0
        last_saved_status: str | None = None

        def emit(event: ModelEvent) -> None:
            nonlocal last_saved_at, last_saved_status
            values = asdict(event)
            # None means that a provider has no new metadata for this event.
            for key in ("downloaded_bytes", "total_bytes", "artifact_version", "has_files"):
                if values[key] is None:
                    del values[key]
            with self._lock:
                self._jobs[model_id].update(values)
                if self._jobs[model_id].get("cancelling"):
                    # A pause can arrive before the provider creates its task.
                    self._providers[model.provider].cancel(model_id)
            now = time.monotonic()
            if event.status in {"preparing", "downloading", "verifying"} and (
                event.status != last_saved_status or now - last_saved_at >= PROGRESS_SAVE_INTERVAL
            ):
                self._repository.save_result(model, event, select=False)
                last_saved_at, last_saved_status = now, event.status

        try:
            try:
                provider = self._providers[model.provider]
                provider_action = "download" if action == "download" else "delete" if action == "delete" else "status"
                event = provider.run(provider_action, model, emit)
            except Exception as error:
                event = ModelEvent("unknown", "无法确认模型资源状态。", error=f"{type(error).__name__}: {error}")
            with self._lock:
                selected = not self._closed and action == "select" and event.status == "installed"
                # Commit selection and deletion metadata atomically. Serialize with
                # list_models so a final job cannot be paired with an old selection.
                self._repository.save_result(
                    model, event, select=selected,
                    clear_selection=action == "delete" and event.status == "supported",
                )
            if selected:
                event = replace(event, detail="已设为默认音频转文字模型。")
            elif action == "select" and event.status == "supported":
                event = ModelEvent("supported", "模型尚未安装，请先下载。")
            emit(event)
        except Exception as error:
            emit(ModelEvent("unknown", "无法保存模型记录，请重新检查。", error=f"{type(error).__name__}: {error}"))
        finally:
            with self._lock:
                job = self._jobs[model_id]
                job["elapsed"] = int(time.monotonic() - job["started_at"])
                job["active"] = False
                job["cancelling"] = False
                self._workers.discard(threading.current_thread())

    def close(self) -> None:
        with self._lock:
            self._closed = True
            workers = tuple(self._workers)
        for provider in self._providers.values():
            provider.close()
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
