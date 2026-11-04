"""Immutable identity supplied by the package build, without runtime discovery."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json
import os
import re
from uuid import uuid4

SERVICE = "earnings-watch"
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class BuildIdentity:
    service: str
    version: str
    revision: str

    @property
    def short_revision(self) -> str:
        return self.revision[:12] if self.revision != "unknown" else "unknown"

    def as_dict(self) -> dict[str, str]:
        return {"service": self.service, "version": self.version, "revision": self.revision}


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate build identity field")
        value[name] = item
    return value


def _load_build() -> BuildIdentity:
    unknown = BuildIdentity(SERVICE, "unknown", "unknown")
    try:
        with files("earnings_watch").joinpath("build_identity.json").open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            return unknown
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "service", "version", "revision"}
            or type(value["schema"]) is not int
            or value["schema"] != 1
            or value["service"] != SERVICE
            or not isinstance(value["version"], str)
            or not _VERSION.fullmatch(value["version"])
            or not isinstance(value["revision"], str)
            or not _REVISION.fullmatch(value["revision"])
        ):
            return unknown
        return BuildIdentity(SERVICE, value["version"], value["revision"])
    except (OSError, UnicodeError, ValueError, TypeError, ImportError, RecursionError):
        return unknown


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
