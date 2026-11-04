"""Run CI commands against a synthetic CLI; never require a Docker daemon."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_images", ROOT / "scripts/ci-images.py")
assert SPEC and SPEC.loader
IMAGES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGES)

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FAKE_CALLS'], 'a') as output:
    output.write(json.dumps({'args': args, 'docker_config': os.environ.get('DOCKER_CONFIG'),
                             'buildx_config': os.environ.get('BUILDX_CONFIG'),
                             'auth': os.environ.get('DOCKER_AUTH_CONFIG')}) + '\n')
if os.environ.get('BLOCK_PHASE') and args[:len(os.environ['BLOCK_PHASE'].split())] == os.environ['BLOCK_PHASE'].split():
    pathlib.Path(os.environ['FAKE_READY']).write_text(str(os.getpid()))
    import time
    time.sleep(60)
if args[:2] == ['image', 'rm'] and os.environ.get('SLOW_CLEANUP'):
    import time
    time.sleep(float(os.environ['SLOW_CLEANUP']))
if args[:2] == ['buildx', 'build']:
    target = args[args.index('--target') + 1]
    if os.environ.get('FAIL_PHASE') == target + '-build': sys.exit(19)
    destination = args[args.index('--output') + 1].split('dest=', 1)[1]
    pathlib.Path(destination).write_text('synthetic image')
    print(json.dumps({'id':'deps', 'name':'RUN uv sync', 'cached':target=='worker',
                      'completed':'2026-01-01T00:00:01Z'}))
    print(json.dumps({'id':'export', 'name':'exporting to docker image format',
                      'started':'2026-01-01T00:00:01Z', 'completed':'2026-01-01T00:00:03Z'}))
if args[:1] == ['load'] and os.environ.get('FAIL_PHASE') == 'load': sys.exit(20)
if args[:2] == ['image', 'inspect']:
    print('wrong' if os.environ.get('FAIL_PHASE') == 'architecture' else os.environ['FAKE_ARCH'])
if args[:1] == ['run']:
    entrypoint = args[args.index('--entrypoint') + 1]
    failure = os.environ.get('FAIL_PHASE')
    if failure == 'codex' and entrypoint == 'codex': sys.exit(21)
    if failure == 'exclusions' and 'command -v' in args[-1]: sys.exit(22)
if args[:2] == ['buildx', 'rm'] and os.environ.get('FAIL_PHASE') == 'builder-remove': sys.exit(23)
"""

FAKE_UV = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FAKE_CALLS'], 'a') as output: output.write(json.dumps(args) + '\n')
if 'pytest' in args:
    report = next(x.split('=',1)[1] for x in args if x.startswith('--junitxml='))
    pathlib.Path(report).write_text('<testsuite tests="1" failures="1"><testcase name="synthetic"><failure/></testcase></testsuite>')
if os.environ.get('FAIL_UV') and os.environ['FAIL_UV'] in args: sys.exit(17)
if args[:1] == ['build']:
    pathlib.Path('dist').mkdir(exist_ok=True)
    pathlib.Path('dist/synthetic-0.0.1.tar.gz').touch()
    if not os.environ.get('OMIT_WHEEL'): pathlib.Path('dist/synthetic-0.0.1-py3-none-any.whl').touch()
