"""The part registries: built-ins, deliberate replacement, and plugins from entry points."""

import pytest

from tamam import plugins
from tamam.plugins import Registry


def test_register_create_and_metadata():
    reg = Registry("widgets")
    reg._plugins_loaded = True
    reg.register("a", lambda n=1: ("a", n), color="red")
    assert reg.create("a", n=2) == ("a", 2)
    assert reg.info("a", "color") == "red" and reg.info("a", "size", 3) == 3
    assert "a" in reg and reg.names() == ["a"]
    with pytest.raises(ValueError, match="already registered"):
        reg.register("a", object)
    with pytest.raises(ValueError, match="Unknown widgets 'b'"):
        reg.create("b")


def test_a_part_is_replaced_only_on_purpose():
    reg = Registry("widgets")
    reg._plugins_loaded = True
    reg.register("ocr", lambda: "built-in")
    reg.register("ocr", lambda: "mine", replace=True)
    assert reg.create("ocr") == "mine"


def test_entry_points_add_parts_and_a_broken_one_is_skipped(monkeypatch):
    class EP:
        def __init__(self, name, fn):
            self.name, self._fn = name, fn

        def load(self):
            return self._fn

    def good(registry):
        registry.register("plug", lambda: "from a plugin", platform="android")

    def broken(registry):
        raise RuntimeError("bad plugin")

    monkeypatch.setattr(plugins, "entry_points",
                        lambda group: [EP("bad", broken), EP("good", good)] if group == "tamam.widgets" else [])
    reg = Registry("widgets")
    reg.register("built-in", lambda: "core")
    assert reg.names() == ["built-in", "plug"]
    assert reg.create("plug") == "from a plugin" and reg.info("plug", "platform") == "android"
