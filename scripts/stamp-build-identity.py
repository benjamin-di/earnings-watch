#!/usr/bin/env python3
"""Stamp a selected source tree before building its distributions or images."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

SERVICE = "earnings-watch"
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")


def stamp(root: Path, revision: str, version: str | None) -> None:
    if not REVISION.fullmatch(revision):
        raise ValueError("a full lowercase source revision is required")
    project_file = root / "pyproject.toml"
    package = root / "src" / "earnings_watch"
    target = package / "build_identity.json"
    if any(path.is_symlink() for path in (root, project_file, root / "src", package, target)):
        raise ValueError("linked build identity inputs or destination are refused")
    project = tomllib.loads(project_file.read_text())["project"]
    product = project["version"]
    if project["name"] != SERVICE or not isinstance(product, str) or not VERSION.fullmatch(product):
        raise ValueError("the source must declare the expected product and a stable version")
    if version is not None and version != product:
        raise ValueError("the expected version differs from the selected source")
    if not package.is_dir() or (target.exists() and not target.is_file()):
        raise ValueError("the package identity destination is not a regular file path")
    value = {"schema": 1, "service": SERVICE, "version": product, "revision": revision}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=package, prefix=".build-identity-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, sort_keys=True) + "\n")
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version")
    args = parser.parse_args()
    try:
        stamp(args.root.absolute(), args.revision, args.version)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, "build identity stamping refused: invalid source metadata or destination\n")


if __name__ == "__main__":
    main()
