#!/usr/bin/env python3
"""Accept installed build identity, or install and accept both distribution formats."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile


PROBE = """
import json, sys
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from uuid import UUID
import earnings_watch
from earnings_watch.build_identity import BUILD, process_instance
expected = json.loads(sys.argv[1])
assert Path(earnings_watch.__file__).is_relative_to(Path(sys.prefix)), 'source tree imported'
assert json.loads(files('earnings_watch').joinpath('build_identity.json').read_text()) == expected
assert BUILD.as_dict() == {key: expected[key] for key in ('service', 'version', 'source_revision')}
assert version('earnings-watch') == earnings_watch.__version__ == BUILD.version
assert BUILD.short_revision == expected['source_revision'][:12]
instance = process_instance()
assert UUID(instance).version == 4 and process_instance() == instance
print(json.dumps({**BUILD.as_dict(), 'instance': instance}))
"""


def expected_identity(revision, version):
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise ValueError("expected a full source SHA and stable product version")
    return {
        "schema": 1,
        "service": "earnings-watch",
        "version": version,
        "source_revision": revision,
    }


def check_installed(python, expected, directory):
    # Deliberately hostile runtime metadata must not relabel the installed artifact.
    environment = dict(os.environ)
    environment.update(
        VERSION="9.9.9",
        PRODUCT_VERSION="9.9.9",
        SOURCE_REVISION="f" * 40,
        VCS_REF="e" * 40,
        EARNINGS_WATCH_VERSION="8.8.8",
        EARNINGS_WATCH_REVISION="d" * 40,
        FLEET_SHA="c" * 40,
        GITHUB_SHA="b" * 40,
        PYTHONPATH="/unavailable-source-tree",
        GIT_DIR="/unavailable-git",
    )
    instances = []
    for _ in range(2):
        result = subprocess.run(
            [str(python), "-I", "-c", PROBE, json.dumps(expected)],
            cwd=directory,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stderr:
            raise ValueError("installed identity probe wrote to stderr")
        value = json.loads(result.stdout)
        if {key: value[key] for key in ("service", "version", "source_revision")} != {
            key: expected[key] for key in ("service", "version", "source_revision")
        }:
            raise ValueError("installed identity differs from the producing source")
        instances.append(value["instance"])
    if instances[0] == instances[1]:
        raise ValueError("separate installed processes reused an instance identity")


def check_distributions(dist, expected):
    wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as archive:
        value = json.loads(archive.read("earnings_watch/build_identity.json"))
    if value != expected:
        raise ValueError("wheel identity differs from the producing source")
    with tarfile.open(sdists[0]) as archive:
        members = [
            item for item in archive.getmembers() if item.name.endswith("/build_identity.json")
        ]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("source distribution must contain one regular build identity")
        stream = archive.extractfile(members[0])
        assert stream is not None
        with stream:
            value = json.load(stream)
    if value != expected:
        raise ValueError("source distribution identity differs from the producing source")
    with tempfile.TemporaryDirectory(prefix="earnings-installed-") as directory:
        scratch = Path(directory)
        for kind, artifact in (("wheel", wheels[0]), ("sdist", sdists[0])):
            if kind == "sdist":
                # Rebuild explicitly so an install cache cannot substitute an older wheel.
                rebuilt = scratch / "rebuilt"
                subprocess.run(
                    [
                        "uv",
                        "build",
                        "--wheel",
                        "--no-sources",
                        "--out-dir",
                        str(rebuilt),
                        str(artifact.absolute()),
                    ],
                    check=True,
                )
                rebuilt_wheels = list(rebuilt.glob("*.whl"))
                if len(rebuilt_wheels) != 1:
                    raise ValueError("source distribution did not rebuild one wheel")
                artifact = rebuilt_wheels[0]
            environment = scratch / kind
            subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
            python = environment / "bin" / "python"
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    "--no-sources",
                    str(artifact.absolute()),
                ],
                check=True,
            )
            check_installed(python, expected, scratch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    try:
        expected = expected_identity(args.revision, args.version)
        if args.dist is None:
            with tempfile.TemporaryDirectory(prefix="earnings-identity-") as directory:
                check_installed(sys.executable, expected, directory)
        else:
            check_distributions(args.dist, expected)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"build identity acceptance failed: {error}\n")
    print(json.dumps(expected, sort_keys=True))


if __name__ == "__main__":
    main()
