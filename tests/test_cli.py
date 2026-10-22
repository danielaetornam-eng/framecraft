"""The CLI and the HTML report.

The CLI is tested by running its ``main()`` in-process rather than by shelling
out, so failures produce tracebacks instead of exit codes -- and so a test can
assert on the *output* rather than on stdout capture alone.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from framecraft import CALL, LINE, RETURN, FunctionTracer, TracerConfig
from framecraft.cli.main import _split_inspect_target, build_parser, main
from framecraft.sinks import HTMLReport, render_report
from framecraft.sinks.report import _CSS

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


SCRIPT = '''
def leaf(n):
    return n * 2


def middle(n):
    return leaf(n) + leaf(n + 1)


def main():
    return middle(3)


RESULT = main()
print("RESULT", RESULT)
'''


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "target.py"
    p.write_text(SCRIPT, encoding="utf-8")
    return p


def _leaf(n):
    return n * 2


def _middle(n):
    return _leaf(n) + _leaf(n + 1)


@pytest.fixture
def jsonl_trace(tmp_path):
    """A small but real trace.

    Statements inside a fixture function produce no events, because its frame
    already existed when the tracer started.
    """
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN))) as t:
        for _ in range(3):
            _middle(2)
    assert t.events, "fixture produced an empty trace"
    path = tmp_path / "trace.jsonl"
    from framecraft.sinks import JSONLSink

    sink = JSONLSink(path=str(path))
    for ev in t.events:
        sink.write(ev)
    sink.close()
    return path


@pytest.fixture
def sqlite_trace(tmp_path, jsonl_trace):
    from framecraft.analysis import load_jsonl
    from framecraft.sinks import SQLiteSink

    path = tmp_path / "trace.sqlite"
    sink = SQLiteSink(str(path), reset=True)
    for ev in load_jsonl(str(jsonl_trace)):
        sink.write(ev)
    sink.close()
    return path


# ----------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------


class TestParser:
    def test_run_defaults(self):
        ns = build_parser().parse_args(["run", "x.py"])
        assert ns.script == "x.py"
        assert ns.engine == "auto"
        assert ns.events == "call,line,return"
        assert ns.out == "trace.jsonl"
        assert ns.locals is False

    def test_report_and_graph(self):
        assert build_parser().parse_args(["report", "t.jsonl"]).top == 20
        assert build_parser().parse_args(["graph", "t.jsonl"]).kind == "flame"

    def test_unknown_command_is_an_error(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["frobnicate"])

    def test_inspect_target_parsing(self):
        assert _split_inspect_target("a.py") == ("a.py", "")
        assert _split_inspect_target("a.py:12") == ("a.py", "12")
        assert _split_inspect_target("a.py::fn") == ("a.py", "fn")
        assert _split_inspect_target("a.py::Cls.meth") == ("a.py", "Cls.meth")
        # Windows drive letters must not be mistaken for the path separator.
        assert _split_inspect_target("C:/src/app.py:42") == ("C:/src/app.py", "42")
        assert _split_inspect_target("C:/src/app.py") == ("C:/src/app.py", "")


# ----------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------


class TestDemo:
    def test_demo_runs(self):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            assert main(["demo"]) == 0
        out = buf.getvalue()
        assert "fc-trace demo" in out
        assert "HOT LINES" in out
        # The report must be valid JSON in the middle of prose.
        m = re.search(r"\{.*?\n\}", out, re.S)
        assert m, "demo did not print a stats block"
        stats = json.loads(m.group(0))
        assert stats["total_events"] > 0

    @pytest.mark.parametrize("engine", ["settrace", "monitoring", "profile", "sample"])
    def test_demo_engines(self, engine):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = main(["demo", "--engine", engine])
        assert rc == 0
        assert engine in buf.getvalue()


class TestRun:
    def _run(self, script, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = main(["run", str(script), *extra])
        return rc, buf.getvalue()

    def test_jsonl(self, script, tmp_path):
        out = tmp_path / "t.jsonl"
        rc, _ = self._run(script, "-o", str(out))
        assert rc == 0
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) > 5
        assert json.loads(lines[0])["kind"] in ("call", "line")

    def test_csv(self, script, tmp_path):
        out = tmp_path / "t.csv"
        rc, _ = self._run(script, "-o", str(out))
        assert rc == 0
        header = out.read_text(encoding="utf-8").split("\n")[0]
        assert header.startswith("seq,kind,perf_ns")

    def test_sqlite(self, script, tmp_path):
        out = tmp_path / "t.sqlite"
        rc, _ = self._run(script, "-o", str(out))
        assert rc == 0
        conn = sqlite3.connect(str(out))
        try:
            assert conn.execute("SELECT count(*) FROM events").fetchone()[0] > 0
        finally:
            conn.close()

    def test_svg(self, script, tmp_path):
        out = tmp_path / "f.svg"
        rc, _ = self._run(script, "-o", str(out), "--format", "svg")
        assert rc == 0
        assert out.read_text(encoding="utf-8").startswith("<svg")

    def test_html(self, script, tmp_path):
        out = tmp_path / "r.html"
        rc, _ = self._run(script, "-o", str(out), "--buffer")
        assert rc == 0
        html = out.read_text(encoding="utf-8")
        assert html.startswith("<!doctype html>")
        assert "Flame graph" in html

    def test_locals_flag(self, script, tmp_path):
        out = tmp_path / "t.jsonl"
        self._run(script, "-o", str(out), "--locals")
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").split("\n") if line]
        assert any("locals" in r for r in rows)

    def test_include_filter(self, script, tmp_path):
        out = tmp_path / "t.jsonl"
        self._run(script, "-o", str(out), "--include", "no_such_directory")
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").split("\n") if line]
        assert not rows, "the include filter should have excluded everything"

    def test_script_args_are_forwarded(self, tmp_path):
        p = tmp_path / "echo.py"
        p.write_text("import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        rc, out = self._run(p, "alpha", "beta")
        assert rc == 0
        assert "ARGV ['alpha', 'beta']" in out

    def test_double_dash_separator(self, tmp_path):
        p = tmp_path / "echo.py"
        p.write_text("import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        rc, out = self._run(p, "--", "--engine", "not-mine")
        assert rc == 0
        assert "ARGV ['--engine', 'not-mine']" in out

    def test_options_and_script_args_can_be_interleaved(self, tmp_path):
        p = tmp_path / "echo.py"
        p.write_text("import sys\nprint('ARGV', sys.argv[1:])\n", encoding="utf-8")
        out_file = tmp_path / "t.jsonl"
        rc, out = self._run(p, "alpha", "-o", str(out_file), "beta")
        assert rc == 0
        assert out_file.exists(), "the -o flag must still be honoured"
        assert "ARGV ['alpha', 'beta']" in out

    def test_sys_argv_is_restored_afterwards(self, script):
        before = list(sys.argv)
        self._run(script)
        assert sys.argv == before, "the tracer must not leave sys.argv rewritten"

    def test_sys_path_is_restored_afterwards(self, script):
        before = list(sys.path)
        self._run(script)
        assert sys.path == before

    def test_script_exiting_with_a_code_is_propagated(self, tmp_path):
        p = tmp_path / "exiter.py"
        p.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        rc, _ = self._run(p)
        assert rc == 3

    def test_script_exiting_with_a_string_is_success(self, tmp_path):
        p = tmp_path / "exiter.py"
        p.write_text("import sys\nsys.exit('bye')\n", encoding="utf-8")
        rc, _ = self._run(p)
        assert rc == 0

    def test_missing_script_is_a_clean_error(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            main(["run", str(tmp_path / "nope.py")])
        assert "not found" in str(exc.value)

    def test_unknown_event_kind_is_a_clean_error(self, script):
        with pytest.raises(SystemExit) as exc:
            main(["run", str(script), "--events", "call,bogus"])
        assert "unknown event kinds" in str(exc.value)

    def test_trace_is_written_even_when_the_script_raises(self, tmp_path):
        p = tmp_path / "crasher.py"
        p.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        out = tmp_path / "t.jsonl"
        buf = io.StringIO()
        with pytest.raises(RuntimeError), redirect_stdout(buf), redirect_stderr(buf):
            main(["run", str(p), "-o", str(out)])
        assert out.exists(), (
            "a crashing program must still leave a trace: the crash is the "
            "thing you are debugging"
        )
        assert out.stat().st_size > 0

    @pytest.mark.parametrize("engine", ["settrace", "monitoring", "profile"])
    def test_engines(self, script, tmp_path, engine):
        out = tmp_path / f"{engine}.jsonl"
        rc, _ = self._run(script, "-o", str(out), "--engine", engine)
        assert rc == 0
        assert out.stat().st_size > 0, f"{engine} produced an empty trace"

    def test_sampling_engine_produces_a_sink_file(self, script, tmp_path):
        """A sampler writes nothing for a workload shorter than one tick.

        The file must still exist and the run must succeed: an empty trace from
        a sampling profiler is a correct answer, not a failure.
        """
        out = tmp_path / "sample.jsonl"
        rc, _ = self._run(script, "-o", str(out), "--engine", "sample")
        assert rc == 0
        assert out.exists()


class TestReportAndGraph:
    def test_report_jsonl(self, jsonl_trace):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["report", str(jsonl_trace)]) == 0
        out = buf.getvalue()
        assert "HOT LINES" in out
        assert "EXCLUSIVE TIME" in out
        assert "events" in out

    def test_report_sqlite(self, sqlite_trace):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["report", str(sqlite_trace)]) == 0
        assert "HOT LINES" in buf.getvalue()

    def test_report_writes_html(self, jsonl_trace, tmp_path):
        html = tmp_path / "r.html"
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["report", str(jsonl_trace), "--html", str(html)])
        assert html.exists()
        assert html.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_graph_flame(self, jsonl_trace, tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["graph", str(jsonl_trace), "-o", str(tmp_path / "g")]) == 0
        svg = tmp_path / "g-flame.svg"
        assert svg.exists() and svg.read_text(encoding="utf-8").startswith("<svg")

    def test_graph_call(self, jsonl_trace, tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["graph", str(jsonl_trace), "--kind", "call",
                         "-o", str(tmp_path / "g")]) == 0
        dot = tmp_path / "g-call.dot"
        assert dot.read_text(encoding="utf-8").startswith("digraph")

    def test_graph_both(self, jsonl_trace, tmp_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["graph", str(jsonl_trace), "--kind", "both", "-o", str(tmp_path / "g")])
        assert (tmp_path / "g-flame.svg").exists()
        assert (tmp_path / "g-call.dot").exists()


class TestDiffCommand:
    def test_identical(self, jsonl_trace):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["diff", str(jsonl_trace), str(jsonl_trace)]) == 0
        assert "TRACE DIFF" in buf.getvalue()

    def test_incomparable_exits_nonzero(self, tmp_path):
        """A diff that cannot be trusted must not exit 0."""
        # Two genuinely different amounts of work, not a truncated file.
        def one():
            return 1

        with FunctionTracer(TracerConfig(events=(CALL, RETURN))) as small:
            one()
        with FunctionTracer(TracerConfig(events=(CALL, RETURN))) as big:
            for _ in range(20):
                one()

        from framecraft.sinks import JSONLSink

        paths = []
        for name, tracer in (("small", small), ("big", big)):
            p = tmp_path / f"{name}.jsonl"
            sink = JSONLSink(path=str(p))
            for ev in tracer.events:
                sink.write(ev)
            sink.close()
            paths.append(p)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["diff", str(paths[0]), str(paths[1])])
        assert rc != 0, "an incomparable diff must not report success"
        assert "NOT COMPARABLE" in buf.getvalue()

    def test_json_output(self, jsonl_trace):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["diff", str(jsonl_trace), str(jsonl_trace), "--json"])
        assert json.loads(buf.getvalue())["comparable"] is True


class TestQuery:
    def test_runs_sql(self, sqlite_trace):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["query", str(sqlite_trace),
                       "SELECT kind, count(*) FROM events GROUP BY kind"])
        assert rc == 0
        assert "call" in buf.getvalue()

    def test_bad_sql_is_a_clean_error(self, sqlite_trace):
        buf = io.StringIO()
        with pytest.raises(SystemExit) as exc, redirect_stdout(buf):
            main(["query", str(sqlite_trace), "SELECT nope FROM nothing"])
        assert "SQL error" in str(exc.value)

    def test_no_rows(self, sqlite_trace):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["query", str(sqlite_trace),
                         "SELECT 1 FROM events WHERE 0"]) == 0
        assert "(no rows)" in buf.getvalue()


class TestInspect:
    def test_file(self, script):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["inspect", str(script)]) == 0
        out = buf.getvalue()
        assert "CODE OBJECT" in out
        assert "INSTRUCTIONS" in out

    def test_function(self, script):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["inspect", f"{script}::leaf"]) == 0
        out = buf.getvalue()
        assert "leaf" in out
        assert "f_lasti" in out

    def test_line_number_inside_a_function_reports_the_code_object(self, script):
        """A function's frame is gone by the time we can capture it.

        So `inspect file.py:N` for a line *inside* a function must report the
        code object and say so, rather than printing the module frame's details
        under a heading that names the function.
        """
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["inspect", f"{script}:2"]) == 0
        out = buf.getvalue()
        assert "CODE OBJECT  leaf" in out
        assert "no live frame" in out
        assert "properties of a frame, not of code" in out
        assert "INSTRUCTIONS" not in out, (
            "instructions require a live frame; the module frame's instructions "
            "must not be presented as the requested function's"
        )

    def test_line_number_at_module_level_gets_a_live_frame(self, script):
        """A module-level line does have a live frame, so we get the full dump."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["inspect", f"{script}:1"]) == 0
        out = buf.getvalue()
        assert "<module" in out
        assert "INSTRUCTIONS" in out
        assert "f_lasti" in out

    def test_function_request_does_not_lie_about_which_frame_it_shows(self, script):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["inspect", f"{script}::leaf"]) == 0
        out = buf.getvalue()
        assert "CODE OBJECT  leaf" in out
        # The word appears in the explanatory note, so assert on the *field*
        # rather than the substring: a code object has no f_lasti.
        assert not any(line.strip().startswith("f_lasti") for line in out.splitlines()), (
            "a code object has no f_lasti; printing one would be a lie"
        )
        assert not any(line.strip().startswith("trace hooks") for line in out.splitlines())

    def test_json_output_is_not_polluted_by_the_target_module(self, script):
        """`inspect` runs the module; the module prints. The JSON must still parse."""
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            assert main(["inspect", str(script), "--json"]) == 0
        data = json.loads(buf.getvalue())
        assert data["code"]["qualname"]
        assert "instructions" in data

    def test_missing_file(self, tmp_path):
        buf = io.StringIO()
        with pytest.raises(SystemExit) as exc, redirect_stdout(buf):
            main(["inspect", str(tmp_path / "nope.py")])
        assert "not found" in str(exc.value)


