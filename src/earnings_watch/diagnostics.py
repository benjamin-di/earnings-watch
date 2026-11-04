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


@contextmanager
def _native_print_segments(console: Any, objects: tuple[Any, ...], kwargs: dict[str, Any]):
    """Keep native print policy and separate Live's host decorations from the log."""
    from rich.control import Control, ControlType
    from rich.live import Live
    from rich.live_render import LiveRender
    from rich.segment import Segment

    host_ids: set[int] = set()
    markers: dict[int, tuple[Any, bool]] = {}

    class Hook:
        def __init__(self, original: Any):
            self.original = original

        def process_renderables(self, renderables: list[Any]) -> list[Any]:
            incoming = {id(item) for item in renderables}
            result = self.original.process_renderables(renderables)
            if isinstance(self.original, Live):
                host_ids.update(
                    id(item)
                    for item in result
                    if id(item) not in incoming and isinstance(item, (Control, LiveRender))
                )
            return result

    def marker(owned: bool) -> Any:
        token = tuple([(ControlType.BELL,)])
        markers[id(token)] = (token, owned)
        return Segment("", None, token)

    def render(proxy: Any, renderable: Any, options: Any = None):
        yield marker(id(renderable) not in host_ids)
        yield from console.render(renderable, options)
        yield marker(True)

    def get_attribute(proxy: Any, name: str) -> Any:
        if name == "render":
            return object.__getattribute__(proxy, name)
        if name == "_render_hooks":
            return [Hook(hook) for hook in console._render_hooks]
        return getattr(console, name)

    console_type: Any = type(console)
    proxy_type = type(
        "NativePrintConsole",
        (console_type,),
        {
            "__getattribute__": get_attribute,
            "__setattr__": lambda proxy, name, value: setattr(console, name, value),
            "__enter__": lambda proxy: console.__enter__(),
            "__exit__": lambda proxy, *exc: console.__exit__(*exc),
            "render": render,
        },
    )
    with console:
        # Rich keeps a current-thread segment buffer. Isolate only this print;
        # local zero-width markers survive native style/crop, then are removed.
        prefix = console._buffer[:]
        console._buffer.clear()
        try:
            console_type.print(object.__new__(proxy_type), *objects, **kwargs)
            raw = console._buffer[:]
        finally:
            console._buffer[:] = prefix
        groups: list[tuple[bool, list[Any]]] = []
        owned = True
        for segment in raw:
            match = markers.get(id(segment.control))
            if match is not None:
                owned = match[1]
            elif groups and groups[-1][0] == owned:
                groups[-1][1].append(segment)
            else:
                groups.append((owned, [segment]))
        yield groups


class _RichConsole:
    """Render one record before delivery through its existing console context."""

    def __init__(self, console: Any, record: logging.LogRecord):
        self.console, self.record = console, record

    def __getattr__(self, name: str) -> Any:
        return getattr(self.console, name)

    def print(self, *objects: Any, **kwargs: Any) -> None:
        from rich.segment import Segment

        console = self.console
        with _native_print_segments(console, objects, kwargs) as groups:
            for owned, segments in groups:
                if not owned:
                    console._buffer.extend(segments)
                elif console.is_terminal or getattr(console.file, "isatty", lambda: False)():
                    header = " ".join(f"{key}={value}" for key, value in _fields().items()) + " "
                    for line, terminated in Segment.split_lines_terminator(segments):
                        console._buffer.append(Segment(header))
                        console._buffer.extend(line)
                        if terminated:
                            console._buffer.append(Segment.line())
                else:
                    prefix = console._buffer[:]
                    console._buffer[:] = segments
                    try:
                        with console.capture() as captured:
                            pass
                        rendered = captured.get()
                    finally:
                        console._buffer[:] = prefix
                    if rendered:
                        console._buffer.append(Segment(_attribute(rendered, self.record) + "\n"))


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
