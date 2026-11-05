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


def _native_console_dispatch(console: Any, render: Any, hook: Any) -> Any:
    """Scope render provenance to this thread without replacing print dispatch."""
    from contextlib import contextmanager
    from threading import local

    @contextmanager
    def dispatch() -> Any:
        key = "_build_identity_render_dispatch"
        with console._lock:
            state = vars(console).get(key)
            if state is None:
                missing = object()
                state = {
                    "local": local(),
                    "users": 0,
                    "render": console.render,
                    "saved_render": vars(console).get("render", missing),
                    "missing": missing,
                    "hooks": console._render_hooks,
                }

                def current() -> Any:
                    stack = getattr(state["local"], "stack", ())
                    return stack[-1] if stack else None

                def dispatch_render(*args: Any, **kwargs: Any) -> Any:
                    context = current()
                    if context is None:
                        yield from state["render"](*args, **kwargs)
                        return
                    stack = state["local"].stack
                    stack.append(None)
                    try:
                        yield from context[0](*args, **kwargs)
                    finally:
                        stack.pop()

                class DispatchHook:
                    def __init__(self, original: Any) -> None:
                        self.original = original

                    def process_renderables(self, renderables: list[Any]) -> list[Any]:
                        context = current()
                        target = self.original
                        if context is not None:
                            target = context[1](target)
                        return target.process_renderables(renderables)

                class DispatchHooks:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(state["hooks"], name)

                    def __getitem__(self, index: Any) -> Any:
                        items = state["hooks"][index]
                        if current() is None:
                            return items
                        if isinstance(index, slice):
                            return [DispatchHook(item) for item in items]
                        return DispatchHook(items)

                    def __len__(self) -> int:
                        return len(state["hooks"])

                    def __iter__(self) -> Any:
                        return iter(self[:])

                state["dispatch_render"] = dispatch_render
                state["dispatch_hooks"] = DispatchHooks()
                console.render = dispatch_render
                console._render_hooks = state["dispatch_hooks"]
                vars(console)[key] = state
            state["users"] += 1
            stack = getattr(state["local"], "stack", None)
            if stack is None:
                stack = state["local"].stack = []
            stack.append((render, hook))
        try:
            yield
        finally:
            with console._lock:
                stack.pop()
                state["users"] -= 1
                if not state["users"]:
                    if console.render is state["dispatch_render"]:
                        if state["saved_render"] is state["missing"]:
                            del console.render
                        else:
                            console.render = state["saved_render"]
                    if console._render_hooks is state["dispatch_hooks"]:
                        console._render_hooks = state["hooks"]
                    del vars(console)[key]

    return dispatch()


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

    def render(renderable: Any, options: Any = None):
        yield marker(id(renderable) not in host_ids)
        yield from console.render(renderable, options)
        yield marker(True)

    with console:
        # Rich keeps a current-thread segment buffer. Isolate only this print;
        # local zero-width markers survive native style/crop, then are removed.
        prefix = console._buffer[:]
        console._buffer.clear()
        try:
            with _native_console_dispatch(console, render, Hook):
                console.print(*objects, **kwargs)
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
        object.__setattr__(self, "_identity_console", console)
        object.__setattr__(self, "_identity_record", record)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._identity_console, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._identity_console, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._identity_console, name)

    def print(self, *objects: Any, **kwargs: Any) -> None:
        from rich.segment import Segment

        console = self._identity_console
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
                        console._buffer.append(
                            Segment(_attribute(rendered, self._identity_record) + "\n")
                        )


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


def failure_details(error: BaseException | None, trace=None) -> dict[str, Any]:
    import traceback
    from pathlib import Path

    return {
        "error_type": type(error).__name__ if error is not None else "unknown",
        "traceback": [
            {
                "file": Path(frame.f_code.co_filename).name,
                "function": frame.f_code.co_name,
                "line": line,
            }
            for frame, line in traceback.walk_tb(
                trace if trace is not None else getattr(error, "__traceback__", None)
            )
        ],
    }


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
        logging.getLogger("earnings_watch").error(
            json.dumps(
                {
                    "message": f"Uncaught failure ({error_type.__name__})",
                    "logger": "earnings_watch",
                    "level": "ERROR",
                    **failure_details(error, traceback),
                }
            )
        )

    def thread_failure(args):
        if args.exc_type is not SystemExit:
            uncaught(args.exc_type, args.exc_value, args.exc_traceback)

    def async_failure(loop, context):
        error = context.get("exception")
        logging.getLogger("asyncio").error(
            json.dumps(
                {
                    "message": "Unhandled asynchronous failure",
                    "logger": "asyncio",
                    "level": "ERROR",
                    **failure_details(error),
                }
            )
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
