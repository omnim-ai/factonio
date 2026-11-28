"""Static architecture invariants for Factonio's production modules (AST only; nothing is imported).

Pins the layered module graph from the SOLID refactor: no internal import cycles (top-level, and
top-level plus function-local lazy imports combined), no cross-module ``_private`` imports or
``module._private`` attribute reads, one read-only `kanban.db` opener, and one full-SHA regex.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
_NON_PRODUCTION = {"tests", "tools", "docs", "skills", "desktop", "__pycache__"}


def _production_files() -> dict[str, Path]:
    """``{module name: path}``; the package ``__init__`` is ``""``, `dashboard/plugin_api.py` is dotted."""
    mods: dict[str, Path] = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in _NON_PRODUCTION or any(p.startswith(".") for p in rel.parts):
            continue
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mods[".".join(parts)] = path
    return mods


MODULES = _production_files()


def _package_of(mod: str) -> list[str]:
    is_pkg = MODULES[mod].name == "__init__.py"
    parts = mod.split(".") if mod else []
    return parts if is_pkg else parts[:-1]


def _dynamic_module(node: ast.AST) -> str | None:
    """Target of ``importlib.import_module(f"{_PKG_NAME}.x")`` (the dashboard's loader), else ``None``."""
    if not (isinstance(node, ast.Call) and node.args and getattr(node.func, "attr", None) == "import_module"):
        return None
    arg = node.args[0]
    if isinstance(arg, ast.JoinedStr) and arg.values and isinstance(arg.values[-1], ast.Constant):
        tail = str(arg.values[-1].value)
        if tail.startswith("."):
            return tail[1:] if tail[1:] in MODULES else None
    return None


class _Scan(NamedTuple):
    top: set[str]
    lazy: set[str]
    private: list[str]


def _scan(mod: str) -> _Scan:
    """Internal import edges (``top`` / ``lazy``) and cross-module private name accesses."""
    tree = ast.parse(MODULES[mod].read_text(encoding="utf-8"))
    toplevel: set[int] = set()
    for stmt in tree.body:
        nodes = ast.walk(stmt) if isinstance(stmt, (ast.If, ast.Try)) else [stmt]
        toplevel.update(id(n) for n in nodes)
    top: set[str] = set()
    lazy: set[str] = set()
    private: list[str] = []
    aliases: dict[str, str] = {}

    def add(target: str, node: ast.AST) -> None:
        if target != mod:
            (top if id(node) in toplevel else lazy).add(target)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = _package_of(mod)
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(base + ([node.module] if node.module else []))
            elif node.module and (node.module == "factonio" or node.module.startswith("factonio.")):
                target = node.module[len("factonio"):].lstrip(".")
            else:
                continue
            for alias in node.names:
                sub = f"{target}.{alias.name}" if target else alias.name
                if sub in MODULES:
                    add(sub, node)
                    aliases[alias.asname or alias.name] = sub
                elif target in MODULES:
                    add(target, node)
                    if alias.name.startswith("_") and not alias.name.startswith("__") and target != mod:
                        private.append(f"{mod}:{node.lineno} from {target} import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name[len("factonio."):] if alias.name.startswith("factonio.") else None
                if name in MODULES:
                    add(name, node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = _dynamic_module(node.value)
            if target is not None:
                add(target, node)
                aliases[node.targets[0].id] = target
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and node.attr.startswith("_")
            and not node.attr.startswith("__")
        ):
            private.append(f"{mod}:{node.lineno} {aliases[node.value.id]}.{node.attr}")
    return _Scan(top, lazy, private)


SCANS = {mod: _scan(mod) for mod in MODULES}


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one module (Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    out: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    for v in sorted(graph):
        if v not in index:
            visit(v)
    return out


def _source(name: str) -> str:
    return MODULES[name].read_text(encoding="utf-8")


class TestImportGraph(unittest.TestCase):
    def test_scanner_sees_every_production_module(self):
        for name in ("vocabulary", "validators", "git_cli", "store_kanban", "verification", "metrics",
                     "orchestrator", "repos", "trail", "dashboard.plugin_api", ""):
            self.assertIn(name, MODULES)
        self.assertIn("metrics", SCANS["dashboard.plugin_api"].top, "dynamic import_module edges are scanned")

    def test_no_top_level_import_cycles(self):
        self.assertEqual(_cycles({m: set(s.top) for m, s in SCANS.items()}), [])

    def test_no_cycles_including_lazy_imports(self):
        combined = {m: set(s.top) | set(s.lazy) for m, s in SCANS.items()}
        self.assertEqual(_cycles(combined), [])

    def test_cycle_detector_reports_a_lazy_cycle(self):
        self.assertEqual(_cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}), [["a", "b", "c"]])

    def test_no_cross_module_private_imports(self):
        found = [p for s in SCANS.values() for p in s.private]
        self.assertEqual(found, [])

    def test_leaf_layer_imports_only_the_leaf_layer(self):
        allowed = {
            "vocabulary": set(),
            "git_cli": set(),
            "validators": {"vocabulary"},
            "store_kanban": {"vocabulary"},
        }
        for mod, deps in allowed.items():
            edges = set(SCANS[mod].top) | set(SCANS[mod].lazy)
            self.assertEqual(edges - deps, set(), mod)

    def test_layer_dependencies_point_down(self):
        forbidden = {
            "config": {"trail", "metrics", "verification", "arena", "harness", "repos", "orchestrator", "issues"},
            "trail": {"repos", "metrics", "verification", "arena", "harness", "orchestrator", "issues", "watchdog"},
            "verification": {"metrics", "arena", "harness", "repos", "orchestrator", "issues", "watchdog"},
            "arena": {"orchestrator", "metrics", "repos", "issues"},
            "metrics": {"orchestrator", "repos", "issues", "watchdog"},
            "harness": {"orchestrator", "metrics", "repos", "issues", "arena"},
            "repos": {"orchestrator", "issues", "watchdog"},
        }
        for mod, bad in forbidden.items():
            edges = set(SCANS[mod].top) | set(SCANS[mod].lazy)
            self.assertEqual(edges & bad, set(), mod)


class TestSingleSources(unittest.TestCase):
    def test_single_kanban_read_only_opener(self):
        # `trail.py` opens its own `trail.db` read-only; every `kanban.db` opener is `store_kanban.py`.
        holders = sorted(m for m in MODULES if "?mode=ro" in _source(m))
        self.assertEqual(holders, ["store_kanban", "trail"])
        self.assertIn('f"{path.resolve().as_uri()}?mode=ro"', _source("store_kanban"))
        raw_uri = [m for m in MODULES if re.search(r"""f["']file:""", _source(m))]
        self.assertEqual(raw_uri, [], "kanban.db URIs must come from Path.as_uri() in store_kanban")

    def test_single_full_sha_regex(self):
        definers = []
        for mod in MODULES:
            for node in ast.walk(ast.parse(_source(mod))):
                is_compile = isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(
                    node.value.func, "attr", None
                ) == "compile"
                if is_compile:
                    for target in node.targets:
                        if isinstance(target, ast.Name) and re.fullmatch(r"_?(FULL_)?SHA_RE", target.id):
                            definers.append(f"{mod}.{target.id}")
        self.assertEqual(definers, ["validators.FULL_SHA_RE"])
        hex40 = re.compile(r"\[0-9a-f(?:A-F)?\]\{40\}")
        offenders = [m for m in MODULES if m not in ("validators", "eval_skills") and hex40.search(_source(m))]
        # eval_skills keeps a rubric *text detector* (`\b[0-9a-f]{40}\b|head_sha`), not a validator.
        self.assertEqual(offenders, [])

    def test_single_git_and_gh_subprocess_seam(self):
        raw_argv = re.compile(r"""\[\s*["'](?:git|gh)["']\s*,""")
        offenders = [m for m in MODULES if m != "git_cli" and raw_argv.search(_source(m))]
        self.assertEqual(offenders, [], "git/gh argv must be built by git_cli")


if __name__ == "__main__":
    unittest.main()
