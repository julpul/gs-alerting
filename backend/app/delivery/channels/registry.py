"""Adapter registry: a new provider is one class decorated with `@register` (see channels/README.md)."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable

from app.delivery.channels.base import ChannelAdapter

_ADAPTERS: dict[str, ChannelAdapter] = {}
_loaded = False
_PLUGIN_PACKAGE = "app.delivery.channels"
_NOT_ADAPTERS = {"base", "registry"}


def register[T: type](cls: T) -> T:
    """Class decorator: instantiate the adapter and make it the handler of `cls.name` (last one wins)."""
    _ADAPTERS[cls.name] = cls()  # type: ignore[attr-defined]
    return cls


def load_adapters() -> None:
    """Import every module of `app.delivery.channels` once so their `@register` decorators run."""
    global _loaded
    if _loaded:
        return
    package = importlib.import_module(_PLUGIN_PACKAGE)
    for info in pkgutil.iter_modules(package.__path__):
        if info.name not in _NOT_ADAPTERS:
            importlib.import_module(f"{_PLUGIN_PACKAGE}.{info.name}")
    _loaded = True


def get_adapter(name: str) -> ChannelAdapter:
    load_adapters()
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise KeyError(f"no channel adapter registered for {name!r}") from None


def adapters() -> dict[str, ChannelAdapter]:
    load_adapters()
    return dict(_ADAPTERS)


def override(name: str, adapter: ChannelAdapter) -> Callable[[], None]:
    """Swap an adapter (tests); returns a function restoring the previous one."""
    load_adapters()
    previous = _ADAPTERS.get(name)
    _ADAPTERS[name] = adapter

    def restore() -> None:
        if previous is None:
            _ADAPTERS.pop(name, None)
        else:
            _ADAPTERS[name] = previous

    return restore
