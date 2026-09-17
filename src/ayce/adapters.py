"""Provider-agnostic adapter convention.

External or local capabilities (research providers, TTS, renderers, ...)
must be integrated through adapters, never hardcoded into stages.

Convention:
- Subclass :class:`Adapter` and give the subclass a unique ``name``.
- Implement :meth:`Adapter.health` truthfully (never fake availability).
- Register the subclass in an :class:`AdapterRegistry`; stages resolve
  capabilities by name via :meth:`AdapterRegistry.create`.

No concrete adapters are implemented at P0 — this module only defines
the convention and the registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Type

from .config import Config


class AdapterError(RuntimeError):
    """Raised for adapter registration or lookup problems."""


@dataclass(frozen=True)
class AdapterHealth:
    """Truthful availability report for an adapter."""

    name: str
    available: bool
    detail: str = ""


class Adapter(ABC):
    """Base class for all capability adapters."""

    #: Unique adapter name (e.g. "ffmpeg-render", "openai-tts").
    name: ClassVar[str] = ""

    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def health(self) -> AdapterHealth:
        """Report whether this adapter can actually do its job right now."""


class AdapterRegistry:
    """Registry mapping adapter names to adapter classes."""

    def __init__(self) -> None:
        self._adapters: dict[str, Type[Adapter]] = {}

    def register(self, adapter_cls: Type[Adapter]) -> None:
        if not (isinstance(adapter_cls, type) and issubclass(adapter_cls, Adapter)):
            raise AdapterError(f"{adapter_cls!r} is not an Adapter subclass")
        if not adapter_cls.name or not isinstance(adapter_cls.name, str):
            raise AdapterError(f"{adapter_cls.__name__} must define a non-empty string `name`")
        if adapter_cls.name in self._adapters:
            raise AdapterError(f"adapter {adapter_cls.name!r} is already registered")
        self._adapters[adapter_cls.name] = adapter_cls

    def create(self, name: str, config: Config) -> Adapter:
        cls = self._adapters.get(name)
        if cls is None:
            known = ", ".join(sorted(self._adapters)) or "(none registered)"
            raise AdapterError(f"unknown adapter {name!r}; registered adapters: {known}")
        return cls(config)

    def names(self) -> list[str]:
        return sorted(self._adapters)
