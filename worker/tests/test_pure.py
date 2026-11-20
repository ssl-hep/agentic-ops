"""The pure-logic modules import only the standard library and each other.

Workflow code imports them, so they must stay deterministic and sandbox-safe, and they must not
pull in temporalio: they are the logic, tested with no server.
"""

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "agentic_ops_worker"
PURE = ["names", "contracts", "units", "planning", "failures", "signatures", "records"]
# Nothing that reads the clock, the environment, randomness or files
FORBIDDEN = {"time", "random", "os", "pathlib", "socket", "subprocess", "uuid", "secrets", "io", "shutil"}


@pytest.mark.parametrize("module", PURE)
def test_imports(module: str) -> None:
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for name in imported:
        top = name.split(".")[0]
        if top == "agentic_ops_worker":
            assert name.split(".")[1] in PURE, f"{module} imports {name}"
        else:
            assert top in sys.stdlib_module_names, f"{module} imports {name}, not in the standard library"
            assert top not in FORBIDDEN, f"{module} imports {name}"
