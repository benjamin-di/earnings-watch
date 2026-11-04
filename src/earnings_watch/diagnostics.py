"""Attribute owned diagnostics after existing formatting and redaction."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import io
import json
import logging
import sys
import threading
from typing import Any, Iterator
import warnings

from .build_identity import BUILD, process_instance


def _fields() -> dict[str, str]:
    return {**BUILD.as_dict(), "instance": process_instance()}


def _with_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep structured values, including native fields with reserved names."""
    result = dict(payload)
    for key, value in _fields().items():
        if key in result:
            displaced = key + "_"
            while displaced in result:
                displaced += "_"
            result[displaced] = result[key]
        result[key] = value
    return result


def _attribute(rendered: str, record: logging.LogRecord, stream: Any = None) -> str:
    try:
        payload = json.loads(rendered)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        return json.dumps(_with_identity(payload), ensure_ascii=False)
    if stream is not None and getattr(stream, "isatty", lambda: False)():
        header = " ".join(f"{key}={value}" for key, value in _fields().items())
        return "".join(f"{header} {line}" for line in rendered.splitlines(keepends=True)) or (
            header + " "
        )
    return json.dumps(
        _with_identity({"message": rendered, "level": record.levelname, "logger": record.name}),
        ensure_ascii=False,
    )


def _is_rich(handler: logging.Handler) -> bool:
    rich_handler = getattr(sys.modules.get("rich.logging"), "RichHandler", None)
    return rich_handler is not None and isinstance(handler, rich_handler)


class _RichConsole:
    """Render one record before delivery through its existing console context."""

    def __init__(self, console: Any, record: logging.LogRecord):
        self.console, self.record = console, record

    def __getattr__(self, name: str) -> Any:
        return getattr(self.console, name)

    def print(self, renderable: Any) -> None:
        from rich.segment import Segment, Segments

        console = self.console
        lines = console.render_lines(renderable, console.options, pad=False, new_lines=True)
        if not lines:
            return
        if console.is_terminal or getattr(console.file, "isatty", lambda: False)():
            header = " ".join(f"{key}={value}" for key, value in _fields().items()) + " "
            retained = Segments([segment for line in lines for segment in [Segment(header), *line]])
            console.print(
                retained,
                end="",
                markup=False,
                highlight=False,
                emoji=False,
                no_wrap=True,
                overflow="ignore",
                crop=False,
            )
        else:
            rendered = "".join(
                segment.text for line in lines for segment in line if not segment.control
            )
            console.out(_attribute(rendered, self.record), highlight=False)


def configure_logging() -> None:
    """Retain native sinks, filters and levels, including handlers installed later.

    Standard handlers render through Handler.format. Rich renders its traceback
    afterwards, so attribute its completed renderable through the same console.
    Neither application streams nor child output are
    wrapped. Embedding applications keep their own exception hooks.
    """
    if not getattr(logging.Handler.format, "_earnings_identity", False):
        original_format = logging.Handler.format
        original_handle = logging.Handler.handle

        def format_record(handler: logging.Handler, record: logging.LogRecord) -> str:
            rendered = original_format(handler, record)
            if _is_rich(handler):
                return rendered
            return _attribute(rendered, record, getattr(handler, "stream", None))

        def handle_record(handler: logging.Handler, record: logging.LogRecord):
            if not _is_rich(handler):
                return original_handle(handler, record)
            # Filters retain their original console; the native emit boundary
            # retains authority over delivery failures and custom handleError.
            handler.acquire()
            original_emit = handler.emit
            had_emit_override = "emit" in handler.__dict__

            def emit_record(effective: logging.LogRecord) -> None:
                console = handler.console  # type: ignore[attr-defined]
                handler.console = _RichConsole(console, effective)  # type: ignore[attr-defined]
                try:
                    original_emit(effective)
                finally:
                    handler.console = console  # type: ignore[attr-defined]

            try:
                setattr(handler, "emit", emit_record)
                return original_handle(handler, record)
            finally:
                if had_emit_override:
                    setattr(handler, "emit", original_emit)
                else:
                    delattr(handler, "emit")
                handler.release()

        setattr(format_record, "_earnings_identity", True)
        setattr(logging.Handler, "format", format_record)
        setattr(logging.Handler, "handle", handle_record)
    # Respect an embedding application's configuration. A bare executable gets
    # the same default WARNING threshold as standard logging.
    logging.basicConfig(format="%(message)s")


def write_error(envelope: dict[str, Any]) -> None:
    """Keep CLI error envelopes machine-readable without logging raw inputs."""
    print(json.dumps(_with_identity(envelope), ensure_ascii=False), file=sys.stderr)


@contextmanager
def executable_diagnostics() -> Iterator[None]:
    """Sanitize executable/thread failures without changing an embedding host."""
    previous = sys.excepthook
    previous_thread = threading.excepthook
    previous_async = asyncio.BaseEventLoop.default_exception_handler
    previous_warning = warnings.showwarning

    def uncaught(error_type, error, traceback):
        logging.getLogger("earnings_watch").error("Uncaught failure (%s)", error_type.__name__)

    def thread_failure(args):
        if args.exc_type is not SystemExit:
            uncaught(args.exc_type, None, None)

    def async_failure(loop, context):
        error = context.get("exception")
        logging.getLogger("asyncio").error(
            "Unhandled asynchronous failure (%s)",
            type(error).__name__ if error is not None else "unknown",
        )

    def show_warning(message, category, filename, lineno, file=None, line=None):
        # Keep native warning filtering/formatting and the requested sink. An
        # existing host showwarning hook remains its owner's responsibility.
        captured = io.StringIO()
        previous_warning(message, category, filename, lineno, file=captured, line=line)
        rendered = captured.getvalue()
        if rendered:
            stream = file if file is not None else sys.stderr
            record = logging.LogRecord(
                "py.warnings", logging.WARNING, filename, lineno, "", (), None
            )
            retained = _attribute(rendered, record, stream)
            stream.write(retained + ("" if retained.endswith("\n") else "\n"))
            stream.flush()

    sys.excepthook = uncaught
    threading.excepthook = thread_failure
    setattr(asyncio.BaseEventLoop, "default_exception_handler", async_failure)
    if previous_warning is getattr(warnings, "_showwarning_orig", None):
        warnings.showwarning = show_warning
    try:
        yield
    finally:
        if sys.excepthook is uncaught:
            sys.excepthook = previous
        if threading.excepthook is thread_failure:
            threading.excepthook = previous_thread
        if asyncio.BaseEventLoop.default_exception_handler is async_failure:
            setattr(asyncio.BaseEventLoop, "default_exception_handler", previous_async)
        if warnings.showwarning is show_warning:
            warnings.showwarning = previous_warning
