import pytest

from ayce.adapters import Adapter, AdapterError, AdapterHealth, AdapterRegistry
from ayce.config import Config


class DummyAdapter(Adapter):
    name = "dummy"

    def health(self) -> AdapterHealth:
        return AdapterHealth(name=self.name, available=True, detail="ok")


class OtherAdapter(DummyAdapter):
    name = "other"


def test_register_and_create():
    registry = AdapterRegistry()
    registry.register(DummyAdapter)
    assert registry.names() == ["dummy"]
    adapter = registry.create("dummy", Config.from_env(env={}))
    assert isinstance(adapter, DummyAdapter)
    assert adapter.health().available is True


def test_duplicate_registration_rejected():
    registry = AdapterRegistry()
    registry.register(DummyAdapter)
    with pytest.raises(AdapterError, match="already registered"):
        registry.register(DummyAdapter)


def test_unknown_adapter_rejected():
    registry = AdapterRegistry()
    with pytest.raises(AdapterError, match="unknown adapter"):
        registry.create("nonexistent", Config.from_env(env={}))


def test_non_adapter_rejected():
    with pytest.raises(AdapterError, match="not an Adapter subclass"):
        AdapterRegistry().register(dict)


def test_adapter_without_name_rejected():
    class Nameless(Adapter):
        def health(self):
            return AdapterHealth(name="", available=False)

    with pytest.raises(AdapterError, match="name"):
        AdapterRegistry().register(Nameless)
