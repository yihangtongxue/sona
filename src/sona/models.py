from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelDefinition:
    id: str
    provider: str
    name: str
    kind: str
    locale: str
    storage: str


BUILTIN_MODELS = (
    ModelDefinition(
        id="apple-speech-zh-cn", provider="apple-speech", name="Apple Speech",
        kind="speech", locale="zh-CN", storage="system",
    ),
)


@dataclass(frozen=True)
class ModelEvent:
    status: str
    detail: str
    progress: float | None = None
    error: str = ""
    resource_path: str | None = None


class ModelProvider(Protocol):
    def run(
        self, action: str, model: ModelDefinition, emit: Callable[[ModelEvent], None],
    ) -> ModelEvent: ...

    def close(self) -> None: ...
