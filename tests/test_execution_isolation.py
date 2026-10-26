"""Non-regression guard: the execution layer stays decoupled and lightweight.

The intraday execution package is the whole reason classic behaviour is claimed
untouched: it must not import the classic flow (`agents`/`dataflows`) nor pull
the heavy data/LLM stack. These tests fail loudly if a future change couples
the fast path to the framework — catching the regression at test time rather
than in a bloated container or a surprised classic run.

The check is static (scans the execution sources' import lines) rather than
sys.modules-based: the repo's autouse `_isolate_config` fixture imports
`dataflows.config` for every test, so a runtime sys.modules probe would measure
that process-global pollution, not the execution layer itself.
"""
from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

# Top-level module names the execution layer must never import (directly or via
# a within-repo module). `langchain_core` is intentionally NOT here: the package
# __init__ preloads it when present, independently of the execution layer.
_FORBIDDEN = {
    "pandas", "numpy", "yfinance", "langgraph", "langchain",
    "stockstats", "backtrader", "requests", "redis",
}
# Within-repo subpackages the fast path must stay decoupled from.
_FORBIDDEN_LOCAL = {"agents", "dataflows", "graph", "llm_clients", "cli"}


def _exec_sources() -> list[pathlib.Path]:
    pkg = pathlib.Path(importlib.import_module("tradingagents.execution").__file__).parent
    return sorted(pkg.glob("*.py"))


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(("." * node.level) + node.module)
    return names


@pytest.mark.unit
class TestImportIsolation:
    def test_no_heavy_or_classic_imports(self):
        offenders: list[str] = []
        for src in _exec_sources():
            for name in _imported_names(ast.parse(src.read_text(), filename=str(src))):
                top = name.lstrip(".").split(".")[0]
                if top in _FORBIDDEN:
                    offenders.append(f"{src.name}: import {name}")
                # within-repo coupling: `from tradingagents.<X>` or `from .<X>`
                parts = name.lstrip(".").split(".")
                if parts[:1] == ["tradingagents"] and len(parts) > 1 and parts[1] in _FORBIDDEN_LOCAL:
                    offenders.append(f"{src.name}: import {name}")
        assert not offenders, f"execution layer coupled to forbidden modules: {offenders}"

    def test_no_dangerous_calls(self):
        """The fast path runs over market data and broker state — keep it free
        of eval/exec/pickle/subprocess/os.system."""
        banned = ("eval(", "exec(", "os.system", "subprocess", "pickle", "__import__(")
        offenders = [
            f"{src.name}: {token}"
            for src in _exec_sources()
            for token in banned
            if token in src.read_text()
        ]
        assert not offenders, f"dangerous calls in execution layer: {offenders}"

