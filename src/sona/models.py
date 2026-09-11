from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
import platform
import sys
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ModelDefinition:
    id: str
    provider: str
    name: str
    kind: str
    locale: str
    storage: str
    description: str = ""
    artifact_url: str | None = None
    artifact_sha256: str | None = None
    artifact_filename: str | None = None
    artifact_version: str | None = None
    bundle: dict | None = None

    @property
    def engine(self) -> str | None:
        if self.provider == "apple-speech":
            return "apple-speech"
        if self.provider == "whisper" and self.bundle:
            return transcription_engine()
        return None

    @property
    def can_transcribe(self) -> bool:
        return self.engine is not None


BUILTIN_MODELS = (
    ModelDefinition(
        id="apple-speech-zh-cn", provider="apple-speech", name="Apple Speech",
        kind="speech", locale="zh-CN", storage="system",
        description="Apple 原生中文离线转录，语言资源由系统管理。需要 macOS 26 或更高版本。",
    ),
    ModelDefinition(
        id="whisper-small", provider="whisper", name="Whisper Small",
        kind="speech", locale="multilingual", storage="managed",
        description="较小的多语言模型，适合节省磁盘空间。",
        artifact_url="https://openaipublic.azureedge.net/main/whisper/models/"
        "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt",
        artifact_sha256="9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794",
        artifact_filename="small.pt", artifact_version="small",
    ),
    ModelDefinition(
        id="whisper-medium", provider="whisper", name="Whisper Medium",
        kind="speech", locale="multilingual", storage="managed",
        description="均衡的多语言模型，需要更多内存和磁盘空间。",
        artifact_url="https://openaipublic.azureedge.net/main/whisper/models/"
        "345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1/medium.pt",
        artifact_sha256="345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1",
        artifact_filename="medium.pt", artifact_version="medium",
    ),
    ModelDefinition(
        id="whisper-turbo", provider="whisper", name="Whisper Turbo",
        kind="speech", locale="multilingual", storage="managed",
        description="转录速度优先，支持多种语言。",
        artifact_url="https://openaipublic.azureedge.net/main/whisper/models/"
        "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a/large-v3-turbo.pt",
        artifact_sha256="aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a",
        artifact_filename="large-v3-turbo.pt", artifact_version="large-v3-turbo",
    ),
    ModelDefinition(
        id="whisper-large-v3", provider="whisper", name="Whisper Large V3",
        kind="speech", locale="multilingual", storage="managed",
        description="识别效果优先，支持多种语言。",
        artifact_url="https://openaipublic.azureedge.net/main/whisper/models/"
        "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb/large-v3.pt",
        artifact_sha256="e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb",
        artifact_filename="large-v3.pt", artifact_version="large-v3",
    ),
)


def transcription_engine() -> str:
    return "mlx-whisper" if sys.platform == "darwin" else "faster-whisper"


def engine_supported() -> bool:
    return sys.platform != "darwin" or platform.machine().lower() == "arm64"


# Keep stable model IDs and legacy checkpoint metadata for migration; new
# installations use a pinned, engine-specific bundle with verified file hashes.
_catalog = json.loads(Path(__file__).with_name("model_catalog.json").read_text(encoding="utf-8"))
BUILTIN_MODELS = tuple(
    replace(model, bundle=_catalog[transcription_engine()].get(model.id))
    for model in BUILTIN_MODELS
)


@dataclass(frozen=True)
class ModelEvent:
    status: str
    detail: str
    progress: float | None = None
    error: str = ""
    resource_path: str | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    artifact_version: str | None = None
    has_files: bool | None = None


class ModelProvider(Protocol):
    def run(
        self, action: str, model: ModelDefinition, emit: Callable[[ModelEvent], None],
    ) -> ModelEvent: ...

    def close(self) -> None: ...

    def cancel(self, model_id: str) -> None: ...