"""


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, source in (
            ("docker", FAKE_DOCKER),
            ("uv", FAKE_UV),
            ("git", "#!/bin/sh\nexit 0\n"),
        ):
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o755)
        (self.root / "Dockerfile").write_text("FROM synthetic\n")
        (self.root / "pyproject.toml").write_text('[project]\nversion="0.0.1"\n')
        (self.root / "uv.lock").write_text("synthetic")
        self.calls_path = self.root / "calls.jsonl"
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FAKE_CALLS": str(self.calls_path),
            "FAKE_ARCH": "amd64",
            "FLEET_SHA": "a" * 40,
            "FLEET_VERSION": "",
            "FLEET_REF": "main",
            "DOCKER_AUTH_CONFIG": "must not reach image commands",
        }
        for key in (
            "FAIL_PHASE",
            "FAIL_UV",
            "OMIT_WHEEL",
            "PYTEST_ADDOPTS",
            "BLOCK_PHASE",
            "SLOW_CLEANUP",
        ):
            self.env.pop(key, None)

    def invoke(self, script, *arguments, **environment):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *arguments],
            cwd=self.root,
            env={**self.env, **environment},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def calls(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def report(self, arch="amd64"):
        return json.loads((self.root / f"reports/images/{arch}.json").read_text())

    def test_progress_summary_reads_real_buildx_envelopes_and_partial_updates(self):
        log = self.root / "progress.log"
        log.write_text(
            "not json\n"
            + "\n".join(
                json.dumps(row)
                for row in [
                    {
                        "vertexes": [
                            {
                                "digest": "deps",
                                "name": "RUN uv sync",
                                "cached": True,
                                "completed": "2026-01-01T00:00:01Z",
                            },
                            {
                                "digest": "export",
                                "name": "exporting to docker image format",
                                "started": "2026-01-01T00:00:01Z",
                            },
                        ]
                    },
                    {"vertexes": [{"digest": "export", "completed": "2026-01-01T00:00:03Z"}]},
                    {"logs": [{"vertex": "export", "data": "synthetic"}]},
                    [],
                    {"vertexes": None},
                    {"vertexes": [None]},
                ]
            )
        )
        self.assertEqual(
            IMAGES.progress_summary(log),
            {
                "cached_vertices": 1,
                "completed_vertices": 2,
                "export_vertices": [{"name": "exporting to docker image format", "seconds": 2}],
            },
        )

    def test_both_platforms_build_once_per_target_and_preserve_all_smokes(self):
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                self.calls_path.unlink(missing_ok=True)
                result = self.invoke("ci-images.py", "--arch", arch, FAKE_ARCH=arch)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = self.calls()
                commands = [row["args"] for row in calls]
                builds = [row for row in commands if row[:2] == ["buildx", "build"]]
                self.assertEqual(
                    [row[row.index("--target") + 1] for row in builds], ["serve", "worker"]
                )
                self.assertEqual(len({row[row.index("--builder") + 1] for row in builds}), 1)
                smokes = [row for row in commands if row[0] == "run"]
                self.assertEqual(len(smokes), 5)
                self.assertEqual(
                    [row[row.index("--entrypoint") + 1] for row in smokes],
                    ["sh", "sh", "sh", "claude", "codex"],
                )
                self.assertIn("! command -v claude && ! command -v codex", smokes[1])
                for smoke in smokes:
                    for flag in ("--read-only", "--cap-drop", "--security-opt", "--tmpfs"):
                        self.assertIn(flag, smoke)
                    self.assertEqual(smoke[smoke.index("--platform") + 1], f"linux/{arch}")
                self.assertTrue(all(row["auth"] is None for row in calls))
                self.assertFalse(Path(calls[0]["docker_config"]).exists())
                self.assertFalse(any("--use" in row or "--push" in row for row in commands))
                receipt = self.report(arch)
                self.assertEqual(receipt["result"], "passed")
                self.assertEqual(
                    [cell["result"] for cell in receipt["cells"].values()], ["passed", "passed"]
                )
                self.assertEqual(receipt["cells"]["worker"]["cached_vertices"], 1)
                self.assertEqual(receipt["cells"]["serve"]["export_vertices"][0]["seconds"], 2)

    def test_failed_cli_smoke_propagates_and_cleans_owned_resources(self):
        result = self.invoke("ci-images.py", "--arch", "amd64", FAIL_PHASE="codex")
        self.assertEqual(result.returncode, 21)
        receipt = self.report()
        self.assertEqual(receipt["result"], "failed")
        self.assertEqual(receipt["cells"]["serve"]["result"], "passed")
        self.assertNotEqual(receipt["cells"]["worker"]["result"], "passed")
        commands = [row["args"] for row in self.calls()]
        self.assertEqual(commands[-1][:3], ["buildx", "rm", "--force"])
        removed = [row[-1] for row in commands if row[:2] == ["image", "rm"]]
        self.assertEqual(len(removed), 2)
        self.assertTrue(all(ref.startswith("earnings-qualification:") for ref in removed))

    def test_exclusion_failure_prevents_worker_build(self):
        result = self.invoke("ci-images.py", "--arch", "amd64", FAIL_PHASE="exclusions")
        self.assertEqual(result.returncode, 22)
        self.assertEqual(list(self.report()["cells"]), ["serve"])

    def test_cancellation_finishes_owned_cleanup_and_records_failure(self):
        for phase in ("buildx build", "load", "run"):
            with self.subTest(phase=phase):
                ready = self.root / "ready"
                ready.unlink(missing_ok=True)
                self.calls_path.unlink(missing_ok=True)
                process = subprocess.Popen(
                    [sys.executable, str(ROOT / "scripts/ci-images.py"), "--arch", "amd64"],
                    cwd=self.root,
                    env={
                        **self.env,
                        "BLOCK_PHASE": phase,
                        "FAKE_READY": str(ready),
                        "SLOW_CLEANUP": "0.3",
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists():
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline, "Fake command did not start")
                        time.sleep(0.01)
                    process.send_signal(signal.SIGTERM)
                    output, _ = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 143, output)
                    self.assertEqual(self.report()["result"], "failed")
                    commands = [row["args"] for row in self.calls()]
                    self.assertTrue(any(row[:2] == ["image", "rm"] for row in commands))
                    self.assertEqual(commands[-1][:3], ["buildx", "rm", "--force"])
                    status = Path("/proc") / ready.read_text() / "stat"
                    self.assertTrue(
                        not status.exists() or status.read_text().split()[2] in ("Z", "X")
                    )
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()

    def test_cancellation_during_spawn_reaps_child_before_owned_cleanup(self):
        # Inject the signal after a real detached child starts, before Popen
        # returns its handle. The runner must own/reap it before unwinding.
        harness = self.root / "spawn-cancellation.py"
        child_pid = self.root / "spawn-child.pid"
        harness.write_text(
            f"""import importlib.util, os, pathlib, signal, subprocess, sys
