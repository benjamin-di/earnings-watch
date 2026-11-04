#!/usr/bin/env python3
"""Validate the source tag and build packages, without publishing images."""

import os
from pathlib import Path
import re
import subprocess
import tomllib


def main():
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    tag = os.environ.get("FLEET_VERSION", "")
    if not tag and re.fullmatch(
        r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", os.environ.get("FLEET_REF", "")
    ):
        tag = os.environ["FLEET_REF"][1:]
    if tag and (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", tag) or tag != version
    ):
        raise SystemExit(f"Source tag {tag!r} differs from package version {version!r}")
    # Eliminate stale distributions before building, without broad directory removal.
    dist = Path("dist")
    if dist.is_symlink():
        raise SystemExit("Refusing a linked distribution directory")
    for pattern in ("*.whl", "*.tar.gz"):
        for artifact in dist.glob(pattern):
            artifact.unlink()
    subprocess.run(["uv", "build", "--no-sources"], check=True)
    if not list(dist.glob("*.whl")) or not list(dist.glob("*.tar.gz")):
        raise SystemExit("Package build did not produce both wheel and source distribution")


if __name__ == "__main__":
    main()
