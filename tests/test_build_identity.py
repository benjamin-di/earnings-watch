"""Real stamp/module boundaries, using only synthetic artifact metadata."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from uuid import UUID
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "earnings_watch" / "build_identity.py"
STAMP = ROOT / "scripts" / "stamp-build-identity.py"
REVISION = "a" * 40
VALUE = {"schema": 1, "service": "earnings-watch", "version": "0.3.3", "revision": REVISION}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    package = root / "src" / "earnings_watch"
    package.mkdir(parents=True)
    # The reader itself is stdlib-only. Full installed-package qualification is separate.
    (package / "__init__.py").write_text('"""Synthetic package for the real reader."""\n')
    shutil.copy2(MODULE, package / "build_identity.py")
    (root / "pyproject.toml").write_text('[project]\nname = "earnings-watch"\nversion = "0.3.3"\n')
    return root


def run_stamp(source, *, revision=REVISION, version=None):
    command = [sys.executable, "-I", str(STAMP), "--root", str(source), "--revision", revision]
    if version is not None:
        command += ["--version", version]
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


def probe(source, body=None):
    if body is None:
        body = """
from earnings_watch.build_identity import BUILD, process_instance
print(json.dumps({**BUILD.as_dict(), "short": BUILD.short_revision,
                  "instance": process_instance(), "again": process_instance()}))
"""
    code = f"import json, sys; sys.path.insert(0, {str(source / 'src')!r})\n" + body
    environment = {
        "PATH": "",
        "HOME": str(source),
        "PYTHONPATH": "/unavailable-untrusted-package",
        "VERSION": "9.9.9",
        "SOURCE_REVISION": "f" * 40,
        "VCS_REF": "e" * 40,
        "EARNINGS_WATCH_VERSION": "8.8.8",
        "EARNINGS_WATCH_REVISION": "d" * 40,
        "GIT_DIR": "/unavailable-git",
    }
    process = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    assert process.stderr == ""
    return json.loads(process.stdout)


def manifest(source):
    return source / "src" / "earnings_watch" / "build_identity.json"


def test_real_stamp_and_isolated_reader_reject_runtime_relabeling(source):
    result = run_stamp(source, version="0.3.3")
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert json.loads(manifest(source).read_text()) == VALUE
    first, second = probe(source), probe(source)
    for value in (first, second):
        assert {key: value[key] for key in ("service", "version", "revision")} == {
            key: VALUE[key] for key in ("service", "version", "revision")
        }
        assert value["short"] == REVISION[:12]
        assert value["instance"] == value["again"]
        assert UUID(value["instance"]).version == 4
    assert first["instance"] != second["instance"]


def test_build_and_process_instance_are_eager_and_frozen_after_import(source):
    assert run_stamp(source).returncode == 0
    value = probe(
        source,
        f"""
import uuid
from pathlib import Path
original = uuid.uuid4
calls = []
def counted():
    calls.append(1)
    return original()
uuid.uuid4 = counted
from earnings_watch.build_identity import BUILD, process_instance
assert len(calls) == 1
Path({str(manifest(source))!r}).write_text('{{}}')
first = process_instance()
assert first == process_instance() and len(calls) == 1
try:
    BUILD.version = "9.9.9"
except Exception as exc:
    assert type(exc).__name__ == "FrozenInstanceError"
else:
    raise AssertionError("build identity was mutable")
print(json.dumps({{**BUILD.as_dict(), "instance": first}}))
""",
    )
    assert value["version"] == "0.3.3" and value["revision"] == REVISION


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a fork-capable platform")
def test_fork_has_its_own_eager_stable_process_instance(source):
    assert run_stamp(source).returncode == 0
    value = probe(
        source,
        """
import os
from earnings_watch import build_identity as identity
parent = identity.process_instance()
read, write = os.pipe()
child = os.fork()
if child == 0:
    os.close(read)
    assert identity._PID == os.getpid()
    eager = identity._INSTANCE
    result = {"instance": identity.process_instance(), "again": identity.process_instance(),
              "eager": eager, "build": identity.BUILD.as_dict()}
    os.write(write, json.dumps(result).encode())
    os.close(write)
    os._exit(0)
