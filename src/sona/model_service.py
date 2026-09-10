from __future__ import annotations

import threading
import time
from dataclasses import asdict

from .database import ModelRepository
from .models import BUILTIN_MODELS, ModelEvent, ModelProvider


class ModelService:
    def __init__(self, repository: ModelRepository, providers: dict[str, ModelProvider]) -> None:
        self._repository = repository
        self._providers = providers
        self._models = {model.id: model for model in BUILTIN_MODELS}
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
                job = self._jobs.get(model_id, {
                    "status": "unchecked", "detail": "尚未确认本机资源状态。",
                    "progress": None, "error": "", "active": False, "action": None,
                }).copy()
                if job.get("active"):
                    job["elapsed"] = int(time.monotonic() - job["started_at"])
                job.pop("started_at", None)
                result.append({**record, **job})
            return result

    def refresh_all(self) -> list[dict[str, object]]:
        for model_id in self._models:
            self.start(model_id, "status")
        return self.list_models()

    def start(self, model_id: str, action: str) -> list[dict[str, object]]:
        if model_id not in self._models:
            raise ValueError("模型不存在。")
        if action not in {"status", "download", "select"}:
            raise ValueError("不支持的模型操作。")
        with self._lock:
            if self._closed:
                raise RuntimeError("应用正在关闭。")
            current = self._jobs.get(model_id)
            if not current or not current["active"]:
                self._jobs[model_id] = {
                    "status": "starting", "detail": "正在启动语音资源工具。",
                    "active": True, "action": action, "started_at": time.monotonic(),
                    "progress": None, "error": "",
                }
                worker = threading.Thread(target=self._run, args=(model_id, action), daemon=True)
                self._workers.add(worker)
                try:
                    worker.start()
                except Exception:
                    self._workers.discard(worker)
                    self._jobs[model_id].update(active=False, status="unknown", detail="无法启动模型任务。")
                    raise
        return self.list_models()

    def _run(self, model_id: str, action: str) -> None:
        model = self._models[model_id]

        def emit(event: ModelEvent) -> None:
            with self._lock:
                self._jobs[model_id].update(asdict(event))

        try:
            try:
                provider = self._providers[model.provider]
                event = provider.run("download" if action == "download" else "status", model, emit)
            except Exception as error:
                event = ModelEvent("unknown", "无法确认模型资源状态。", error=f"{type(error).__name__}: {error}")
            with self._lock:
                if self._closed:
                    return
            # Selection is committed only after a fresh provider availability check.
            self._repository.save_result(model, event, select=action == "select" and event.status == "installed")
            if action == "select" and event.status == "installed":
                event = ModelEvent("installed", "已设为当前音频转文字模型。", resource_path=event.resource_path)
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
                self._workers.discard(threading.current_thread())

    def close(self) -> None:
        with self._lock:
            self._closed = True
            workers = tuple(self._workers)
        for provider in self._providers.values():
            provider.close()
        for worker in workers:
            worker.join(timeout=5)
