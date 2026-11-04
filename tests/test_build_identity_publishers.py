"""Exercise package publishers offline; image publisher contracts use a Docker double."""

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
VERSION = "0.3.3"
VALUE = {"schema": 1, "service": "earnings-watch", "version": VERSION, "revision": REVISION}


def source_tree(tmp_path):
    root = tmp_path / "source"
    package = root / "src/earnings_watch"
    package.mkdir(parents=True)
    for name in ("__init__.py", "build_identity.py"):
        shutil.copy2(ROOT / "src/earnings_watch" / name, package / name)
    for name in (
        "pyproject.toml",
        ".gitignore",
        "README.md",
        "LICENSE",
        "NOTICE",
        "Dockerfile",
        "uv.lock",
    ):
        shutil.copy2(ROOT / name, root / name)
    (root / "scripts").mkdir()
    for name in ("stamp-build-identity.py", "ci-package.py", "check-build-identity.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    commands = root / "bin"
    commands.mkdir()
    git = commands / "git"
    git.write_text('#!/bin/sh\nprintf "%s\\n" "$SELECTED_SHA"\n')
    git.chmod(0o755)
    environment = {
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "SELECTED_SHA": REVISION,
        "UV_OFFLINE": "1",
    }
    return root, environment


@pytest.fixture
def source(tmp_path):
    return source_tree(tmp_path)


def package(source, **environment):
    root, base = source
    return subprocess.run(
        [sys.executable, str(root / "scripts/ci-package.py")],
        cwd=root,
        env=base | environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def published_distributions(tmp_path_factory):
    source = source_tree(tmp_path_factory.mktemp("publisher-artifacts"))
    result = package(source, FLEET_SHA=REVISION, FLEET_VERSION=VERSION)
    assert result.returncode == 0, result.stdout + result.stderr
    return source, result


@pytest.fixture
def distributions(published_distributions, tmp_path):
    (original, environment), result = published_distributions
    root = tmp_path / "accepted"
    shutil.copytree(original / "dist", root / "dist")
    (root / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/check-build-identity.py", root / "scripts/check-build-identity.py")
    return (root, environment), result


def test_package_publisher_really_installs_wheel_and_rebuilt_sdist(distributions):
    (root, _), result = distributions
    assert json.loads(result.stdout.splitlines()[-1]) == VALUE
    (wheel,) = (root / "dist").glob("*.whl")
    (sdist,) = (root / "dist").glob("*.tar.gz")
    with zipfile.ZipFile(wheel) as archive:
        assert json.loads(archive.read("earnings_watch/build_identity.json")) == VALUE
    with tarfile.open(sdist) as archive:
        stream = archive.extractfile(
            next(
                member
                for member in archive.getmembers()
                if member.name.endswith("/build_identity.json")
            )
        )
        assert stream is not None
        with stream:
            assert json.load(stream) == VALUE
    assert "earnings-installed-" in result.stderr
    assert "Building wheel from source distribution" in result.stderr


@pytest.mark.parametrize(
    "environment",
    [
        {"SELECTED_SHA": ""},
        {"SELECTED_SHA": "A" * 40},
        {"SELECTED_SHA": "a" * 39},
        {"FLEET_SHA": "b" * 40},
        {"GITHUB_SHA": "b" * 40},
        {"FLEET_VERSION": "0.3.4"},
        {"FLEET_REF": "v0.3.4"},
        {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v0.3.4"},
        {"GITHUB_REF_TYPE": "tag"},
    ],
)
def test_package_publisher_refuses_wrong_or_missing_selected_inputs_before_build(
    source, environment
):
    result = package(source, **environment)
    assert result.returncode != 0
    root, _ = source
    assert not (root / "dist").exists()
    assert not (root / "src/earnings_watch/build_identity.json").exists()


def test_installed_acceptance_refuses_a_lost_or_replaced_stamp(published_distributions, tmp_path):
    (root, environment), _ = published_distributions
    venv = tmp_path / "installed"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(venv)],
        env=environment,
        check=True,
        capture_output=True,
    )
    (wheel,) = (root / "dist").glob("*.whl")
    python = venv / "bin/python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        env=environment,
        check=True,
        capture_output=True,
    )
    (manifest,) = venv.glob("lib/python*/site-packages/earnings_watch/build_identity.json")
    for value in (None, VALUE | {"revision": "b" * 40}):
        if value is None:
            manifest.unlink()
        else:
            manifest.write_text(json.dumps(value))
        result = subprocess.run(
            [
                str(python),
                "-I",
                str(ROOT / "scripts/check-build-identity.py"),
                "--revision",
                REVISION,
                "--version",
                VERSION,
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "build identity acceptance failed" in result.stderr


@pytest.mark.parametrize(
    "environment",
    [
        {"SELECTED_SHA": ""},
        {"FLEET_SHA": "b" * 40},
        {"GITHUB_SHA": "b" * 40},
        {"FLEET_VERSION": "0.3.4"},
    ],
)
def test_native_images_refuse_mismatched_inputs_before_starting_docker(source, environment):
    root, base = source
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci-images.py"), "--arch", "amd64"],
        cwd=root,
        env=base | environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "builder-create" not in result.stdout
    assert "SHA" in result.stderr or "version" in result.stderr


@pytest.mark.parametrize(
    "kind, change",
    [
        ("wheel", "missing"),
        ("wheel", "wrong"),
        ("sdist", "missing"),
        ("sdist", "wrong"),
        ("expected", "revision"),
        ("expected", "version"),
    ],
)
def test_distribution_acceptance_rejects_missing_or_inconsistent_identity(
    distributions, kind, change
):
    (root, environment), _ = distributions
    revision, version = REVISION, VERSION
    if kind == "expected":
        if change == "revision":
            revision = "b" * 40
        else:
            version = "0.3.4"
    elif kind == "wheel":
        (artifact,) = (root / "dist").glob("*.whl")
        with zipfile.ZipFile(artifact) as archive:
            entries = [
                (item, archive.read(item))
                for item in archive.infolist()
                if item.filename != "earnings_watch/build_identity.json"
            ]
        with zipfile.ZipFile(artifact, "w") as archive:
            for item, content in entries:
                archive.writestr(item, content)
            if change == "wrong":
                archive.writestr(
                    "earnings_watch/build_identity.json", json.dumps(VALUE | {"revision": "b" * 40})
                )
    else:
        (artifact,) = (root / "dist").glob("*.tar.gz")
        temporary = artifact.with_suffix(".tmp")
        with tarfile.open(artifact) as original, tarfile.open(temporary, "w:gz") as output:
            for member in original.getmembers():
                if member.name.endswith("/build_identity.json"):
                    if change == "missing":
                        continue
                    # Change another required field without changing archive layout.
                    stream = original.extractfile(member)
                    assert stream is not None
                    content = stream.read().replace(b'"0.3.3"', b'"0.3.4"')
                    member.size = len(content)
                    output.addfile(member, io.BytesIO(content))
                else:
                    output.addfile(
                        member, original.extractfile(member) if member.isfile() else None
                    )
        temporary.replace(artifact)
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/check-build-identity.py"),
            "--dist",
            str(root / "dist"),
            "--revision",
            revision,
            "--version",
            version,
        ],
        env=environment,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "revision, version, succeeds",
    [
        (REVISION, VERSION, True),
        ("", VERSION, False),
        (REVISION, "", False),
        ("a" * 39, VERSION, False),
        (REVISION, "0.3.4", False),
    ],
)
def test_docker_build_uses_real_stamper_and_requires_both_inputs(
    source, revision, version, succeeds
):
    root, environment = source
    dockerfile = (ROOT / "Dockerfile").read_text()
    command = re.search(r'RUN test -n "\$SOURCE_REVISION".*?(?=\nFROM)', dockerfile, re.S)
    assert command is not None
    stamp = re.split(r"&&\s+uv sync", command.group()[4:].replace("\\\n", " "), maxsplit=1)[0]
    # Execute the Docker RUN's real stamping segment; Docker execution is a separate CI gate.
    stamp = stamp.replace(
        "/tmp/stamp-build-identity.py", shlex.quote(str(root / "scripts/stamp-build-identity.py"))
    )
    stamp = stamp.replace("--root /app", "--root " + shlex.quote(str(root)))
    result = subprocess.run(
        ["sh", "-c", stamp],
        cwd=root,
        env=environment | {"SOURCE_REVISION": revision, "PRODUCT_VERSION": version},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is succeeds, result.stderr
    manifest = root / "src/earnings_watch/build_identity.json"
    assert manifest.exists() is succeeds
    if succeeds:
        assert json.loads(manifest.read_text()) == VALUE
    assert "/app/.venv/bin/python -I /tmp/check-build-identity.py" in dockerfile


def test_native_images_pass_selected_inputs_and_enforce_matching_provenance(source):
    root, _ = source
    spec = importlib.util.spec_from_file_location("image_publishers", ROOT / "scripts/ci-images.py")
    assert spec and spec.loader
    images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(images)
    for wrong_labels in (False, True):
        calls = []
        qualification = images.Qualification(root, "amd64", REVISION, root / "reports/images")

        def phase(name, command, timeout, required=True):
            calls.append(command)
            log = root / "reports/images" / (name + ".log")
            content = ""
            if "--output" in command:
                Path(command[command.index("--output") + 1].split("dest=", 1)[1]).touch()
            if name.endswith("architecture"):
                content = "amd64"
            if name.endswith("labels"):
                labels = qualification.labels | (
                    {"org.opencontainers.image.revision": "b" * 40} if wrong_labels else {}
                )
                content = json.dumps(labels)
            log.write_text(content)
            return log, 0

        qualification.phase = phase
        if wrong_labels:
            with pytest.raises(RuntimeError, match="image provenance"):
                qualification.run()
        else:
            qualification.run()
            builds = [command for command in calls if command[:3] == ["docker", "buildx", "build"]]
            assert len(builds) == 2
            for build in builds:
                assert f"SOURCE_REVISION={REVISION}" in build
                assert f"PRODUCT_VERSION={VERSION}" in build
            smokes = [command for command in calls if command[:2] == ["docker", "run"]]
            assert len(smokes) == 5
            for smoke in (smokes[0], smokes[2]):
                assert f"--revision {REVISION} --version {VERSION}" in smoke[-1]
                assert "/usr/local/bin/check-build-identity.py" in smoke[-1]
                assert smoke[smoke.index("--network") + 1] == "none"
        assert calls[-1][:4] == ["docker", "buildx", "rm", "--force"]


def release_workflow():
    return yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("wrong_labels", [False, True])
def test_public_image_acceptance_checks_candidate_digest_and_source_labels(tmp_path, wrong_labels):
    workflow = release_workflow()
    build = next(step for step in workflow["jobs"]["images"]["steps"] if step.get("id") == "build")[
        "with"
    ]
    assert build["build-args"].splitlines() == [
        "SOURCE_REVISION=${{ github.sha }}",
        "PRODUCT_VERSION=${{ steps.metadata.outputs.version }}",
    ]
    assert build["push"] == "true" and "candidate-${{ github.sha }}" in build["tags"]
    step = next(step for step in workflow["jobs"]["smoke"]["steps"] if "run" in step)
    (tmp_path / "digests").mkdir()
    (tmp_path / "digests/earnings-watch-worker").write_text("sha256:" + "1" * 64)
    calls = tmp_path / "calls.jsonl"
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CALLS'], 'a') as output: output.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['image', 'inspect']:
    print(json.dumps({'org.opencontainers.image.source': 'https://github.com/example/earnings-watch',
                      'org.opencontainers.image.revision': os.environ['LABEL_SHA'],
                      'org.opencontainers.image.version': '0.3.3'}))
""")
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"].replace("${{ matrix.image }}", "earnings-watch-worker")],
        cwd=tmp_path,
        env=os.environ
        | {
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "CALLS": str(calls),
            "IMAGE": "ghcr.io/example/earnings-watch-worker",
            "ARCH": "amd64",
            "GITHUB_REPOSITORY": "example/earnings-watch",
            "EXPECTED_SHA": REVISION,
            "GITHUB_REF_NAME": "v" + VERSION,
            "LABEL_SHA": "b" * 40 if wrong_labels else REVISION,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is not wrong_labels, result.stderr
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    smoke = commands[0]
    assert "--network" in smoke and "none" in smoke
    assert smoke[-2:] == [REVISION, VERSION]
    assert "check-build-identity.py" in smoke[-4]
    assert any("@sha256:" + "1" * 64 in argument for argument in smoke)
    if not wrong_labels:
        assert [
            command[command.index("--entrypoint") + 1] for command in (commands[2], commands[3])
        ] == ["claude", "codex"]


def test_resumed_public_package_publication_rechecks_downloaded_distributions():
    workflow = release_workflow()
    commands = next(step["run"] for step in workflow["jobs"]["pypi"]["steps"] if "run" in step)
    assert commands.index("gh release download") < commands.index("scripts/check-build-identity.py")
    assert '--dist dist --revision "$GITHUB_SHA" --version "${GITHUB_REF_NAME#v}"' in commands
    assert commands.index("scripts/check-build-identity.py") < commands.index("twine check")