os.close(write)
result = json.loads(os.read(read, 16384))
os.close(read)
assert os.waitpid(child, 0)[1] == 0
assert parent == identity.process_instance()
print(json.dumps({"parent": parent, "child": result}))
""",
    )
    child = value["child"]
    assert child["instance"] == child["again"] == child["eager"]
    assert child["instance"] != value["parent"]
    assert child["build"]["revision"] == REVISION


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"\xff",
        "{",
        "null",
        "[]",
        json.dumps(VALUE).replace('"schema": 1', '"schema": true'),
        json.dumps(VALUE).replace('"schema": 1', '"schema": 2'),
        json.dumps(VALUE).replace('"schema": 1', '"schema": "1"'),
        json.dumps(VALUE).replace('"earnings-watch"', '"another-product"'),
        json.dumps(VALUE).replace('"0.3.3"', '"unknown"'),
        json.dumps(VALUE).replace('"0.3.3"', '"0.03.3"'),
        json.dumps(VALUE).replace('"0.3.3"', '"0.3.3.dev1"'),
        json.dumps(VALUE).replace(REVISION, "A" * 40),
        json.dumps(VALUE).replace(REVISION, "a" * 39),
        json.dumps(VALUE).replace(REVISION, " " + REVISION),
        json.dumps(VALUE | {"extra": "not admitted"}),
        json.dumps({key: item for key, item in VALUE.items() if key != "revision"}),
        json.dumps(VALUE | {"revision": [REVISION]}),
        json.dumps(VALUE)[:-1] + ', "version": "9.9.9"}',
        json.dumps(VALUE)[:-1] + ', "schema": 1}',
        "[" * 2000 + "]" * 2000,
        " " * 16385,
    ],
)
def test_unavailable_or_unadmitted_manifest_never_guesses_identity(source, content):
    if isinstance(content, bytes):
        manifest(source).write_bytes(content)
    elif content is not None:
        manifest(source).write_text(content)
    value = probe(source)
    assert value["service"] == "earnings-watch"
    assert value["version"] == value["revision"] == value["short"] == "unknown"
    assert UUID(value["instance"]).version == 4


@pytest.mark.parametrize("revision", ["unknown", "A" * 40, "a" * 39, "a" * 41, " " + REVISION])
def test_stamper_rejects_invalid_revision_without_writing(source, revision):
    result = run_stamp(source, revision=revision)
    assert result.returncode == 2
    assert not manifest(source).exists()
    assert result.stdout == ""


@pytest.mark.parametrize(
    "metadata, expected_version",
    [
        ('[project]\nname = "other"\nversion = "0.3.3"\n', None),
        ('[project]\nname = "earnings-watch"\nversion = "unknown"\n', None),
        ('[project]\nname = "earnings-watch"\nversion = "0.03.3"\n', None),
        ('[project]\nname = "earnings-watch"\nversion = 3\n', None),
        ("{not toml}", None),
        ('[project]\nname = "earnings-watch"\nversion = "0.3.3"\n', "0.3.4"),
    ],
)
def test_stamper_rejects_wrong_source_product_or_expected_version(
    source, metadata, expected_version
):
    (source / "pyproject.toml").write_text(metadata)
    result = run_stamp(source, version=expected_version)
    assert result.returncode == 2
    assert result.stdout == ""
    assert not manifest(source).exists()


def test_failed_stamp_preserves_prior_complete_identity(source):
    assert run_stamp(source).returncode == 0
    previous = manifest(source).read_bytes()
    assert run_stamp(source, revision="unknown").returncode == 2
    assert manifest(source).read_bytes() == previous
    assert not list(manifest(source).parent.glob(".build-identity-*"))


@pytest.mark.parametrize("selected", ["pyproject.toml", "src", "src/earnings_watch", "manifest"])
def test_stamper_refuses_linked_input_or_destination(source, tmp_path, selected):
    path = manifest(source) if selected == "manifest" else source / selected
    outside = tmp_path / "outside"
    if path.exists():
        path.rename(outside)
    else:
        outside.write_text("unchanged\n")
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    result = run_stamp(source)
    assert result.returncode == 2
    if selected == "manifest":
        assert outside.read_text() == "unchanged\n"
    else:
        assert not manifest(source).exists()


def test_hatch_includes_the_ignored_generated_manifest_in_wheel_and_sdist(source, tmp_path):
    for name in ("pyproject.toml", ".gitignore", "README.md", "LICENSE", "NOTICE"):
        shutil.copy2(ROOT / name, source / name)
    subprocess.run(["git", "init", "--quiet", str(source)], check=True, capture_output=True)
    assert run_stamp(source).returncode == 0
    ignored = subprocess.run(
        ["git", "-C", str(source), "check-ignore", "src/earnings_watch/build_identity.json"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert ignored.stdout.strip() == "src/earnings_watch/build_identity.json"
    dist = tmp_path / "dist"
    built = subprocess.run(
        ["uv", "build", "--offline", "--no-sources", "--out-dir", str(dist), str(source)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stderr
    wheel, sdist = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
    assert len(wheel) == len(sdist) == 1
    with zipfile.ZipFile(wheel[0]) as archive:
        assert json.loads(archive.read("earnings_watch/build_identity.json")) == VALUE
    with tarfile.open(sdist[0]) as archive:
        members = [
            item for item in archive.getmembers() if item.name.endswith("/build_identity.json")
        ]
        assert len(members) == 1
        stream = archive.extractfile(members[0])
        assert stream is not None
        assert json.load(stream) == VALUE
