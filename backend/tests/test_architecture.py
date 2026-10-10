"""The layer contract: which part of tamam may import which. Every import counts, including the
ones inside functions, so a lazy import can't hide a dependency. The point is drop-in parts: a
platform, a judge or a device provider can be replaced without touching the layers above it.

    config, plugins, observability            the base
    executors.base, executors.core            the executor contract and shared screen model
    harness                                   the trust layer (verdicts, clauses, guardrails)
    executors.<platform>, graduation          platforms; fast replay (through graduation.platform)
    assembly, evals, runtime, api, cli, …     composition: they wire the parts together
"""

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "tamam"

# layer → packages it must never import
FORBIDDEN = {
    "harness": ["db", "models", "runtime", "api", "graduation", "evals", "assembly", "dispatch",
                "executors.ios", "executors.agent", "executors.registry"],
    "executors.core": ["harness", "db", "models", "runtime", "api", "graduation", "evals",
                       "executors.ios", "executors.agent"],
    "executors.base": ["harness", "db", "models", "runtime", "api", "graduation", "evals"],
    "executors.ios": ["graduation", "db", "models", "runtime", "api", "evals", "assembly",
                      "dispatch", "executors.agent", "executors.registry"],
    "graduation": ["executors.ios", "executors.agent", "runtime", "api", "evals", "assembly"],
    "plugins": ["harness", "executors", "graduation", "runtime", "api", "db", "models"],
}


def _layer(path: Path) -> str | None:
    rel = path.relative_to(PKG).with_suffix("").parts
    if rel[0] == "executors" and len(rel) > 1:
        return f"executors.{rel[1]}"
    return rel[0]


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tamam."):
            out.add(node.module[len("tamam."):])
        elif isinstance(node, ast.Import):
            out.update(a.name[len("tamam."):] for a in node.names if a.name.startswith("tamam."))
    return out


def _violations() -> list[str]:
    bad = []
    for path in PKG.rglob("*.py"):
        layer = _layer(path)
        for banned in FORBIDDEN.get(layer, []):
            for mod in _imports(path):
                if mod == banned or mod.startswith(banned + "."):
                    bad.append(f"{path.relative_to(PKG)} ({layer}) imports tamam.{mod}")
    return sorted(bad)


def test_every_layer_imports_only_what_is_below_it():
    assert _violations() == []


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_each_guarded_layer_still_exists(layer):
    """A renamed package would make its rule vacuous: fail instead."""
    parts = layer.split(".")
    assert (PKG.joinpath(*parts)).exists() or (PKG.joinpath(*parts[:-1], parts[-1] + ".py")).exists()
