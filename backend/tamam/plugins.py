"""Registries for the parts a run is assembled from — the drop-in seam.

Each kind of part (executors, grounders, judges, device providers) has one `Registry`: a name
maps to a factory plus a little metadata the rest of the system may read without building the
part (an executor's platform, whether it supports graduation). Built-ins register at import;
another package adds or replaces a part through the Python entry-point group `tamam.<kind>`,
e.g. in its pyproject:

    [project.entry-points."tamam.grounders"]
    my_ocr = "my_pkg.grounder:register"     # a callable taking the registry

Nothing else changes: whatever reads the registry picks the part up by name from settings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable, Generic, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class Entry(Generic[T]):
    name: str
    factory: Callable[..., T]
    info: dict[str, Any] = field(default_factory=dict)


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._entries: dict[str, Entry[T]] = {}
        self._plugins_loaded = False

    def register(self, name: str, factory: Callable[..., T], *, replace: bool = False,
                 **info: Any) -> None:
        """Add a part. Re-registering a name is an error unless `replace=True` (a deliberate
        drop-in replacement of a built-in)."""
        if name in self._entries and not replace:
            raise ValueError(f"{self.kind}: {name!r} is already registered")
        self._entries[name] = Entry(name, factory, info)

    def entry(self, name: str) -> Entry[T]:
        self._load_plugins()
        try:
            return self._entries[name]
        except KeyError:
            raise ValueError(f"Unknown {self.kind} {name!r}. Registered: {self.names()}") from None

    def create(self, name: str, *args: Any, **kwargs: Any) -> T:
        return self.entry(name).factory(*args, **kwargs)

    def info(self, name: str, key: str, default: Any = None) -> Any:
        return self.entry(name).info.get(key, default)

    def names(self) -> list[str]:
        self._load_plugins()
        return sorted(self._entries)

    def __contains__(self, name: str) -> bool:
        self._load_plugins()
        return name in self._entries

    def _load_plugins(self) -> None:
        if self._plugins_loaded:
            return
        self._plugins_loaded = True
        for ep in entry_points(group=f"tamam.{self.kind}"):
            try:
                ep.load()(self)
            except Exception:  # a broken plugin must not take the built-ins down with it
                logger.exception("plugin %s for tamam.%s failed to load", ep.name, self.kind)
