#!/usr/bin/env python3
"""Qualify both image targets on one disposable, bounded builder per architecture."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import uuid


# Resolve the manifest list once; never follow a moving builder image at runtime.
BUILDKIT = "moby/buildkit:buildx-stable-1@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea"
BUILD_TIMEOUT = 1200
SMOKE_TIMEOUT = 300
BUILD_CONFIG = """[worker.oci]
  max-parallelism = 2
  gc = true
  reservedSpace = "512MB"
  maxUsedSpace = "4GB"
  minFreeSpace = "2GB"
"""
SMOKE = "earnings-watch init && earnings-watch health && earnings-watch upcoming --format json"


class PhaseFailure(RuntimeError):
    def __init__(self, name, status):
        super().__init__(f"{name} exited {status}")
        self.status = status


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def progress_summary(path):
    """Keep raw BuildKit evidence; exporter time overlaps the measured command."""
    vertices = {}
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        # Buildx versions emit flat vertices or SolveStatus envelopes. Retain
        # partial updates by identity so later completion records keep the name.
        updates = row.get("vertexes", [row])
        if not isinstance(updates, list):
            continue
        for update in updates:
            if not isinstance(update, dict):
                continue
            identity = update.get("digest", update.get("id"))
            if isinstance(identity, str):
                vertices.setdefault(identity, {}).update(update)
    exporters = []
    for row in vertices.values():
        if (
            isinstance(row.get("name"), str)
            and row["name"].startswith("exporting")
            and row.get("started")
            and row.get("completed")
        ):
            try:
                start = datetime.fromisoformat(row["started"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(row["completed"].replace("Z", "+00:00"))
                seconds = (end - start).total_seconds()
            except (AttributeError, TypeError, ValueError):
                continue
            if seconds < 0:
                continue
            exporters.append({"name": row["name"], "seconds": seconds})
    return {
        "cached_vertices": sum(bool(row.get("cached")) for row in vertices.values()),
        "completed_vertices": sum(bool(row.get("completed")) for row in vertices.values()),
        "export_vertices": exporters,
    }


class Qualification:
    def __init__(self, root, arch, sha, reports):
        self.root, self.arch, self.sha = root, arch, sha
        self.reports = reports
        self.reports.mkdir(parents=True, exist_ok=True)
        self.report = reports / f"{arch}.json"
        key = hashlib.sha256(arch.encode() + BUILDKIT.encode())
        for name in ("Dockerfile", "pyproject.toml", "uv.lock"):
            key.update(name.encode() + b"\0" + (root / name).read_bytes())
        self.record = {
            "schema": 1,
            "source_sha": sha,
            "architecture": arch,
            "builder_image": BUILDKIT,
            "input_key": key.hexdigest(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "builder_cache": "fresh; reused only between serve and worker in this invocation",
            "phases": [],
            "cells": {},
            "result": "running",
        }
        self.environment = dict(os.environ)
        self.cancelled = None
        self.cleaning = False
        self.spawning = False

    def save(self):
        temporary = self.report.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.record, indent=2) + "\n")
        temporary.replace(self.report)

    def phase(self, name, command, timeout, required=True):
        log = self.reports / f"{self.arch}-{name}.log"
        started = time.monotonic()
        status = 127
        print(f"[{self.arch}/{name}] start (timeout {timeout}s)", flush=True)
        try:
            with log.open("w") as output:
                process = None
                try:
                    if self.cancelled and not self.cleaning:
                        raise self.cancelled
                    # Popen can spawn a detached child before returning its
                    # handle. Defer signal unwinding until ownership is recorded.
                    self.spawning = True
                    try:
                        process = subprocess.Popen(
                            command,
                            cwd=self.root,
                            env=self.environment,
                            stdout=output,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                    finally:
                        self.spawning = False
                    if self.cancelled and not self.cleaning:
                        raise self.cancelled
                    status = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    stop_process(process)
                    status = 124
                except BaseException as error:
                    if process is not None:
                        stop_process(process)
                    status = getattr(error, "status", 130)
                    raise
        except OSError as error:
            log.write_text(str(error) + "\n")
        finally:
            self.record["phases"].append(
                {"name": name, "seconds": time.monotonic() - started, "exit_status": status}
            )
            self.save()
        print(f"[{self.arch}/{name}] exit {status}; log {log}", flush=True)
        if required and status:
            print(log.read_text(errors="replace")[-4000:], flush=True)
            raise PhaseFailure(name, status)
        return log, status

    def run(self):
        token = uuid.uuid4().hex[:16]
        builder = f"earnings-qualification-{self.arch}-{token}"
        images, containers = [], []
        created = False
        failure = None
        self.save()
        # No host credentials, global builder selection or persistent source cache.
        with tempfile.TemporaryDirectory(prefix="earnings-images-") as directory:
            scratch = Path(directory)
            self.environment.update(
                DOCKER_CONFIG=str(scratch / "docker"), BUILDX_CONFIG=str(scratch / "buildx")
            )
            for variable in ("DOCKER_AUTH_CONFIG", "DOCKER_CONTEXT", "BUILDX_BUILDER"):
                self.environment.pop(variable, None)
            config = scratch / "buildkitd.toml"
            config.write_text(BUILD_CONFIG)
            try:
                # No privileged binfmt installer: worker emulation is a prerequisite.
                self.phase(
                    "builder-create",
                    [
                        "docker",
                        "buildx",
                        "create",
                        "--name",
                        builder,
                        "--driver",
                        "docker-container",
                        "--driver-opt",
                        f"image={BUILDKIT}",
                        "--driver-opt",
                        "memory=4g",
                        "--driver-opt",
                        "memory-swap=4g",
                        "--driver-opt",
                        "cpu-period=100000",
                        "--driver-opt",
                        "cpu-quota=200000",
                        "--buildkitd-config",
                        str(config),
                    ],
                    120,
                )
                created = True
                self.phase(
                    "builder-bootstrap",
                    ["docker", "buildx", "inspect", "--bootstrap", builder],
                    300,
                )
                for target in ("serve", "worker"):
                    image = f"earnings-qualification:{self.sha}-{target}-{self.arch}-{token}"
                    images.append(image)
                    archive = scratch / f"{target}.tar"
                    self.record["cells"][target] = {"result": "running"}
                    log, _ = self.phase(
                        f"{target}-build-export",
                        [
                            "docker",
                            "buildx",
                            "build",
                            "--builder",
                            builder,
                            "--platform",
                            f"linux/{self.arch}",
                            "--target",
                            target,
                            "--progress=rawjson",
                            "--tag",
                            image,
                            "--output",
                            f"type=docker,dest={archive}",
                            ".",
                        ],
                        BUILD_TIMEOUT,
                    )
                    self.record["cells"][target].update(progress_summary(log))
                    self.phase(f"{target}-load", ["docker", "load", "--input", str(archive)], 300)
                    archive.unlink()
                    architecture, _ = self.phase(
                        f"{target}-architecture",
                        ["docker", "image", "inspect", "--format", "{{.Architecture}}", image],
                        30,
                    )
                    if architecture.read_text().strip() != self.arch:
                        raise RuntimeError(
                            f"{target}: loaded image architecture differs from {self.arch}"
                        )
                    probes = [("state", "sh", ["-c", SMOKE])]
                    if target == "worker":
                        probes += [(cli, cli, ["--version"]) for cli in ("claude", "codex")]
                    else:
                        probes += [
                            (
                                "exclusions",
                                "sh",
                                ["-c", "! command -v claude && ! command -v codex"],
                            )
                        ]
                    for probe, entrypoint, arguments in probes:
                        container = f"{builder}-{target}-{probe}"
                        containers.append(container)
                        self.phase(
                            f"{target}-smoke-{probe}",
                            [
                                "docker",
                                "run",
                                "--rm",
                                "--name",
                                container,
                                "--platform",
                                f"linux/{self.arch}",
                                "--read-only",
                                "--network",
                                "none",
                                "--cap-drop",
                                "ALL",
                                "--security-opt",
                                "no-new-privileges",
                                "--tmpfs",
                                "/data:uid=1000,gid=1000,mode=0700",
                                "--tmpfs",
                                "/tmp",
                                "--entrypoint",
                                entrypoint,
                                image,
                                *arguments,
                            ],
                            SMOKE_TIMEOUT,
                        )
                        containers.remove(container)
                    self.record["cells"][target]["result"] = "passed"
                    self.save()
            except BaseException as error:
                failure = error
                self.record["result"] = "failed"
                self.record["error"] = str(error)
                for cell in self.record["cells"].values():
                    if cell["result"] == "running":
                        cell["result"] = "failed"
                self.save()
            finally:
                self.cleaning = True
                # --rm does not stop daemon containers when a timed-out client dies.
                for index, container in enumerate(containers):
                    # Already auto-removed containers are expected; do not mask a failed smoke.
                    self.phase(
                        f"cleanup-container-{index}",
                        ["docker", "rm", "--force", container],
                        30,
                        required=False,
                    )
                for image in images:
                    _, status = self.phase(
                        "cleanup-" + image.split("-")[-3],
                        ["docker", "image", "rm", "--force", image],
                        60,
                        required=False,
                    )
                    if status and failure is None:
                        failure = PhaseFailure("image cleanup", status)
                _, status = self.phase(
                    "builder-remove",
                    ["docker", "buildx", "rm", "--force", builder],
                    120,
                    required=False,
                )
                if status and created and failure is None:
                    failure = PhaseFailure("builder cleanup", status)
                failure = failure or self.cancelled
                self.record["result"] = "failed" if failure else "passed"
                if failure:
                    self.record["error"] = str(failure)
                self.save()
        if failure:
            raise failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    args = parser.parse_args()
    root = Path.cwd()
    sha = (
        os.environ.get("FLEET_SHA")
        or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    )
    if not re.fullmatch("[a-f0-9]{40}", sha):
        parser.error("Expected a full source SHA")
    qualification = Qualification(root, args.arch, sha, root / "reports/images")

    def interrupted(signum, _frame):
        # Repeated cancellation must not interrupt cleanup of owned resources.
        if qualification.cancelled is None:
            qualification.cancelled = PhaseFailure("interrupted", 128 + signum)
            if not qualification.cleaning and not qualification.spawning:
                raise qualification.cancelled

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        qualification.run()
    except PhaseFailure as error:
        raise SystemExit(error.status if 0 < error.status < 256 else 1) from error


if __name__ == "__main__":
    main()
