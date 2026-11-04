"""Source-home checks use a fictional tree, never operational records."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "public_source_check", Path(__file__).resolve().parents[1] / "scripts/check-public-source.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def source(tmp_path):
    (tmp_path / ".public-source.toml").write_text(
        'schema = 1\nrepository = "benjamin-di/earnings-watch"\n'
        'images = ["earnings-watch", "earnings-watch-worker"]\n'
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "1.2.3"\n[project.urls]\n'
        'Homepage = "https://github.com/benjamin-di/earnings-watch"\n'
    )
    (tmp_path / "server.json").write_text(
        json.dumps(
            {
                "version": "1.2.3",
                "repository": {"url": "https://github.com/benjamin-di/earnings-watch"},
                "packages": [{"identifier": "earnings-watch", "version": "1.2.3"}],
            }
        )
    )
    MODULE.check(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "path", ["private", "publish", "AGENTS.md", ".github/jenkins", "docs/reviews"]
)
def test_source_refuses_private_paths(source, path):
    (source / path).mkdir(parents=True)
    with pytest.raises(ValueError, match="unsupported public source path"):
        MODULE.check(source)


def test_source_refuses_unknown_marker_keys(source):
    with (source / ".public-source.toml").open("a") as stream:
        stream.write('unreviewed = "value"\n')
    with pytest.raises(ValueError, match="unexpected public source contract"):
        MODULE.check(source)


def test_source_refuses_version_drift(source):
    (source / "server.json").write_text('{"version": "1.2.4"}')
    with pytest.raises(ValueError, match="versions disagree"):
        MODULE.check(source)


def test_source_refuses_broken_relative_docs(source):
    (source / "README.md").write_text("[Missing](missing.md)")
    with pytest.raises(ValueError, match="broken documentation link"):
        MODULE.check(source)


def test_registry_package_version_cannot_lag_source(source):
    path = source / "server.json"
    registry = json.loads(path.read_text())
    registry["packages"][0]["version"] = "1.2.2"
    path.write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="registry packages must match"):
        MODULE.check(source)