# ----------------------------------------------------------------------
# HTML report
# ----------------------------------------------------------------------


class TestHTMLReport:
    @pytest.fixture
    def report(self):
        def boom():
            raise ValueError("nope")

        with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN, "exception"),
                                         capture_locals=True)) as t:
            try:
                boom()
            except ValueError:
                pass  # the point is to capture the failure, not to survive it
        assert t.events, "fixture produced no events"
        return t

    def test_is_self_contained(self, report):
        html = render_report(report.events)
        assert html.startswith("<!doctype html>")
        assert "<script" not in html.lower(), "no JS is needed and none should be required"
        # The SVG xmlns is a namespace URI, not a fetch. What matters is that
        # nothing is *loaded*: no src/href/@import pointing off-document.
        assert not re.search(r'(src|href)\s*=\s*["\']https?://', html), (
            "a report that fetches anything will not open on an air-gapped machine"
        )
        assert "@import" not in html

    def test_contains_every_section(self, report):
        html = render_report(report.events)
        for marker in ("Hot lines", "Exclusive time", "Busiest functions",
                       "Exceptions", "Event log", "Flame graph"):
            assert marker in html, f"missing section: {marker}"

    def test_inline_css(self, report):
        html = render_report(report.events)
        assert "<style>" in html
        assert "prefers-color-scheme" in _CSS, "a light-mode fallback is expected"

    def test_locals_are_off_by_default(self, report):
        """A report about to be attached to a bug report must not carry secrets."""
        html = render_report(report.events)
        assert "ValueError" in html
        # Locals were captured, but include_locals defaults to False.
        assert "include_locals" not in html

    def test_locals_can_be_included_explicitly(self, report):
        html = render_report(report.events, include_locals=True)
        assert "data-kind=" in html

    def test_incomplete_trace_is_advertised(self, report):
        html = render_report(report.events, dropped=500, errors=3)
        assert "500 events were dropped" in html
        assert "INCOMPLETE" in html or "lower bounds" in html

    def test_complete_trace_is_not_advertised_as_incomplete(self, report):
        html = render_report(report.events)
        assert "were dropped" not in html

    def test_flame_can_be_skipped(self, report):
        html = render_report(report.events, flame_only=True)
        assert "<svg" not in html

    def test_row_cap_is_respected(self, report):
        html = render_report(report.events, max_log_rows=5)
        assert html.count("<tr data-kind=") <= 5

    def test_empty_trace_still_renders(self):
        html = render_report([])
        assert html.startswith("<!doctype html>")
        assert "(none)" in html

    def test_write(self, report, tmp_path):
        path = HTMLReport(report.events).write(str(tmp_path / "r.html"))
        assert os.path.getsize(path) > 1000

    def test_html_is_escaped(self, tmp_path):
        def evil():
            return "<script>alert(1)</script>"

        with FunctionTracer(TracerConfig(events=(CALL, "return"), capture_args=True,
                                         capture_return=True,
                                         include=lambda c: c.co_name == "evil")) as t:
            evil()
        assert any("script" in (e.retval_repr or "") for e in t.events)
        html = render_report(t.events, include_locals=True)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
