"""Immutable identity supplied by the package build, without runtime discovery."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path
import json
import os
import re
import tomllib
from uuid import uuid4

SERVICE = "earnings-watch"
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True)
class BuildIdentity:
    service: str
    version: str
    source_revision: str

    @property
    def short_revision(self) -> str:
        return self.source_revision[:12] if self.source_revision != "unknown" else "unknown"

    def as_dict(self) -> dict[str, str]:
        return {
            "service": self.service,
            "version": self.version,
            "source_revision": self.source_revision,
        }


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate build identity field")
        value[name] = item
    return value


def _load_build() -> BuildIdentity:
    try:
        installed = distribution(SERVICE)
        product_version = installed.version
    except PackageNotFoundError:
        installed = None
        try:
            project = tomllib.loads(
                Path(__file__).parents[2].joinpath("pyproject.toml").read_text(encoding="utf-8")
            )["project"]
            product_version = project["version"]
            if project["name"] != SERVICE or not isinstance(product_version, str):
                raise ValueError("Invalid source product metadata")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("Unavailable application product metadata") from exc
    if not _VERSION.fullmatch(product_version):
        raise ValueError("Invalid application product version")
    try:
        with files("earnings_watch").joinpath("build_identity.json").open("rb") as stream:
            raw = stream.read(16385)
    except FileNotFoundError:
        if installed is not None:
            try:
                direct = json.loads(installed.read_text("direct_url.json") or "{}")
            except (TypeError, ValueError):
                direct = None
            if not (
                isinstance(direct, dict)
                and isinstance(direct.get("dir_info"), dict)
                and direct["dir_info"].get("editable") is True
            ):
                raise ValueError(
                    "Missing application build identity in installed package"
                ) from None
        return BuildIdentity(SERVICE, product_version, "unknown")
    except OSError as exc:
        raise ValueError("Unavailable application build identity") from exc
    try:
        if len(raw) > 16384:
            raise ValueError("Oversized build identity")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "service", "version", "source_revision"}
            or type(value["schema"]) is not int
            or value["schema"] != 1
            or value["service"] != SERVICE
            or value["version"] != product_version
            or not isinstance(value["source_revision"], str)
            or not _REVISION.fullmatch(value["source_revision"])
        ):
            raise ValueError("Application build identity conflicts with the installed package")
        return BuildIdentity(SERVICE, product_version, value["source_revision"])
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Application build identity conflicts with the installed package") from exc


BUILD = _load_build()
_PID = os.getpid()
_INSTANCE = str(uuid4())


def _after_fork() -> None:
    global _PID, _INSTANCE
    _PID = os.getpid()
    _INSTANCE = str(uuid4())


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def process_instance() -> str:
    """One eagerly admitted instance per process, refreshed after a fork."""
    if os.getpid() != _PID:
        _after_fork()
    return _INSTANCE
