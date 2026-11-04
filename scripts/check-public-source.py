"""Offline source-home contract checks; credentials and runtime data are never read."""

import json
import re
import sys
import tomllib
from pathlib import Path

REPOSITORY = "benjamin-di/earnings-watch"
MARKER = ".public-source.toml"


def check(root: Path) -> None:
    expected = {
        "schema": 1,
        "repository": REPOSITORY,
        "images": ["earnings-watch", "earnings-watch-worker"],
    }
    if tomllib.loads((root / MARKER).read_text()) != expected:
        raise ValueError("unexpected public source contract")
    for private in (
        "private",
        "publish",
        "AGENTS.md",
        ".github/jenkins",
        ".gitmodules",
        "docs/reviews",
    ):
        if (root / private).exists() or (root / private).is_symlink():
            raise ValueError("unsupported public source path: " + private)
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    registry = json.loads((root / "server.json").read_text())
    if project["version"] != registry["version"]:
        raise ValueError("package and registry versions disagree")
    if project["urls"]["Homepage"] != "https://github.com/" + REPOSITORY:
        raise ValueError("package source URL must identify the public source")
    if registry["repository"]["url"] != project["urls"]["Homepage"]:
        raise ValueError("registry source URL must identify the public source")
    if not registry.get("packages") or any(
        package.get("identifier") != "earnings-watch"
        or package.get("version") != project["version"]
        for package in registry["packages"]
    ):
        raise ValueError("registry packages must match the released package")
    for page in root.rglob("*.md"):
        if any(part.startswith(".") for part in page.relative_to(root).parts):
            continue
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", page.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            if not (page.parent / target.split("#")[0]).exists():
                raise ValueError("broken documentation link: " + str(page.relative_to(root)))


if __name__ == "__main__":
    try:
        check(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd())
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(str(error)) from error
    print("Public source contract, package versions and documentation links passed")
