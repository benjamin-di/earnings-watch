"""Keep real Rich console policy authoritative through configured logging."""

import io
import logging
import threading
import unittest

from rich.console import Console
from rich.logging import RichHandler

from earnings_watch import diagnostics as logs


class DeliveryFailure(Exception):
    pass


class NativeHandler(RichHandler):
    def __init__(self, console):
        super().__init__(
            console=console, show_time=False, show_level=False, show_path=False, highlighter=None
        )
        self.errors = []

    def handleError(self, record):
        self.errors.append(record)


def record():
    return logging.LogRecord(
        "synthetic.delivery", logging.INFO, __file__, 1, "retained diagnostic", (), None
    )


def adapter(original):
    logs.configure_logging()
    return original, []


class RichDeliveryContractTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        native_format = logging.Handler.format
        native_handle = logging.Handler.handle
        handlers = logging.root.handlers[:]
        self.addCleanup(setattr, logging.Handler, "format", native_format)
        self.addCleanup(setattr, logging.Handler, "handle", native_handle)
        self.addCleanup(setattr, logging.root, "handlers", handlers)
        fields = patch.object(
            logs,
            "_fields",
            return_value={
                "service": "synthetic-service",
                "version": "1.2.3",
                "revision": "a" * 40,
                "instance": "synthetic-instance",
            },
        )
        fields.start()
        self.addCleanup(fields.stop)

    def test_native_console_state_reads_writes_and_deletes_are_preserved(self):
        sink = io.StringIO()
        console = Console(file=sink, record=False, force_terminal=False)
        console.synthetic_policy = "retained"
        seen = []

        class StatefulHandler(NativeHandler):
            def emit(self, effective):
                seen.append(self.console.record)
                self.console.quiet = True
                del self.console.synthetic_policy
                super().emit(effective)

        original = StatefulHandler(console)
        wrapped, errors = adapter(original)
        wrapped.handle(record())
        self.assertEqual(seen, [False])
        self.assertTrue(console.quiet)
        self.assertFalse(hasattr(console, "synthetic_policy"))
        self.assertEqual(sink.getvalue(), "")
        self.assertEqual(original.errors, [])
        self.assertEqual(errors, [])
        self.assertIs(original.console, console)

    def test_native_error_recovery_can_replace_console_file(self):
        initial, fallback = io.StringIO(), io.StringIO()
        console = Console(file=initial, force_terminal=False)

        class RecoveringHandler(NativeHandler):
            def emit(self, effective):
                self.handleError(effective)

            def handleError(self, effective):
                self.errors.append(effective)
                self.console.file = fallback
                self.console.print("recovered diagnostic", markup=False, highlight=False)

        original = RecoveringHandler(console)
        wrapped, errors = adapter(original)
        wrapped.handle(record())
        self.assertEqual(initial.getvalue(), "")
        self.assertIn("recovered diagnostic", fallback.getvalue())
        self.assertIs(console.file, fallback)
        self.assertEqual(len(original.errors), 1)
        self.assertEqual(errors, [])
        self.assertIs(original.console, console)

    def test_effective_instance_print_redaction_and_suppression_are_preserved(self):
        from functools import partial
        from types import MethodType

        for callable_kind in ("bound", "closure", "partial"):
            for suppress in (False, True):
                with self.subTest(callable_kind=callable_kind, suppress=suppress):
                    sink = io.StringIO()
                    console = Console(file=sink, force_terminal=False)
                    native_print = console.print
                    calls = []

                    def redact(
                        *objects,
                        calls=calls,
                        suppress=suppress,
                        native_print=native_print,
                        **kwargs,
                    ):
                        calls.append(objects)
                        if not suppress:
                            native_print("redacted diagnostic", **kwargs)

                    def bound_redact(receiver, *objects, console=console, redact=redact, **kwargs):
                        self.assertIs(receiver, console)
                        return redact(*objects, **kwargs)

                    console.print = (
                        MethodType(bound_redact, console)
                        if callable_kind == "bound"
                        else partial(redact)
                        if callable_kind == "partial"
                        else redact
                    )
                    effective_print = console.print
                    original = NativeHandler(console)
                    wrapped, errors = adapter(original)
                    hooks = console._render_hooks
                    with console.capture() as captured:
                        wrapped.handle(record())
                    output = captured.get()
                    self.assertEqual(len(calls), 1)
                    self.assertNotIn("retained diagnostic", output)
                    if suppress:
                        self.assertEqual(output, "")
                    else:
                        self.assertIn("redacted diagnostic", output)
                        self.assertGreater(len(output), len("redacted diagnostic\n"))
                    self.assertEqual(sink.getvalue(), "")
                    self.assertEqual(original.errors, [])
                    self.assertEqual(errors, [])
                    self.assertIs(console.print, effective_print)
                    self.assertIs(console._render_hooks, hooks)
                    self.assertNotIn("render", vars(console))
                    self.assertNotIn("_build_identity_render_dispatch", vars(console))

    def test_effective_unbound_print_keeps_live_host_output_unattributed(self):
        from rich.live import Live

        sink = io.StringIO()
        console = Console(
            file=sink,
            width=80,
            force_terminal=True,
            force_interactive=True,
            color_system="standard",
            _environ={"TERM": "xterm"},
        )
        native_print = console.print

        def effective_print(*objects, **kwargs):
            return native_print(*objects, **kwargs)

        console.print = effective_print
        original = NativeHandler(console)
        wrapped, errors = adapter(original)
        hooks = console._render_hooks
        live = Live(
            "host status",
            console=console,
            auto_refresh=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        live.start(refresh=True)
        try:
            sink.seek(0)
            sink.truncate()
            wrapped.handle(record())
            emitted = sink.getvalue()
        finally:
            live.stop()
        self.assertTrue(emitted.startswith("\r\x1b[2K"), repr(emitted))
        self.assertTrue(emitted.endswith("host status"), repr(emitted))
        self.assertEqual(emitted.count("host status"), 1)
        self.assertIn("retained diagnostic", emitted)
        self.assertGreater(len(emitted.split("retained diagnostic")[0]), len("\r\x1b[2K"))
        self.assertEqual(original.errors, [])
        self.assertEqual(errors, [])
        self.assertIs(console._render_hooks, hooks)
        self.assertNotIn("render", vars(console))

    def test_two_handlers_share_console_without_cross_thread_attribution(self):
        sink = io.StringIO()
        console = Console(file=sink, force_terminal=False, width=80)
        native_print = console.print
        first_entered, second_entered = threading.Event(), threading.Event()
        outputs, failures = {}, []

        def effective_print(*objects, **kwargs):
            name = threading.current_thread().name
            if name == "first-record":
                first_entered.set()
                if not second_entered.wait(2):
                    raise DeliveryFailure("second print could not enter")
            elif name == "second-record":
                second_entered.set()
            return native_print(*objects, **kwargs)

        console.print = effective_print
        originals = [NativeHandler(console), NativeHandler(console)]
        wrapped = [adapter(original)[0] for original in originals]

        def emit(index):
            try:
                with console.capture() as captured:
                    native_print("host prefix", highlight=False)
                    wrapped[index].handle(record())
                    native_print("host suffix", highlight=False)
                outputs[index] = captured.get()
            except (AssertionError, DeliveryFailure, OSError, RuntimeError) as error:
                failures.append(error)

        workers = [
            threading.Thread(target=emit, args=(index,), name=name)
            for index, name in enumerate(("first-record", "second-record"))
        ]
        workers[0].start()
        try:
            self.assertTrue(first_entered.wait(2))
            workers[1].start()
        finally:
            for worker in workers:
                if worker.ident is not None:
                    worker.join(3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(set(outputs), {0, 1})
        for output in outputs.values():
            self.assertTrue(output.startswith("host prefix\n"), repr(output))
            self.assertTrue(output.endswith("host suffix\n"), repr(output))
            self.assertEqual(output.count("retained diagnostic"), 1)
            self.assertGreater(len(output), len("host prefix\nretained diagnostic\nhost suffix\n"))
        self.assertEqual([original.errors for original in originals], [[], []])
        self.assertNotIn("render", vars(console))
        self.assertNotIn("_build_identity_render_dispatch", vars(console))

    def test_print_override_can_install_a_live_hook_before_native_print(self):
        from rich.live import Live

        class LateLiveConsole(Console):
            def print(self, *objects, **kwargs):
                live = Live(
                    "host status",
                    console=self,
                    auto_refresh=False,
                    redirect_stdout=False,
                    redirect_stderr=False,
                )
                self.push_render_hook(live)
                try:
                    return super().print(*objects, **kwargs)
                finally:
                    self.pop_render_hook()

        sink = io.StringIO()
        console = LateLiveConsole(
            file=sink,
            width=80,
            force_terminal=True,
            force_interactive=True,
            color_system="standard",
            _environ={"TERM": "xterm"},
            record=True,
        )
        hooks = console._render_hooks
        original = NativeHandler(console)
        wrapped, errors = adapter(original)
        wrapped.handle(record())
        self.assertEqual(sink.getvalue().splitlines()[-1], "host status")
        self.assertGreater(len(sink.getvalue().split("retained diagnostic")[0]), len("\r\x1b[2K"))
        self.assertEqual(console.export_text().splitlines()[-1], "host status")
        self.assertIn("retained diagnostic", sink.getvalue())
        self.assertEqual(original.errors, [])
        self.assertEqual(errors, [])
        self.assertIs(console._render_hooks, hooks)
        self.assertEqual(len(hooks), 0)
        self.assertNotIn("render", vars(console))
        self.assertNotIn("_build_identity_render_dispatch", vars(console))

    def test_concurrent_live_start_during_native_print_keeps_host_output(self):
        from rich.live import Live

        entered, resume = threading.Event(), threading.Event()
        failures = []

        class PausedConsole(Console):
            def print(self, *objects, **kwargs):
                if threading.current_thread().name == "record-writer":
                    entered.set()
                    if not resume.wait(3):
                        raise DeliveryFailure("record print timed out")
                return super().print(*objects, **kwargs)

        sink = io.StringIO()
        console = PausedConsole(
            file=sink,
            width=80,
            force_terminal=True,
            force_interactive=True,
            color_system="standard",
            _environ={"TERM": "xterm"},
            record=True,
        )
        hooks = console._render_hooks
        original = NativeHandler(console)
        wrapped, errors = adapter(original)

        def emit():
            try:
                wrapped.handle(record())
            except (AssertionError, DeliveryFailure, OSError, RuntimeError) as error:
                failures.append(error)

        worker = threading.Thread(target=emit, name="record-writer")
        worker.start()
        live = Live(
            "host status",
            console=console,
            auto_refresh=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        try:
            self.assertTrue(entered.wait(3))
            live.start(refresh=True)
            sink.seek(0)
            sink.truncate()
            console.export_text()
            resume.set()
            worker.join(4)
            emitted = sink.getvalue()
            recorded = console.export_text()
        finally:
            resume.set()
            worker.join(4)
            live.stop()
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(emitted.startswith("\r\x1b[2K"), repr(emitted))
        self.assertEqual(emitted.splitlines()[-1], "host status")
        self.assertEqual(recorded.splitlines()[-1], "host status")
        self.assertIn("retained diagnostic", emitted)
        self.assertEqual(original.errors, [])
        self.assertEqual(errors, [])
        self.assertIs(console._render_hooks, hooks)
        self.assertEqual(len(hooks), 0)
        self.assertNotIn("render", vars(console))
        self.assertNotIn("_build_identity_render_dispatch", vars(console))
