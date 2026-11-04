"""Exercise real handler, executable and protocol boundaries in isolated processes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from uuid import UUID

import pytest


REVISION = "a" * 40


@pytest.fixture
def runtime(tmp_path):
    target = tmp_path / "earnings_watch"
    shutil.copytree(Path(__file__).parents[1] / "src" / "earnings_watch", target)
    (target / "build_identity.json").write_text(
        json.dumps(
            {"schema": 1, "service": "earnings-watch", "version": "1.2.3", "revision": REVISION}
        )
    )
    return tmp_path


def run(runtime, code, *, input=None):
    env = {
        **os.environ,
        "VERSION": "99.99.99",
        "SOURCE_REVISION": "b" * 40,
        "EARNINGS_WATCH_DATA_DIR": str(runtime / "state"),
    }
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(runtime)!r})\n" + textwrap.dedent(code),
        ],
        cwd=runtime,
        env=env,
        input=input,
        text=True,
        capture_output=True,
        timeout=30,
    )


def fields(payload):
    assert payload["service"] == "earnings-watch"
    assert payload["version"] == "1.2.3"
    assert payload["revision"] == REVISION
    assert str(UUID(payload["instance"])) == payload["instance"]


def test_package_and_executable_imports_do_not_require_optional_dependencies(runtime):
    result = run(
        runtime,
        """
        import importlib.abc
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, *args):
                if fullname.split('.')[0] in {'pydantic', 'yaml', 'fastmcp'}:
                    raise ImportError('synthetic-private-import-detail')
        sys.meta_path.insert(0, Block())
        import earnings_watch
        import earnings_watch.cli
        import earnings_watch.llm.research_server
        assert earnings_watch.__version__ == '1.2.3'
        assert 'earnings_watch.composition' not in sys.modules
        assert 'earnings_watch.settings' not in sys.modules
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_dynamic_handlers_keep_formatters_filters_levels_and_structured_types(runtime):
    result = run(
        runtime,
        """
        import io, json, logging
        from earnings_watch.diagnostics import configure_logging
        configure_logging()
        configure_logging()
        first, second = io.StringIO(), io.StringIO()
        logger = logging.getLogger('sdk.synthetic')
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(first)
        handler.setLevel(logging.INFO)
        class Redact(logging.Formatter):
            def format(self, record):
                return record.getMessage().replace('private-value', '[redacted]')
        formatter = Redact()
        handler.setFormatter(formatter)
        accepted = []
        def filter_record(record):
            accepted.append(record.msg)
            return record.msg != 'denied'
        handler.addFilter(filter_record)
        logger.addHandler(handler)
        logger.debug('below threshold')
        logger.warning('denied')
        logger.info('private-value')
        logger.error(json.dumps({'version': 7, 'version_': False, 'instance': ['native'],
                                 'count': 3, 'ready': True, 'message': 'private-value'}))
        handler.setStream(second)
        logger.warning('later sink')
        assert handler.formatter is formatter and handler.level == logging.INFO
        assert handler.filters == [filter_record]
        assert len(accepted) == 4
        print(json.dumps([first.getvalue(), second.getvalue()]))
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    first, second = json.loads(result.stdout)
    records = [json.loads(line) for line in first.splitlines() + second.splitlines()]
    assert len(records) == 3
    for record in records:
        fields(record)
    assert records[0]["message"] == "[redacted]"
    assert records[1]["version__"] == 7 and records[1]["version_"] is False
    assert records[1]["instance_"] == ["native"]
    assert records[1]["count"] == 3 and records[1]["ready"] is True
    assert records[1]["message"] == "[redacted]"
    assert records[2]["message"] == "later sink"
    assert len({record["instance"] for record in records}) == 1


def test_rich_keeps_completed_rendering_traceback_policy_and_shared_dynamic_sink(runtime):
    result = run(
        runtime,
        """
        import io, json, logging
        from rich.console import Console
        from rich.logging import RichHandler
        from earnings_watch.diagnostics import configure_logging
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=False, width=100)
        normal = RichHandler(console=console, show_time=False, show_path=False)
        trace = RichHandler(console=console, show_time=False, show_path=False,
                            rich_tracebacks=True, tracebacks_show_locals=False,
                            tracebacks_max_frames=3, tracebacks_suppress=['/synthetic'])
        normal.addFilter(lambda record: record.exc_info is None)
        trace.addFilter(lambda record: record.exc_info is not None)
        normal.setFormatter(logging.Formatter('normal %(message)s'))
        trace.setFormatter(logging.Formatter('failure %(message)s'))
        try:
            raise ValueError('synthetic failure')
        except ValueError:
            failure = logging.LogRecord('fastmcp.synthetic', logging.ERROR, '', 1,
                                        'request failed', (), sys.exc_info())
        info = logging.LogRecord('fastmcp.synthetic', logging.INFO, '', 1, 'ready', (), None)
        normal.handle(info)
        normal.handle(failure)
        trace.handle(info)
        trace.handle(failure)
        baseline = stream.getvalue()
        stream.seek(0); stream.truncate()
        original = (normal.formatter, trace.formatter, list(normal.filters), list(trace.filters))
        configure_logging()
        normal.handle(info)
        normal.handle(failure)
        trace.handle(info)
        trace.handle(failure)
        assert original == (normal.formatter, trace.formatter, normal.filters, trace.filters)
        assert trace.tracebacks_max_frames == 3
        assert trace.tracebacks_suppress == ['/synthetic']
        assert trace.tracebacks_show_locals is False and trace.console is console
        attributed = stream.getvalue()
        second = io.StringIO()
        console.file = second
        normal.handle(info)
        print(json.dumps([baseline, attributed, second.getvalue()]))
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    baseline, rendered, second = json.loads(result.stdout)
    records = [json.loads(line) for line in rendered.splitlines()]
    assert len(records) == 2
    assert "".join(record["message"] for record in records) == baseline
    assert "Traceback" in records[1]["message"] and "ValueError: synthetic failure" in baseline
    for record in records + [json.loads(second)]:
        fields(record)


