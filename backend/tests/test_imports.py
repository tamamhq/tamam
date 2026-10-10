"""Every `from tamam… import name` in the package and in scripts/ resolves. Scripts have no
tests of their own, so a moved module or a renamed name would otherwise surface only in a
paid eval run."""

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_tamam_import_resolves():
    bad = []
    for path in [*(ROOT / "scripts").glob("*.py"), *(ROOT / "tamam").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tamam")):
                continue
            try:
                mod = importlib.import_module(node.module)
            except ImportError as exc:
                if "browser_use" in str(exc) or "playwright" in str(exc):
                    continue        # the optional web plugin's own dependencies
                bad.append(f"{path.relative_to(ROOT)}: {node.module} ({exc})")
                continue
            for alias in node.names:
                if alias.name != "*" and not hasattr(mod, alias.name):
                    try:
                        importlib.import_module(f"{node.module}.{alias.name}")
                    except ImportError:
                        bad.append(f"{path.relative_to(ROOT)}: {node.module}.{alias.name}")
    assert bad == []


def test_the_execution_contract_rejects_unknown_fields():
    import pytest
    from pydantic import ValidationError

    from tamam.executors.base import ExecutionSpec
    with pytest.raises(ValidationError):
        ExecutionSpec(instructions="x", app_build_ref="old.app")   # the pre-target spelling