spec = importlib.util.spec_from_file_location('images', {str(ROOT / "scripts/ci-images.py")!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = subprocess.Popen
launched = []
def spawn(command, **kwargs):
    if not launched:
        child = original([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        launched.append(child)
        pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
        return child
    child = original(command, **kwargs)
    if command[:3] == ['docker', 'buildx', 'rm']:
        os.kill(os.getpid(), signal.SIGINT)
    return child
module.subprocess.Popen = spawn
sys.argv = ['ci-images.py', '--arch', 'amd64']
try:
    module.main()
finally:
    child = launched[0]
    reaped = child.returncode is not None
    if not reaped:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    assert reaped, 'Cancellation left the detached startup child running or unreaped'
"""
        )
        result = subprocess.run(
            [sys.executable, str(harness)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        receipt = self.report()
        self.assertEqual(receipt["result"], "failed")
        self.assertEqual(receipt["phases"][0]["exit_status"], 143)
        self.assertEqual(receipt["phases"][-1]["name"], "builder-remove")
        self.assertEqual(self.calls()[-1]["args"][:3], ["buildx", "rm", "--force"])
        self.assertFalse((Path("/proc") / child_pid.read_text()).exists())

    def test_build_load_architecture_and_cleanup_failures_cannot_pass(self):
        for failure in ("serve-build", "load", "architecture", "builder-remove"):
            with self.subTest(failure=failure):
                result = self.invoke("ci-images.py", "--arch", "amd64", FAIL_PHASE=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.report()["result"], "failed")
                if failure != "builder-remove":
                    self.assertFalse(
                        any(
                            row["name"].startswith("serve-smoke") for row in self.report()["phases"]
                        )
                    )

    def test_phase_timeout_kills_client_and_records_failure(self):
        qualification = IMAGES.Qualification(self.root, "amd64", "a" * 40, self.root / "reports")
        with self.assertRaises(IMAGES.PhaseFailure) as failure:
            qualification.phase("slow", [sys.executable, "-c", "import time; time.sleep(10)"], 0.05)
        self.assertEqual(failure.exception.status, 124)
        phase = qualification.record["phases"][-1]
        self.assertEqual(phase["exit_status"], 124)
        self.assertLess(phase["seconds"], 3)

    def test_check_preserves_failed_pytest_junit_and_fails_make(self):
        result = subprocess.run(
            ["make", "-f", str(ROOT / "Makefile"), "ci-check"],
            cwd=self.root,
            env={**self.env, "FAIL_UV": "pytest", "MAKEFILES": str(ROOT / "Makefile")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failure", (self.root / "reports/junit/check.xml").read_text())
        self.assertFalse((self.root / "dist").exists())

    def test_bootstrap_failure_stops_before_project_checks(self):
        result = subprocess.run(
            ["make", "-f", str(ROOT / "Makefile"), "ci-check"],
            cwd=self.root,
            env={**self.env, "FAIL_UV": "sync"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [["sync", "--locked"]])

    def test_package_tag_mismatch_fails_before_build(self):
        for tag in ("0.0.2", "malformed"):
            result = self.invoke("ci-package.py", FLEET_VERSION=tag)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.calls_path.exists())

    def test_package_manual_and_tag_builds_retain_distributions(self):
        for ref in ("main", "version-work", "v0.0.1"):
            with self.subTest(ref=ref):
                result = self.invoke("ci-package.py", FLEET_REF=ref)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(list((self.root / "dist").glob("*.whl")))
                self.assertTrue(list((self.root / "dist").glob("*.tar.gz")))

    def test_package_build_failure_and_missing_output_cannot_use_stale_wheel(self):
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "stale.whl").touch()
        result = self.invoke("ci-package.py", FAIL_UV="build")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(dist.glob("*.whl")))
        result = self.invoke("ci-package.py", OMIT_WHEEL="1")
        self.assertNotEqual(result.returncode, 0)

    def test_input_key_separates_architectures_and_dependency_changes(self):
        def key(arch):
            return IMAGES.Qualification(self.root, arch, "a" * 40, self.root / "reports").record[
                "input_key"
            ]

        original = key("amd64")
        self.assertNotEqual(original, key("arm64"))
        (self.root / "uv.lock").write_text("changed dependency")
        self.assertNotEqual(original, key("amd64"))


if __name__ == "__main__":
    unittest.main()