def test_actual_fastmcp_filters_and_reconfiguration_remain_effective(runtime):
    result = run(
        runtime,
        """
        import logging
        from earnings_watch.diagnostics import configure_logging
        configure_logging()
        from fastmcp.utilities.logging import configure_logging as native_configure
        logger = logging.getLogger('fastmcp')
        native_configure('DEBUG', enable_rich_tracebacks=False)
        logger.debug('first diagnostic')
        try:
            raise ValueError('explicit synthetic exception')
        except ValueError:
            logger.exception('explicit exception policy')
        native_configure('DEBUG', enable_rich_tracebacks=False)
        logger.debug('later diagnostic')
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(records) == 3
    assert all(record["logger"] == "fastmcp" for record in records)
    for record in records:
        fields(record)
    assert "explicit synthetic exception" in records[1]["message"]


def test_rich_delivery_preserves_enclosing_capture_and_host_text(runtime):
    result = run(
        runtime,
        """
        import io, json, logging
        from rich.console import Console
        from rich.logging import RichHandler
        from earnings_watch.diagnostics import configure_logging
        stream = io.StringIO()
        console = Console(file=stream, color_system=None, width=100)
        handler = RichHandler(console=console, show_time=False, show_path=False)
        handler.addFilter(lambda record: handler.console is console)
        record = logging.LogRecord('sdk', logging.WARNING, '', 1, 'captured diagnostic', (), None)
        configure_logging()
        with console.capture() as captured:
            console.out('host before')
            handler.handle(record)
            console.out('host after')
        assert stream.getvalue() == ''
        lines = captured.get().splitlines()
        assert lines[0] == 'host before' and lines[-1] == 'host after'
        assert len(lines) == 3, lines
        retained = json.loads(lines[1])
        assert 'captured diagnostic' in retained['message']
        assert 'host before' not in retained['message']
        assert handler.console is console
        print(json.dumps(retained))
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    fields(json.loads(result.stdout))


@pytest.mark.parametrize("suppress", [False, True])
def test_rich_delivery_preserves_native_render_hook_redaction_and_suppression(runtime, suppress):
    result = run(
        runtime,
        """
        import io, json, logging
        from rich.console import Console, RenderHook
        from rich.logging import RichHandler
        from rich.table import Table
        from rich.text import Text
        from earnings_watch.diagnostics import configure_logging
        class Redact(RenderHook):
            def process_renderables(self, renderables):
                calls.append(len(renderables))
                return [Text('redacted') for item in renderables
                        if not isinstance(item, Table) or not SUPPRESS]
        calls = []
        stream = io.StringIO()
        console = Console(file=stream, color_system=None, width=100)
        console.push_render_hook(Redact())
        handler = RichHandler(console=console, show_time=False, show_level=False,
                              show_path=False, highlighter=None)
        record = logging.LogRecord('sdk', logging.INFO, '', 1, 'synthetic-secret', (), None)
        with console.capture() as captured:
            handler.handle(record)
        native = captured.get()
        assert len(calls) == 1
        calls.clear()
        configure_logging()
        with console.capture() as captured:
            handler.handle(record)
        assert len(calls) == 1, calls
        retained = captured.get()
        if SUPPRESS:
            assert native == retained == ''
        else:
            payload = json.loads(retained)
            assert payload['message'] == native == 'redacted\\n'
            assert 'synthetic-secret' not in retained
        assert stream.getvalue() == '' and handler.console is console
        """.replace("SUPPRESS", repr(suppress)),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_rich_delivery_retains_subclass_print_arguments_styles_and_plain_record_export(runtime):
    result = run(
        runtime,
        """
        import io, json, logging
        from rich.console import Console
        from rich.logging import RichHandler
        from rich.text import Text
        from earnings_watch.diagnostics import configure_logging
        class Custom(RichHandler):
            def emit(self, record):
                try:
                    self.console.print(self.format(record), 'second', sep=' | ',
                                       style='bold red', soft_wrap=True, end='!\\n')
                except Exception:
                    self.handleError(record)
            def handleError(self, record):
                errors.append(record)
        errors = []
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=True, color_system='standard',
                          record=True, width=30)
        handler = Custom(console=console)
        record = logging.LogRecord('sdk', logging.INFO, '', 1, 'retained diagnostic', (), None)
        with console.capture() as captured:
            handler.handle(record)
        native = captured.get()
        assert '\\x1b[' in native
        configure_logging()
        with console.capture() as captured:
            console.out('host prefix', highlight=False)
            handler.handle(record)
            console.out('host suffix', highlight=False)
        retained = captured.get()
        assert errors == [] and handler.console is console
        lines = retained.splitlines(keepends=True)
        assert lines[0] == 'host prefix\\n' and lines[-1] == 'host suffix\\n'
        line = ''.join(lines[1:-1])
        assert line.startswith('service=earnings-watch version=1.2.3 revision=')
        assert line.endswith(' ' + native), (line, native)
        assert stream.getvalue() == ''
        handler.handle(record)
        assert console.export_text(styles=False) == Text.from_ansi(stream.getvalue()).plain
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("policy", ["no_color", "null_color"])
def test_rich_terminal_record_keeps_styles_independent_of_output_color_policy(runtime, policy):
    result = run(
        runtime,
        """
        import io, logging
        from rich.console import Console
        from rich.logging import RichHandler
        from rich.style import Style
        from rich.text import Text
        from earnings_watch.build_identity import process_instance
        from earnings_watch.diagnostics import configure_logging
        class Styled(RichHandler):
            def emit(self, record):
                self.console.print(Text(record.getMessage(), style=Style(color='#123456', bold=True)))
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=True, record=True, width=60,
                          no_color=NO_COLOR, color_system=COLOR_SYSTEM)
        handler = Styled(console=console)
        record = logging.LogRecord('sdk', logging.INFO, '', 1, 'retained diagnostic', (), None)
        handler.handle(record)
        native_output = stream.getvalue()
        native_record = console.export_text(styles=True)
        stream.seek(0); stream.truncate()
        configure_logging()
        handler.handle(record)
        header = ('service=earnings-watch version=1.2.3 revision=' + 'a' * 40
                  + ' instance=' + process_instance() + ' ')
        assert stream.getvalue() == header + native_output
        assert console.export_text(styles=True) == header + native_record
        assert handler.console is console
        """.replace("NO_COLOR", repr(policy == "no_color")).replace(
            "COLOR_SYSTEM", repr(None if policy == "null_color" else "standard")
        ),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_rich_delivery_keeps_console_subclass_policy_and_state(runtime):
    result = run(
        runtime,
        """
        import io, json, logging
        from rich.console import Console, RenderHook
        from rich.logging import RichHandler
        from rich.text import Text
        from earnings_watch.diagnostics import configure_logging
        class Redact(RenderHook):
            def process_renderables(self, renderables):
                return [Text('console policy redacted')]
        class PolicyConsole(Console):
            calls = 0
            def print(self, *objects, **kwargs):
                self.calls += 1
                self.push_render_hook(Redact())
                try:
                    super().print(*objects, **kwargs)
                finally:
                    self.pop_render_hook()
        stream = io.StringIO()
        console = PolicyConsole(file=stream, color_system=None, width=100)
        handler = RichHandler(console=console, show_time=False, show_level=False,
                              show_path=False, highlighter=None)
        record = logging.LogRecord('sdk', logging.INFO, '', 1, 'retained diagnostic', (), None)
        handler.handle(record)
        native = stream.getvalue()
        stream.seek(0); stream.truncate()
        configure_logging()
        handler.handle(record)
        assert json.loads(stream.getvalue())['message'] == native
        assert console.calls == 2 and handler.console is console
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_rich_live_keeps_native_cursor_controls_and_host_status(runtime):
    result = run(
        runtime,
        """
        import io, logging
        from rich.console import Console
        from rich.live import Live
        from rich.logging import RichHandler
        from earnings_watch.build_identity import process_instance
        from earnings_watch.diagnostics import configure_logging
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=True, force_interactive=True,
                          color_system='standard', record=True, width=60,
                          _environ={'TERM': 'xterm'})
        handler = RichHandler(console=console, show_time=False, show_level=False,
                              show_path=False, highlighter=None)
        record = logging.LogRecord('sdk', logging.INFO, '', 1, 'retained diagnostic', (), None)
        live = Live('host live status', console=console, auto_refresh=False,
                    redirect_stdout=False, redirect_stderr=False)
        live.start(refresh=True)
        try:
            stream.seek(0); stream.truncate(); console.export_text()
            handler.handle(record)
            native_output = stream.getvalue()
            stream.seek(0); stream.truncate(); console.export_text()
            configure_logging()
            handler.handle(record)
            header = ('service=earnings-watch version=1.2.3 revision=' + 'a' * 40
                      + ' instance=' + process_instance() + ' ')
            assert '\\r\\x1b[2K' in native_output
            expected = native_output.replace('retained diagnostic', header + 'retained diagnostic', 1)
            assert stream.getvalue() == expected, (stream.getvalue(), expected)
            assert handler.console is console
        finally:
            live.stop()
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_rich_delivery_keeps_original_handle_error_policy(runtime, failure):
    result = run(
        runtime,
        f"""
        import io, json, logging
        from rich.console import Console
        from rich.logging import RichHandler
        from earnings_watch.diagnostics import configure_logging
        class Broken(io.StringIO):
            def {failure}(self, *args): raise OSError('synthetic sink failure')
        class NativeHandler(RichHandler):
            def handleError(self, record): errors.append(record.getMessage())
        errors = []
        console = Console(file=Broken(), color_system=None)
        handler = NativeHandler(console=console, show_time=False, show_path=False)
        record = logging.LogRecord('sdk', logging.WARNING, '', 1, 'retained diagnostic', (), None)
        handler.handle(record)
        assert errors == ['retained diagnostic']
        errors.clear()
        configure_logging()
        handler.handle(record)
        assert errors == ['retained diagnostic']
        assert handler.console is console
        print(json.dumps(errors))
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("force_terminal", [False, True])
def test_tty_attribution_preserves_native_lines_after_standard_and_rich_rendering(
    runtime, force_terminal
):
    result = run(
        runtime,
        """
        import io, json, logging
        from rich.console import Console
        from rich.logging import RichHandler
        from earnings_watch.diagnostics import configure_logging
        class Tty(io.StringIO):
            def isatty(self): return True
        standard_stream, rich_stream = Tty(), Tty()
        handlers = [logging.StreamHandler(standard_stream),
                    RichHandler(console=Console(file=rich_stream, force_terminal=False),
                                show_time=False, show_path=False)]
        record = logging.LogRecord('sdk', logging.WARNING, '', 1, 'first\\nsecond', (), None)
        for handler in handlers: handler.handle(record)
        baseline = [stream.getvalue() for stream in [standard_stream, rich_stream]]
        for stream in [standard_stream, rich_stream]: stream.seek(0); stream.truncate()
        configure_logging()
        for handler in handlers: handler.handle(record)
        print(json.dumps([baseline, [standard_stream.getvalue(), rich_stream.getvalue()]]))
    """.replace("force_terminal=False", f"force_terminal={force_terminal!r}"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    baseline, retained = json.loads(result.stdout)
    for original, attributed in zip(baseline, retained):
        assert len(original.splitlines()) == len(attributed.splitlines())
        for native, line in zip(original.splitlines(), attributed.splitlines()):
            assert line.startswith(f"service=earnings-watch version=1.2.3 revision={REVISION} ")
            assert " instance=" in line and line.endswith(" " + native)


def test_existing_root_configuration_and_host_hooks_are_unchanged(runtime):
    result = run(
        runtime,
        """
        import asyncio, io, logging, threading
        from earnings_watch.diagnostics import configure_logging
        root = logging.getLogger()
        handler = logging.StreamHandler(io.StringIO())
        root.addHandler(handler)
        root.setLevel(logging.ERROR)
        original = sys.excepthook, threading.excepthook, asyncio.BaseEventLoop.default_exception_handler
        configure_logging()
        assert root.handlers == [handler] and root.level == logging.ERROR
        assert original == (sys.excepthook, threading.excepthook,
                            asyncio.BaseEventLoop.default_exception_handler)
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_diagnostics_work_during_partial_optional_rich_import(runtime):
    result = run(
        runtime,
        """
        import logging, types
        from earnings_watch.diagnostics import configure_logging
        configure_logging()
        sys.modules['rich.logging'] = types.ModuleType('rich.logging')
        logging.getLogger('sdk.import').warning('import progress')
    """,
    )
    assert result.returncode == 0 and result.stdout == ""
    payload = json.loads(result.stderr)
    fields(payload)
    assert payload["message"] == "import progress"


def test_actual_uvicorn_config_and_multiline_diagnostics_are_attributed(runtime):
    result = run(
        runtime,
        """
        import logging, logging.config
        from earnings_watch.diagnostics import configure_logging
        configure_logging()
        from uvicorn.config import LOGGING_CONFIG
        logging.config.dictConfig(LOGGING_CONFIG)
        logging.getLogger('uvicorn.error').info('synthetic startup')
        logging.getLogger('uvicorn.error').error('first line\\nsecond line')
    """,
    )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(records) == 2
    for record in records:
        fields(record)
    assert "INFO:" in records[0]["message"]
    assert records[1]["message"].endswith("first line\nsecond line")


def test_logging_never_wraps_raw_streams_or_child_results(runtime):
    result = run(
        runtime,
        """
        import subprocess
        from earnings_watch.diagnostics import configure_logging
        original = sys.stdout, sys.stderr
        configure_logging()
        assert original == (sys.stdout, sys.stderr)
        child = subprocess.run([sys.executable, '-c',
            'import sys; print("raw-result"); print("raw-child-error", file=sys.stderr)'],
            capture_output=True)
        sys.stdout.buffer.write(child.stdout)
        sys.stderr.buffer.write(child.stderr)
    """,
    )
    assert result.returncode == 0
    assert result.stdout == "raw-result\n"
    assert result.stderr == "raw-child-error\n"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_eager_process_instance_is_shared_by_threads_and_changes_after_fork(runtime):
    result = run(
        runtime,
        """
        import logging, os, threading
        from earnings_watch.build_identity import process_instance
        from earnings_watch.diagnostics import configure_logging, executable_diagnostics
        instance = process_instance()
        configure_logging()
        logger = logging.getLogger('asyncio')
        with executable_diagnostics():
            logger.warning('parent')
            thread = threading.Thread(target=lambda: logger.warning('thread'))
            thread.start(); thread.join()
            pid = os.fork()
            if pid == 0:
                logger.warning('child')
                os._exit(0)
            os.waitpid(pid, 0)
        assert process_instance() == instance
    """,
    )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines()]
    for record in records:
        fields(record)
    messages = {record["message"]: record for record in records if record["logger"] == "asyncio"}
    assert len(messages) == 3
    assert messages["parent"]["instance"] == messages["thread"]["instance"]
    assert messages["parent"]["instance"] != messages["child"]["instance"]


def test_executable_warnings_keep_native_formatter_filter_sink_and_host_hook(runtime):
    result = run(
        runtime,
        """
        import io, json, warnings
        from earnings_watch.diagnostics import configure_logging, executable_diagnostics
        configure_logging()
        original = warnings.showwarning
        warnings.formatwarning = lambda message, *args: 'redacted warning\\n'
        explicit = io.StringIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            with executable_diagnostics():
                warnings.warn('private-value', DeprecationWarning)
                warnings.warn('private-value', RuntimeWarning)
                warnings.showwarning('private-value', RuntimeWarning, 'synthetic.py', 1,
                                     file=explicit)
        assert warnings.showwarning is original
        seen = []
        def host(*args, **kwargs): seen.append(args)
        warnings.showwarning = host
        with executable_diagnostics():
            assert warnings.showwarning is host
            warnings.showwarning('host warning', RuntimeWarning, 'synthetic.py', 1)
        assert warnings.showwarning is host and len(seen) == 1
        print(json.dumps(explicit.getvalue()))
    """,
    )
    assert result.returncode == 0, result.stderr
    assert "private-" not in result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(records) == 1
    explicit = json.loads(json.loads(result.stdout))
    for record in records + [explicit]:
        fields(record)
        assert record["logger"] == "py.warnings" and record["message"] == "redacted warning\n"


def test_executable_hooks_sanitize_failures_restore_exact_hooks_and_leave_custom_loop(runtime):
    result = run(
        runtime,
        """
        import asyncio, logging, threading
        from earnings_watch.diagnostics import configure_logging, executable_diagnostics
        configure_logging()
        original = sys.excepthook, threading.excepthook, asyncio.BaseEventLoop.default_exception_handler
        loop = asyncio.new_event_loop()
        seen = []
        def custom(loop, context): seen.append(context)
        loop.set_exception_handler(custom)
        with executable_diagnostics():
            context = {'message': 'private-context', 'exception': ValueError('private-exception')}
            loop.call_exception_handler(context)
            assert seen == [context] and loop.get_exception_handler() is custom
            loop.set_exception_handler(None)
            loop.call_exception_handler(context)
            sys.excepthook(KeyError, KeyError('private-key'), None)
            def fail(): raise RuntimeError('private-thread')
            thread = threading.Thread(target=fail)
            thread.start(); thread.join()
        assert original == (sys.excepthook, threading.excepthook,
                            asyncio.BaseEventLoop.default_exception_handler)
        try:
            with executable_diagnostics():
                raise LookupError('private-abrupt-exit')
        except LookupError:
            pass
        assert original == (sys.excepthook, threading.excepthook,
                            asyncio.BaseEventLoop.default_exception_handler)
        loop.close()
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "private-" not in result.stderr and "Traceback" not in result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(records) == 3
    for record in records:
        fields(record)
    assert {record["logger"] for record in records} == {"asyncio", "earnings_watch"}


@pytest.mark.parametrize(
    "arguments", [[], ["--private-token-value"], ["serve", "--port", "private-port"]]
)
def test_actual_parser_errors_are_quiet_and_attributed(runtime, arguments):
    result = run(
        runtime, f"from earnings_watch.cli import main\nraise SystemExit(main({arguments!r}))"
    )
    assert result.returncode == 2
    assert result.stdout == "" and "private-" not in result.stderr
    payload = json.loads(result.stderr)
    fields(payload)
    assert payload["error"]["code"] == "invalid_argument"


def test_actual_cli_help_and_success_json_are_unchanged(runtime):
    help_result = run(runtime, "from earnings_watch.cli import main\nmain(['--help'])")
    assert help_result.returncode == 0 and help_result.stderr == ""
    assert help_result.stdout.startswith("usage: earnings-watch")
    result = run(
        runtime,
        """
        from earnings_watch.cli import main
        assert main(['init']) == 0
        assert main(['export', 'events', '--format', 'json']) == 0
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    decoder = json.JSONDecoder()
    initialized, end = decoder.raw_decode(result.stdout)
    assert initialized["ok"] is True and initialized["state"] == "initialized"
    assert not {"service", "version", "revision", "instance"} & initialized.keys()
    assert json.loads(result.stdout[end:]) == {"events": []}


def test_optional_import_failure_is_sanitized_at_actual_cli_boundary(runtime):
    result = run(
        runtime,
        """
        import importlib.abc
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, *args):
                if fullname == 'yaml': raise ImportError('private-import-value')
        sys.meta_path.insert(0, Block())
        from earnings_watch.cli import main
        raise SystemExit(main(['init']))
    """,
    )
    assert result.returncode == 1 and result.stdout == ""
    assert "private-" not in result.stderr and "Traceback" not in result.stderr
    payload = json.loads(result.stderr)
    fields(payload)
    assert payload["error"]["error_type"] == "ImportError"


def test_invalid_configuration_is_sanitized_at_actual_cli_boundary(runtime):
    (runtime / "private-config.toml").write_text('unexpected = "private-config-value')
    result = run(
        runtime,
        """
        from earnings_watch.cli import main
        raise SystemExit(main(['--config', 'private-config.toml', 'status']))
    """,
    )
    assert result.returncode == 1 and result.stdout == ""
    assert "private-" not in result.stderr
    payload = json.loads(result.stderr)
    fields(payload)
    assert payload["error"]["code"] == "invalid_argument"


def test_missing_build_metadata_stays_unknown_despite_runtime_relabel(runtime):
    (runtime / "earnings_watch" / "build_identity.json").unlink()
    result = run(
        runtime,
        """
        from earnings_watch import __version__
        from earnings_watch.cli import main
        assert __version__ == 'unknown'
        raise SystemExit(main(['--bad']))
    """,
    )
    assert result.returncode == 2 and result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["service"] == "earnings-watch"
    assert payload["version"] == payload["revision"] == "unknown"
    assert str(UUID(payload["instance"])) == payload["instance"]


@pytest.mark.parametrize("frontend", ["cli", "research"])
def test_executable_interrupt_keeps_exit_130_without_raw_traceback(runtime, frontend):
    (runtime / "research.json").write_text('{"directory":"captures","hosts":[]}')
    module_name = "cli" if frontend == "cli" else "llm.research_server"
    result = run(
        runtime,
        f"""
        import importlib
        module = importlib.import_module('earnings_watch.{module_name}')
        def interrupt(*args): raise KeyboardInterrupt('private-interrupt')
        module.execute = module.create = interrupt
        args = ['status'] if {frontend!r} == 'cli' else ['research.json']
        raise SystemExit(module.main(args))
    """,
    )
    assert result.returncode == 130 and result.stdout == ""
    assert "private-" not in result.stderr and "Traceback" not in result.stderr
    payload = json.loads(result.stderr)
    fields(payload)
    assert payload["error"]["code"] == "interrupted"


@pytest.mark.parametrize("code,status", [("invalid_argument", 1), ("temporarily_unavailable", 3)])
def test_existing_domain_error_fields_and_exit_status_are_preserved(runtime, code, status):
    result = run(
        runtime,
        f"""
        import earnings_watch.cli as cli
        from earnings_watch.errors import EarningsError
        def fail(*args):
            raise EarningsError({code!r}, 'controlled message', retry_after=7, allowed=False)
        cli.execute = fail
        raise SystemExit(cli.main(['status']))
    """,
    )
    assert result.returncode == status and result.stdout == ""
    payload = json.loads(result.stderr)
    fields(payload)
    assert payload["ok"] is False
    assert payload["error"] == {
        "code": code,
        "message": "controlled message",
        "retry_after": 7,
        "allowed": False,
    }


def test_research_factory_reports_artifact_version(runtime):
    result = run(
        runtime,
        """
        from earnings_watch.llm.research_server import create
        server = create({'directory': 'captures', 'hosts': ['issuer.example']})
        assert server.version == '1.2.3'
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_research_main_preserves_protocol_arguments_and_restores_hooks(runtime):
    (runtime / "research.json").write_text('{"directory":"captures","hosts":[]}')
    result = run(
        runtime,
        """
        import asyncio, threading
        import earnings_watch.llm.research_server as research
        original = sys.excepthook, threading.excepthook, asyncio.BaseEventLoop.default_exception_handler
        class Server:
            def run(self, **kwargs):
                assert kwargs == {'transport': 'stdio', 'show_banner': False}
                sys.stdout.write('native-protocol-bytes\\n')
        research.create = lambda config: Server()
        assert research.main(['research.json']) == 0
        assert original == (sys.excepthook, threading.excepthook,
                            asyncio.BaseEventLoop.default_exception_handler)
    """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "native-protocol-bytes\n" and result.stderr == ""


@pytest.mark.parametrize("arguments", [[], ["private-missing-config.json"]])
def test_research_failures_are_quiet_and_attributed(runtime, arguments):
    result = run(
        runtime,
        f"""
        from earnings_watch.llm.research_server import main
        raise SystemExit(main({arguments!r}))
    """,
    )
    assert result.returncode in {1, 2} and result.stdout == ""
    assert "private-" not in result.stderr and "Traceback" not in result.stderr
    fields(json.loads(result.stderr))


def test_actual_research_stdio_initialize_keeps_native_json_protocol(runtime):
    (runtime / "research.json").write_text('{"directory":"captures","hosts":[]}')
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "synthetic", "version": "1.0"},
        },
    }
    result = run(
        runtime,
        """
        from earnings_watch.llm.research_server import main
        raise SystemExit(main(['research.json']))
    """,
        input=json.dumps(request) + "\n",
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["id"] == 1
    assert response["result"]["serverInfo"] == {"name": "Earnings Research", "version": "1.2.3"}
    assert not {"service", "revision", "instance"} & response.keys()
    for line in result.stderr.splitlines():
        fields(json.loads(line))
