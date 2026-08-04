#!/usr/bin/env python3
"""Validate a rendered copier output tree.

Checks that a freshly scaffolded project is structurally sound: every expected
file exists, no template artifacts survived rendering, all Python parses, the
pyproject declares the metadata a publishable package needs, and the generated
workflow is valid YAML.

This deliberately stops short of installing the project. Installing is a
stronger check and belongs in CI too, but it depends on pete-mcp-core being
resolvable from PyPI, whereas everything here runs offline.

Usage:
    python scripts/validate_render.py /path/to/rendered/output
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

# Relative to the rendered root. "{pkg}" is substituted with the discovered
# package directory name, since it depends on the answers copier was given.
REQUIRED_FILES = [
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "Dockerfile",
    "docker-compose.yml",
    ".env.example",
    ".gitignore",
    ".github/workflows/ci.yml",
    "src/{pkg}/__init__.py",
    "src/{pkg}/server.py",
    "src/{pkg}/settings.py",
    "src/{pkg}/healthcheck.py",
    "tests/test_server.py",
]

REQUIRED_PROJECT_KEYS = [
    "name",
    "version",
    "description",
    "requires-python",
    "dependencies",
]


def fail(msg: str) -> None:
    """Emit a GitHub Actions error annotation and record the failure."""
    print(f"::error::{msg}")
    _failures.append(msg)


_failures: list[str] = []


def discover_package(root: Path) -> str | None:
    """Return the single package directory name under src/."""
    src = root / "src"
    if not src.is_dir():
        fail("no src/ directory in rendered output")
        return None
    pkgs = [p.name for p in src.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(pkgs) != 1:
        fail(f"expected exactly one package under src/, found {pkgs}")
        return None
    return pkgs[0]


def check_files(root: Path, pkg: str) -> None:
    for rel in REQUIRED_FILES:
        path = root / rel.format(pkg=pkg)
        if not path.is_file():
            fail(f"missing expected file: {rel.format(pkg=pkg)}")


def check_no_template_artifacts(root: Path) -> None:
    """A surviving .jinja suffix or Jinja block means a file was copied, not rendered."""
    for path in root.rglob("*.jinja"):
        if ".git" in path.parts:
            continue
        fail(f"unrendered template file: {path.relative_to(root)}")

    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue

        rel = path.relative_to(root)
        if "{%" in text:
            fail(f"unrendered Jinja block in {rel}")
        # GitHub Actions expressions in the *generated* workflow are legitimate
        # and share Jinja's delimiter, so only that directory is exempt.
        if "{{" in text and "workflows" not in rel.parts:
            fail(f"unrendered Jinja expression in {rel}")


def check_python_parses(root: Path) -> None:
    for path in root.rglob("*.py"):
        if ".git" in path.parts:
            continue
        try:
            ast.parse(path.read_text())
        except SyntaxError as exc:
            fail(f"rendered Python does not parse: {path.relative_to(root)}: {exc}")


def check_pyproject(root: Path) -> None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return
    try:
        data = tomllib.load(path.open("rb"))
    except tomllib.TOMLDecodeError as exc:
        fail(f"pyproject.toml is not valid TOML: {exc}")
        return

    project = data.get("project")
    if project is None:
        fail("pyproject.toml has no [project] table")
        return

    for key in REQUIRED_PROJECT_KEYS:
        if key not in project:
            fail(f"pyproject [project] is missing {key}")

    if "build-system" not in data:
        fail("pyproject.toml has no [build-system] table")

    name = project.get("name", "?")
    py = project.get("requires-python", "?")
    print(f"  package: {name}  requires-python: {py}")


def check_workflow_yaml(root: Path) -> None:
    path = root / ".github" / "workflows" / "ci.yml"
    if not path.is_file():
        return
    try:
        import yaml
    except ImportError:
        print("  (pyyaml unavailable, skipping workflow YAML check)")
        return
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        fail(f"generated workflow is not valid YAML: {exc}")
        return
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not jobs:
        fail("generated workflow declares no jobs")
        return
    print(f"  generated workflow jobs: {list(jobs)}")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"::error::not a directory: {root}")
        return 1

    print(f"Validating rendered output at {root}")
    pkg = discover_package(root)
    if pkg is not None:
        print(f"  package dir: src/{pkg}")
        check_files(root, pkg)
    check_no_template_artifacts(root)
    check_python_parses(root)
    check_pyproject(root)
    check_workflow_yaml(root)

    if _failures:
        print(f"\n{len(_failures)} problem(s) found.")
        return 1
    print("\nRendered output looks structurally sound.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
