"""Pinned CTranslate2/MLX model bundles, using the resumable HTTP transport."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
from dataclasses import replace
from pathlib import Path

from ..models import ModelEvent, engine_supported, transcription_engine
from .whisper import (
    CHUNK_SIZE, MANIFEST_NAME, WhisperProvider, _DownloadCancelled,
    _ModelLocked, _read_manifest, _remove_directory,
)


class WhisperBundleProvider(WhisperProvider):
    def run(self, action, model, emit):
        if not model.bundle or not engine_supported():
            return ModelEvent("unsupported", "这台电脑暂不支持此模型。")
        return super().run(action, model, emit)

    def _target_dir(self, model):
        return self._models_dir / transcription_engine() / model.id

    def _partial_dir(self, model):
        return self._downloads_dir / transcription_engine() / f"{model.id}.{model.bundle['revision']}.partial"

    def _legacy_paths(self, model):
        return (self._models_dir / model.id, self._downloads_dir / f"{model.id}.partial")

    def _has_files(self, model):
        return any(path.exists() for path in (
            self._target_dir(model), self._partial_dir(model), *self._legacy_paths(model),
        ))

    def _partial_size(self, model):
        total = 0
        for entry in model.bundle["files"]:
            path = self._partial_dir(model) / entry["name"]
            try:
                total += min(path.stat().st_size, entry["size"])
            except FileNotFoundError:
                pass
        return total

    def _version(self, model):
        return f"{transcription_engine()}:{model.bundle['revision']}"

    def _status(self, model):
        target = self._target_dir(model)
        manifest = _read_manifest(target / MANIFEST_NAME)
        total = sum(entry["size"] for entry in model.bundle["files"])
        valid = manifest == self._manifest(model)
        if valid:
            for entry in model.bundle["files"]:
                try:
                    if (target / entry["name"]).stat().st_size != entry["size"]:
                        valid = False
                        break
                except FileNotFoundError:
                    valid = False
                    break
        if valid:
            return ModelEvent("installed", "模型已安装。", resource_path=str(target),
                              downloaded_bytes=total, total_bytes=total,
                              artifact_version=self._version(model), has_files=True)
        size = self._partial_size(model)
        # Download/resume buttons already explain the next step. Legacy file
        # formats are an implementation detail, not a decision for the user.
        detail = "下载不完整，请重新下载。" if target.exists() else ""
        return ModelEvent("supported", detail, downloaded_bytes=size, total_bytes=total,
                          artifact_version=self._version(model), has_files=self._has_files(model))

    def _manifest(self, model):
        return {"schema_version": 2, "model_id": model.id,
                "engine": transcription_engine(), **model.bundle}

    def _matches(self, path, entry, cancelled):
        if not path.is_file() or path.stat().st_size != entry["size"]:
            return False
        digest = hashlib.sha256() if entry["sha256"] else hashlib.sha1()
        if not entry["sha256"]:
            digest.update(f"blob {entry['size']}\0".encode("ascii"))
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                self._check_cancelled(cancelled)
                digest.update(chunk)
        return digest.hexdigest() == (entry["sha256"] or entry["git_sha1"])

    @staticmethod
    def _remove_within(path: Path, root: Path):
        resolved = path.resolve()
        if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
            raise ValueError("模型目录超出应用数据范围。")
        _remove_directory(path)

    def _download(self, model, emit, cancelled):
        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._model_lock(model):
                self._check_cancelled(cancelled)
                current = self._status(model)
                if current.status == "installed":
                    return current
                partial = self._partial_dir(model)
                partial.mkdir(parents=True, exist_ok=True)
                total = sum(entry["size"] for entry in model.bundle["files"])
                emit(ModelEvent("preparing", "正在准备模型下载。", total_bytes=total,
                                artifact_version=self._version(model)))
                for entry in model.bundle["files"]:
                    self._check_cancelled(cancelled)
                    path = partial / entry["name"]
                    emit(ModelEvent("verifying", "正在检查已下载的资源。",
                                    downloaded_bytes=self._partial_size(model), total_bytes=total))
                    if self._matches(path, entry, cancelled):
                        continue
                    if path.exists() and path.stat().st_size >= entry["size"]:
                        path.unlink()
                    artifact = replace(model, artifact_filename=entry["name"],
                        artifact_url=f"https://huggingface.co/{model.bundle['repo']}/resolve/"
                                     f"{model.bundle['revision']}/{entry['name']}")

                    def report(event):
                        downloaded = self._partial_size(model)
                        emit(replace(event, progress=downloaded / total,
                                     downloaded_bytes=downloaded, total_bytes=total,
                                     artifact_version=self._version(model)))

                    try:
                        self._transfer(artifact, path, report, cancelled)
                    except urllib.error.HTTPError as error:
                        if error.code != 416:
                            raise
                        path.unlink(missing_ok=True)
                        self._transfer(artifact, path, report, cancelled)
                    emit(ModelEvent("verifying", "正在校验模型资源。",
                                    downloaded_bytes=self._partial_size(model), total_bytes=total))
                    if not self._matches(path, entry, cancelled):
                        path.unlink(missing_ok=True)
                        raise ValueError(f"{entry['name']} 校验失败，已移除损坏文件，请重试。")
                self._check_cancelled(cancelled)
                (partial / MANIFEST_NAME).write_text(
                    json.dumps(self._manifest(model), ensure_ascii=False), encoding="utf-8")
                target = self._target_dir(model)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    self._remove_within(target, self._models_dir)
                os.replace(partial, target)
                return self._status(model)
        except _ModelLocked:
            return ModelEvent("locked", "模型正在使用中，请稍后重试。")
        except _DownloadCancelled:
            return self._paused(model)
        except Exception as error:
            if self._closed.is_set() or cancelled.is_set():
                return self._paused(model)
            return ModelEvent("failed", "下载失败，可继续下载。", error=str(error),
                              downloaded_bytes=self._partial_size(model),
                              artifact_version=self._version(model), has_files=self._has_files(model))

    def _paused(self, model):
        return replace(self._status(model), status="paused", detail="下载已暂停，可继续下载。")

    def _delete(self, model):
        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._model_lock(model):
                paths = [(self._target_dir(model), self._models_dir),
                         (self._partial_dir(model), self._downloads_dir),
                         (self._legacy_paths(model)[0], self._models_dir),
                         (self._legacy_paths(model)[1], self._downloads_dir)]
                # Validate every target before removing any directory.
                for path, root in paths:
                    if path.resolve() == root.resolve() or not path.resolve().is_relative_to(root.resolve()):
                        raise ValueError("模型目录超出应用数据范围。")
                for path, root in paths:
                    self._remove_within(path, root)
                return ModelEvent("supported", "模型资源已删除。", downloaded_bytes=0,
                                  total_bytes=0, has_files=False, artifact_version=self._version(model))
        except _ModelLocked:
            return ModelEvent("locked", "模型正在使用中，请稍后重试。")
        except (OSError, ValueError) as error:
            return ModelEvent("failed", "模型资源未能完整删除。", error=str(error),
                              has_files=self._has_files(model))
