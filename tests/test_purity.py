"""The purity gate.

The render core and the estimator are pure by contract: no wall time, no randomness, no I/O,
and no imports from the layers beneath them. Those are architectural laws, not preferences, and
laws that live only in a document erode. This test reads the source of both packages and fails
on any path by which they could stop being true, so the contract is enforced on every run from
the first commit onward.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "pascl"

# What each pure package may import from inside the project. Order matters: the core may not
# know the estimator exists, and neither may know the harness or the shell.
ALLOWED_INTERNAL: dict[str, frozenset[str]] = {
    "core": frozenset({"pascl.model", "pascl.core", "pascl.clock"}),
    "estimator": frozenset({"pascl.model", "pascl.core", "pascl.estimator", "pascl.clock"}),
}

# Standard-library modules that are a clock, a random source or an I/O surface.
FORBIDDEN_MODULES = frozenset(
    {
        "time",
        "random",
        "secrets",
        "os",
        "sys",
        "io",
        "socket",
        "select",
        "signal",
        "asyncio",
        "threading",
        "multiprocessing",
        "subprocess",
        "pathlib",
        "shutil",
        "tempfile",
        "urllib",
        "http",
        "sqlite3",
        "logging",
    }
)
# Attribute calls that read the wall clock even when the module itself is allowed.
FORBIDDEN_CALLS = frozenset(
    {("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"), ("date", "today")}
)
# Names that are I/O or the real clock by definition.
FORBIDDEN_NAMES = frozenset({"open", "input", "print", "SystemClock"})


def _pure_files() -> list[Path]:
    return sorted(p for pkg in ALLOWED_INTERNAL for p in (SRC / pkg).rglob("*.py"))


def _root(module: str) -> str:
    return module.split(".", 1)[0]


def _violations(path: Path) -> list[str]:
    package = path.relative_to(SRC).parts[0]
    allowed_internal = ALLOWED_INTERNAL[package]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.extend(_check_module(alias.name, allowed_internal, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = f"pascl.{package}" if not module else f"pascl.{package}.{module}"
            found.extend(_check_module(module, allowed_internal, node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in FORBIDDEN_CALLS
            ):
                found.append(f"line {node.lineno}: wall time via {func.value.id}.{func.attr}()")
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
                found.append(f"line {node.lineno}: calls {func.id}()")
        elif isinstance(node, ast.Name) and node.id == "SystemClock":
            found.append(f"line {node.lineno}: names SystemClock, which belongs to the shell")
    return found


def _check_module(module: str, allowed_internal: frozenset[str], lineno: int) -> list[str]:
    if _root(module) in FORBIDDEN_MODULES:
        return [f"line {lineno}: imports {module}"]
    if _root(module) == "pascl" and not any(
        module == a or module.startswith(a + ".") for a in allowed_internal
    ):
        return [f"line {lineno}: imports {module}, outside this layer's allowed set"]
    return []


def test_pure_packages_exist() -> None:
    for pkg in ALLOWED_INTERNAL:
        assert (SRC / pkg / "__init__.py").is_file(), f"src/pascl/{pkg} is missing"


@pytest.mark.parametrize("path", _pure_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_pure_package_source_stays_pure(path: Path) -> None:
    violations = _violations(path)
    assert not violations, f"{path.relative_to(SRC)} is not pure:\n  " + "\n  ".join(violations)


def test_gate_catches_a_wall_clock_read(tmp_path: Path) -> None:
    """The gate itself must work, or its silence means nothing."""
    fake_pkg = tmp_path / "src" / "pascl" / "core"
    fake_pkg.mkdir(parents=True)
    bad = fake_pkg / "bad.py"
    bad.write_text(
        "import time\nfrom datetime import datetime\nfrom pascl.shell import x\n"
        "def f():\n    return datetime.now(), time.time(), open('x')\n",
        encoding="utf-8",
    )
    global SRC
    real = SRC
    SRC = tmp_path / "src" / "pascl"
    try:
        found = _violations(bad)
    finally:
        SRC = real
    joined = "\n".join(found)
    assert "imports time" in joined
    assert "pascl.shell" in joined
    assert "datetime.now()" in joined
    assert "calls open()" in joined
