#!/usr/bin/env python3
"""Validate the source tag and build packages, without publishing images."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib


def main():
    root = Path.cwd()
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    tag = os.environ.get("FLEET_VERSION", "")
    if not tag and re.fullmatch(
        r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", os.environ.get("FLEET_REF", "")
    ):
        tag = os.environ["FLEET_REF"][1:]
    if not tag and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME", "").removeprefix("v")
        if not tag:
            raise SystemExit("Expected the source tag version")
    if tag and (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", tag) or tag != version
    ):
        raise SystemExit(f"Source tag {tag!r} differs from package version {version!r}")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit("Expected a full source SHA")
    for variable in ("FLEET_SHA", "GITHUB_SHA"):
        if os.environ.get(variable) and os.environ[variable] != sha:
            raise SystemExit(f"{variable} differs from the selected source SHA")
    identity = root / "src/earnings_watch/build_identity.json"
    if identity.is_symlink():
        raise SystemExit("Refusing a linked source build stamp")
    previous = identity.read_bytes() if identity.exists() else None
    try:
        subprocess.run(
            [
                sys.executable,
                str(root / "scripts/stamp-build-identity.py"),
                "--root",
                str(root),
                "--revision",
                sha,
                "--version",
                version,
            ],
            check=True,
        )
        dist = Path("dist")
        if dist.is_symlink():
            raise SystemExit("Refusing a linked distribution directory")
        for pattern in ("*.whl", "*.tar.gz"):
            for artifact in dist.glob(pattern):
                artifact.unlink()
        subprocess.run(["uv", "build", "--no-sources"], check=True)
        if not list(dist.glob("*.whl")) or not list(dist.glob("*.tar.gz")):
            raise SystemExit("Package build did not produce both wheel and source distribution")
        subprocess.run(
            [
                sys.executable,
                str(root / "scripts/check-build-identity.py"),
                "--dist",
                str(dist),
                "--revision",
                sha,
                "--version",
                version,
            ],
            check=True,
        )
    finally:
        if previous is None:
            identity.unlink(missing_ok=True)
        else:
            identity.write_bytes(previous)


if __name__ == "__main__":
    main()
