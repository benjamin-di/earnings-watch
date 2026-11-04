"""Verified public facts with optional personal projections."""

from typing import TYPE_CHECKING

from .build_identity import BUILD

if TYPE_CHECKING:
    from .composition import open_service as open
    from .settings import Settings


def __getattr__(name: str):
    # Admit the stdlib-only package identity before importing optional runtime
    # dependencies, so executables can report a quiet attributed import failure.
    if name == "open":
        from .composition import open_service

        return open_service
    if name == "Settings":
        from .settings import Settings

        return Settings
    raise AttributeError(name)


__all__ = ["open", "Settings"]
__version__ = BUILD.version
