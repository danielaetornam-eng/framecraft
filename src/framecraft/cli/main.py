"""``fc-trace`` -- a small command line tracer, built on the library.

Subcommands
-----------

``run``        Execute a script under a tracer and write a trace.
``report``     Summarise a trace (text, HTML, or both).
``graph``      Render a call graph (DOT) or a flame graph (SVG) from a trace.
``diff``       Compare two traces.
``query``      Run SQL against a ``.sqlite`` trace.
``inspect``    Dump everything about a code object / frame -- the "open the hood"
               tool.
``demo``       Run the built-in sample workload.  Zero setup, useful for
               checking an install.

The ``run`` subcommand is the interesting one.  It deliberately supports
tracing a script *by path* rather than requiring an import hook, because the
moment you need a tracer you usually have a specific script that misbehaves and
you want it traced with nothing else changed.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import textwrap
from collections.abc import Sequence
from typing import Any

from framecraft._internal.compat import HAS_MONITORING
from framecraft.core.events import KINDS

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fc-trace",
        description="Execution tracer for Python, built on framecraft.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            examples:
              fc-trace run app.py --events call,line,return -o trace.jsonl
              fc-trace run app.py --engine sample --interval 0.001 -o flame.jsonl
              fc-trace report trace.jsonl --html report.html
              fc-trace graph trace.jsonl --kind flame -o flame.svg
              fc-trace diff before.jsonl after.jsonl
              fc-trace query trace.sqlite "SELECT kind, count(*) FROM events GROUP BY kind"
              fc-trace inspect app.py:42
            """
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # ------------------------------------------------------------------ run
    r = sub.add_parser("run", help="run a script under a tracer")
    # NOTE: no `script_args` here.  `nargs=REMAINDER` swallows *everything*
    # after the script path -- including this command's own options -- so
    # `fc-trace run app.py -o trace.jsonl` silently ignored `-o` and wrote
    # trace.jsonl.  Unrecognised arguments are collected with
    # `parse_known_args` instead and forwarded to the script, which is what a
    # user expects and what every other tool does.
    r.add_argument("script", help="path to a .py file")
    r.add_argument(
        "--engine",
        default="auto",
        choices=["auto", "settrace", "monitoring", "profile", "sample"],
        help="which hook to use (default: auto -> monitoring on 3.12+, else settrace)",
    )
    r.add_argument(
        "--events",
        default="call,line,return",
        help=f"comma-separated event kinds. available: {','.join(KINDS)}",
    )
    r.add_argument("-o", "--out", default="trace.jsonl", help="output path (.jsonl, .csv, .sqlite, .html, .dot, .svg)")
    r.add_argument("--format", default=None, choices=["jsonl", "csv", "sqlite", "text", "html", "dot", "svg"])
    r.add_argument("--locals", action="store_true", help="capture local variables at every event")
    r.add_argument("--stack", action="store_true", help="capture a stack snapshot at every event")
    r.add_argument("--stack-depth", type=int, default=8)
    r.add_argument("--args", action="store_true", help="capture call arguments and return values")
    r.add_argument("--include", default="", help="only trace files under this prefix")
    r.add_argument("--exclude", default="", help="never trace files under this prefix")
    r.add_argument("--interval", type=float, default=0.001, help="sampling interval in seconds")
    r.add_argument("--c-calls", action="store_true", help="include C-function events (profile engine only)")
    r.add_argument("--buffer", action="store_true", help="keep events in memory (needed for html/dot/svg)")
    r.add_argument("--min-flame", type=int, default=1, help="prune flame graph nodes below this weight")
    r.add_argument("--quiet", action="store_true")

    # --------------------------------------------------------------- report
    rp = sub.add_parser("report", help="summarise a trace file")
    rp.add_argument("trace", help="path to .jsonl or .sqlite trace")
    rp.add_argument("--html", help="write an HTML report here")
    rp.add_argument("--top", type=int, default=20)
    rp.add_argument("--min-flame", type=int, default=1)
    rp.add_argument("--no-flame", action="store_true")

    # ---------------------------------------------------------------- graph
    g = sub.add_parser("graph", help="render a call graph or flame graph")
    g.add_argument("trace")
    g.add_argument("--kind", default="flame", choices=["flame", "call", "both"])
    g.add_argument("-o", "--out-prefix", default="graph")
    g.add_argument("--min-calls", type=int, default=1)
    g.add_argument("--max-nodes", type=int, default=200)
    g.add_argument("--min-flame", type=int, default=1)

    # ----------------------------------------------------------------- diff
    d = sub.add_parser("diff", help="compare two traces")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--min-pct", type=float, default=1.0)
    d.add_argument("--json", action="store_true")

    # ---------------------------------------------------------------- query
    q = sub.add_parser("query", help="run SQL against a .sqlite trace")
    q.add_argument("db")
    q.add_argument("sql")

    # -------------------------------------------------------------- inspect
    i = sub.add_parser("inspect", help="dump frame/code-object details")
    i.add_argument("target", help="path/to/file.py[:funcname|:line]")
    i.add_argument("--json", action="store_true")
    i.add_argument("--limit", type=int, default=20)

    # ----------------------------------------------------------------- demo
    dmo = sub.add_parser("demo", help="run a built-in workload under a tracer")
    dmo.add_argument("--engine", default="auto", choices=["auto", "settrace", "monitoring", "sample", "profile"])
    dmo.add_argument("--html", help="also write an HTML report here")

    return p


# ----------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------


def _load_events(path: str) -> list:
    from framecraft.analysis.diff import load_jsonl
    from framecraft.sinks.machine import query_sqlite

    if path.endswith(".sqlite") or path.endswith(".db"):
        rows = query_sqlite(
            path,
            # code_hash lives on codes, not on events: the events table
            # stores only the integer id, which is the whole point of interning.
            # (Selecting events.code_hash is a natural mistake and fails with
            # "no such column", which is at least an honest error.)
            "SELECT e.seq, e.kind, e.perf_ns, e.wall, e.cpu, e.thread_id,"
            " e.thread_name, e.depth, e.call_id, e.parent_call_id,"
            " c.code_hash, e.lineno, e.offset, e.opcode, e.c_name,"
            " e.arg, e.retval, e.exception_type, e.exception,"
            " c.qualname, c.filename, c.name"
            " FROM events e LEFT JOIN codes c ON c.id = e.code_id"
            " ORDER BY e.seq",
        )
        from framecraft.core.events import TraceEvent

        out = []
        for r in rows:
            (
                seq, kind, perf_ns, wall, cpu, tid, tname, depth, cid, pcid,
                chash, lineno, offset, opcode, c_name, arg, retval, etype, exc,
                qualname, filename, name,
            ) = r
            # A column may legitimately be NULL in SQLite (we never wrote it for
            # this event kind).  TraceEvent treats wall/cpu/perf_ns as numbers and
            # `to_dict` formats them, so coerce here rather than letting a report
            # fail on `round(None, 9)`.
            ev = TraceEvent(
                seq=seq,
                kind=kind,
                wall=float(wall or 0.0),
                cpu=float(cpu or 0.0),
                perf_ns=int(perf_ns or 0),
            )
            ev.thread_id = int(tid or 0)
            ev.thread_name = tname or ""
            ev.depth = int(depth or 0)
            ev.call_id = cid
            ev.parent_call_id = pcid
            ev.code_hash = chash
            ev.lineno = lineno
            ev.offset = offset
            ev.opcode = opcode
            ev.c_name = c_name
            ev.arg_repr = arg
            ev.retval_repr = retval
            ev.exception_type = etype
            ev.exception_repr = exc
            ev.qualname = qualname or ""
            ev.filename = filename or ""
            ev.name = name or ""
            out.append(ev)
        return out
    return load_jsonl(path)


def _make_sink(path: str, fmt: str | None, *, buffer: bool, locs: bool, min_flame: int = 1):
    from framecraft.sinks import (
        CSVSink,
        DOTSink,
        JSONLSink,
        SQLiteSink,
        SVGFlameSink,
    )
    from framecraft.sinks.base import open_sink

    if not fmt:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        fmt = {"jsonl": "jsonl", "json": "jsonl", "csv": "csv", "sqlite": "sqlite",
               "db": "sqlite", "svg": "svg", "dot": "dot", "html": "html"}.get(ext, "jsonl")
    fmt = fmt.lower()

    if fmt == "jsonl":
        return JSONLSink(path=path, include_locals=locs, include_stack=False), fmt
    if fmt == "csv":
        return CSVSink(path=path), fmt
    if fmt == "sqlite":
        return SQLiteSink(path=path, reset=True), fmt
    if fmt == "svg":
        return SVGFlameSink(path, min_width_px=0.1), fmt
    if fmt == "dot":
        return DOTSink(path), fmt
    if fmt in ("html", "text"):
        return open_sink(sys.stdout), fmt
    raise SystemExit(f"unknown output format {fmt!r}")


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------


def cmd_run(ns: argparse.Namespace) -> int:
    from framecraft.trace import (
        FunctionTracer,
        MonitorTracer,
        ProfileTracer,
        SamplingTracer,
        TracerConfig,
    )

    kinds = tuple(k.strip() for k in ns.events.split(",") if k.strip())
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise SystemExit(f"unknown event kinds {bad}; available: {list(KINDS)}")

    engine = ns.engine
    if engine == "auto":
        engine = "monitoring" if HAS_MONITORING else "settrace"

    buffer_needed = ns.buffer or (ns.format or os.path.splitext(ns.out)[1].lower()) in (
        ".html", ".svg", ".dot"
    )

    cfg = TracerConfig(
        events=kinds,
        capture_locals=ns.locals,
        capture_stack=ns.stack,
        capture_stack_depth=ns.stack_depth,
        capture_args=ns.args,
        capture_return=ns.args,
        include_paths=(ns.include,) if ns.include else (),
        exclude_paths=(ns.exclude,) if ns.exclude else (),
        buffer=buffer_needed,
    )

    tracer: Any
    if engine == "monitoring":
        tracer = MonitorTracer(cfg)
    elif engine == "profile":
        tracer = ProfileTracer(cfg, trace_c=ns.c_calls)
    elif engine == "sample":
        tracer = SamplingTracer(cfg, interval=ns.interval)
    else:
        tracer = FunctionTracer(cfg, opcode_events=("opcode" in kinds))

    sink, fmt = _make_sink(
        ns.out, ns.format, buffer=buffer_needed, locs=ns.locals, min_flame=ns.min_flame
    )

    # For html / text we stream nothing and aggregate at the end, so the sink is
    # NullSink and the tracer buffer does the work.
    needs_sink = fmt not in ("html", "text")
    if needs_sink:
        tracer.add_sink(sink)

    # Everything from here on happens inside the try, so that *any* early exit
    # -- a missing script, a bad argument, an exception from the script itself --
    # still closes the sink and restores sys.argv/sys.path.  Opening the file and
    # then raising before the `finally` was attaching one leaked a handle per
    # failed invocation, which is the sort of thing that only shows up as a
    # ResourceWarning in someone else's test suite.
    try:
        target = ns.script
        if not os.path.exists(target):
            raise SystemExit(f"script not found: {target}")

        # The traced script gets a normal __main__ environment: sys.argv set, its
        # own directory on sys.path (so `import mymodule` works the way it would
        # if you ran `python myscript.py`).  Without the sys.path entry, tracing
        # a script that imports its own helpers fails for reasons that have
        # nothing to do with tracing -- and that is a confusing 20-minute
        # debugging session.
        saved_argv = sys.argv
        saved_path = list(sys.path)
        script_dir = os.path.dirname(os.path.abspath(target))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        sys.argv = [target, *ns.script_args]

        exit_code = 0
        started = False
        try:
            with tracer:
                started = True
                runpy.run_path(target, run_name="__main__")
        except SystemExit as exc:
            # A script calling sys.exit() is normal, not a failure of the tracer.
            # Anything else non-zero we propagate; anything non-int we collapse to
            # 0 because a bare `raise SystemExit("message")` is a success exit.
            exit_code = exc.code if isinstance(exc.code, int) else 0
        finally:
            if started:
                tracer.stop()
            sys.argv = saved_argv
            sys.path[:] = saved_path
    finally:
        _finish(tracer, sink, fmt, ns)
    return exit_code


def _is_standard_stream(obj: Any) -> bool:
    return obj in (sys.stdout, sys.stderr, sys.stdin)


def _finish(tracer: Any, sink: Any, fmt: str, ns: argparse.Namespace) -> None:
    from framecraft.sinks.report import HTMLReport
    from framecraft.sinks.text import TextSink

    # Only close what we opened.  `open_sink(sys.stdout)` returns stdout itself,
    # and closing it breaks every later write in the process -- which is how a
    # CLI can leave pytest's capture permanently dead.
    if getattr(sink, "_owns_stream", True) is not False and not _is_standard_stream(sink):
        closer = getattr(sink, "close", None)
        if callable(closer):
            try:
                closer()
            except TypeError:  # pragma: no cover
                pass

    if fmt == "html":
        report = HTMLReport(
            tracer.events, dropped=tracer.dropped, errors=tracer.error_count,
            include_locals=ns.locals, min_flame_weight=ns.min_flame,
        )
        report.write(ns.out)
        if not ns.quiet:
            print(f"wrote {ns.out} ({len(tracer.events):,} events)")
    elif fmt == "text":
        ts = TextSink(sys.stdout)
        for ev in tracer.events:
            ts.write(ev)
        if not ns.quiet:
            import json as _json

            print(_json.dumps(tracer.stats(), indent=2))
    else:
        if not ns.quiet:
            print(f"wrote {ns.out} ({tracer.stats()})", file=sys.stderr)


# ----------------------------------------------------------------------
# report / graph / diff / query / inspect / demo
# ----------------------------------------------------------------------


def cmd_report(ns: argparse.Namespace) -> int:
    from framecraft.analysis import (
        exception_summary,
        exclusive_times,
        hot_lines,
        render_table,
        summarise_events,
    )

    events = _load_events(ns.trace)
    s = summarise_events(events)
    print(f"TRACE {ns.trace}")
    print("=" * 78)
    print(f"  events        {s['total_events']:,}")
    print(f"  span          {s['span_s']:.4f}s  ({s['events_per_s']:,.0f} events/s)")
    print(f"  kinds         {s['events_by_kind']}")
    print(f"  threads       {s['threads']}   files {s['distinct_files']}   functions {s['distinct_code_objects']:,}")
    print(f"  coverage      {s['coverage_ratio'] * 100:.4f}%  dropped={s['dropped']}  errors={s['errors']}")
    if s["dropped"] or s["errors"]:
        print("  !! trace is INCOMPLETE -- all percentages are lower bounds")

    print("\nHOT LINES")
    print(render_table(hot_lines(events, limit=ns.top), ["location", "count", "pct"]))
    print("\nEXCLUSIVE TIME")
    print(
        render_table(
            exclusive_times(events, limit=ns.top),
            ["name", "calls", "total_ms", "self_ms", "pct_total", "pct_self"],
        )
    )
    exc = exception_summary(events)
    if exc:
        print("\nEXCEPTIONS")
        print(render_table(exc, ["type", "raised_in", "location", "count"]))
    if ns.html:
        from framecraft.sinks.report import HTMLReport

        HTMLReport(events, title=f"report: {ns.trace}", dropped=s["dropped"], errors=s["errors"]).write(ns.html)
        print(f"\nwrote {ns.html}")
    return 0


def cmd_graph(ns: argparse.Namespace) -> int:
    from framecraft.graph import build_call_graph, build_stack_tree
    from framecraft.sinks.dot import callgraph_dot, render_dot
    from framecraft.sinks.flame import flame_svg

    events = _load_events(ns.trace)
    if ns.kind in ("flame", "both"):
        tree = build_stack_tree(events, min_weight=ns.min_flame)
        path = f"{ns.out_prefix}-flame.svg"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(flame_svg(tree, title=f"flame: {ns.trace}", unit="line events"))
        print(f"wrote {path}  ({tree.total:,} events, {len(list(tree.walk()))} nodes)")
    if ns.kind in ("call", "both"):
        graph = build_call_graph(events)
        dot = callgraph_dot(graph, name=ns.out_prefix, min_calls=ns.min_calls, max_nodes=ns.max_nodes)
        path = f"{ns.out_prefix}-call.dot"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dot)
        rendered = render_dot(dot, f"{ns.out_prefix}-call")
        print(f"wrote {path}  ({len(graph)} nodes, {len(graph.edges)} edges)")
        if rendered:
            print(f"wrote {rendered}")
        else:
            print("note: graphviz 'dot' not found; wrote DOT source only")
    return 0


def cmd_diff(ns: argparse.Namespace) -> int:
    from framecraft.analysis import diff_traces, render_diff

    d = diff_traces(
        _load_events(ns.before), _load_events(ns.after),
        label_before=os.path.basename(ns.before), label_after=os.path.basename(ns.after),
        min_pct_delta=ns.min_pct,
    )
    if ns.json:
        print(json.dumps(d, indent=2))
    else:
        print(render_diff(d))
    return 0 if d["comparable"] else 1


def cmd_query(ns: argparse.Namespace) -> int:
    from framecraft.sinks.machine import query_sqlite

    try:
        rows = query_sqlite(ns.db, ns.sql)
    except Exception as exc:
        raise SystemExit(f"SQL error: {exc}") from exc
    if not rows:
        print("(no rows)")
        return 0
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)))
    print(f"({len(rows)} rows)")
    return 0


def cmd_inspect(ns: argparse.Namespace) -> int:
    from framecraft.analysis import render_table
    from framecraft.core.frames import describe

    path, spec = _split_inspect_target(ns.target)

    if not os.path.exists(path):
        raise SystemExit(f"file not found: {path}")

    tree = _module_code(path)

    wanted_code = None
    if spec.isdigit():
        lineno = int(spec)
        for code in _walk_code(tree):
            if code.co_name == "<module>":
                wanted_code = _innermost_at(code, lineno)
                break
    elif not spec:
        # No spec means the module.  Note that co_qualname.endswith("") is True
        # for *every* code object, so an unguarded name comparison silently picks
        # the last one walked rather than the module.
        wanted_code = tree
    else:
        name = spec
        for code in _walk_code(tree):
            if code.co_name == name or getattr(code, "co_qualname", "").endswith("." + name):
                wanted_code = code
                break
        if wanted_code is None:
            names = sorted({c.co_qualname for c in _walk_code(tree)})
            raise SystemExit(f"no code object named {name!r} in {path}. available: {names}")

    if wanted_code is None:
        raise SystemExit(f"could not find a code object for {spec!r} in {path}")

    # Run the module, catch the live frame, describe it.  If the target function
    # was never called (e.g. `inspect app.py:never_called`) there is no frame,
    # and we say so rather than reporting a frame of `None`.
    global target_path
    target_path = os.path.abspath(path)
    frame = _capture_frame_for(tree)

    # A frame only exists for code that is *currently executing* (or was, when
    # we grabbed it).  For `file.py::some_function` that is almost never the
    # function you asked about: it returns, and then the module finishes.  So the
    # frame we hold is the module's, and printing its details under a heading
    # that says `some_function` is a lie.  Report the code object we were asked
    # about, and say plainly that there was no live frame for it.
    if frame is None or frame.f_code is not wanted_code:
        from framecraft._internal import codeinfo as _ci

        info = _ci.describe_code(wanted_code)
        note = (
            "no live frame: this code object was never executing when the "
            "module's frame was captured"
            if frame is not None
            else "no live frame: this code object was never entered"
        )
        if ns.json:
            print(json.dumps({"code": info, "note": note}, indent=2, default=str))
            return 0
        print(f"CODE OBJECT  {info['qualname']}   id={info['id']} hash={info['hash']}")
        print(f"  file            {info['filename']}")
        print(f"  first line      {info['firstlineno']}")
        print(f"  args            {info['argcount']} positional, "
              f"{info['kwonlyargcount']} kw-only, {info['posonlyargcount']} pos-only")
        print(f"  varnames        {info['varnames']}")
        print(f"  cellvars        {info['cellvars']}   freevars {info['freevars']}")
        print(f"  stacksize       {info['stacksize']} slots ({info['stacksize_bytes']} bytes)")
        print(f"  bytecode        {info['size_bytes']} bytes, {info['line_spans']} line spans")
        print()
        print(f"  note: {note}.")
        print("        So this is the *code object* only -- there is no f_lasti,")
        print("        no f_locals and no stack position for it, because those are")
        print("        properties of a frame, not of code.  Use --json for the full")
        print("        description, or trace the function and look at the call event.")
        return 0

    result = describe(frame)
    if ns.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    code = result["code"]
    print(f"CODE OBJECT  {code['qualname']}   id={code['id']} hash={code['hash']}")
    print("=" * 78)
    print(f"  file            {code['filename']}")
    print(f"  first line      {code['firstlineno']}")
    print(f"  args            {code['argcount']} positional, {code['kwonlyargcount']} kw-only, {code['posonlyargcount']} pos-only")
    print(f"  varnames        {code['varnames']}")
    print(f"  cellvars        {code['cellvars']}   freevars {code['freevars']}")
    print(f"  names           {code['names'][:20]}{' ...' if len(code['names']) > 20 else ''}")
    print(f"  consts          {code['consts']} entries")
    print(f"  flags           {code['flags']}  gen={code['is_generator']} coro={code['is_coroutine']} asyncgen={code['is_async_generator']}")
    print(f"  stacksize       {code['stacksize']} slots ({code['stacksize_bytes']} bytes)")
    print(f"  bytecode        {code['size_bytes']} bytes, {code['line_spans']} line spans")
    if "exception_table_bytes" in code:
        print(f"  exception tbl   {code['exception_table_bytes']} bytes (3.11+ zero-cost)")
    ip = result["instruction_pointer"]
    print(f"  f_lasti         {ip['f_lasti']}  -> line {ip['line_at_lasti']}")
    th = result["trace_hooks"]
    print(f"  trace hooks     f_trace={th['f_trace']} lines={th['f_trace_lines']} opcodes={th['f_trace_opcodes']}")
    print(f"\nINSTRUCTIONS (first {ns.limit}, caches elided)")
    print(render_table(result.get("instructions", [])[: ns.limit], ["offset", "opname", "arg", "line", "col", "argrepr"]))
    if result.get("exception_table"):
        print("\nEXCEPTION TABLE")
        print(render_table(result["exception_table"], ["start", "end", "target", "depth", "lasti"]))
    return 0


def _split_inspect_target(target: str) -> tuple[str, str]:
    """Split ``file.py``, ``file.py:123`` or ``file.py::name`` into (path, spec).

    Plain ``rpartition(":")`` is wrong for the ``::func`` form: it splits at the
    *last* colon, leaving ``"x.py:"`` as the path -- which then fails with
    "file not found: x.py:" and a message that does not mention the syntax you
    used.  So try ``::`` first, then a single ``:`` that is not a drive letter.

    The drive-letter guard is not paranoia: on Windows a target such as
    ``C:/src/app.py:42`` has genuinely ambiguous colons, and a naive split
    produces the path ``"C"``.
    """
    if "::" in target:
        path, _, spec = target.rpartition("::")
        return path, spec
    # Split at the last colon that is not part of a Windows drive letter.
    idx = -1
    for i, ch in enumerate(target):
        if ch != ":":
            continue
        if i == 1 and target[0].isalpha():  # "C:\..."
            continue
        idx = i
    if idx <= 0:
        return target, ""
    return target[:idx], target[idx + 1:]


def _walk_code(code: Any):
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            yield from _walk_code(const)


def _innermost_at(code: Any, lineno: int) -> Any:
    best = code
    for child in code.co_consts:
        if hasattr(child, "co_code") and child.co_firstlineno <= lineno:
            best = _innermost_at(child, lineno)
    return best


def _capture_frame_for(code: Any) -> Any:
    """Execute the module and return a **live frame** whose ``f_code`` is ``code``.

    A code object and a frame are different things (see
    :mod:`framecraft._internal.codeinfo`), and ``f_lasti``/``f_lineno``/``f_locals``
    only exist on the frame.  ``inspect`` wants both, so it has to actually *run*
    the code.

    We get the frame the same way a debugger does: install a global trace
    function, let the module execute, and grab the frame at the moment the
    target code object is entered.  Returning ``None`` from the global trace
    function then declines to trace anything else, so the cost is one identity
    comparison per call for the length of the module's execution.

    This is also the smallest honest demonstration of the ``settrace``
    two-level protocol, which is why the function lives in a CLI rather than in
    the library.

    Parameters
    ----------
    code:
        The **already-compiled** module code object.  It must be the very object
        the caller inspected: the check below is ``frame.f_code is code``, and
        recompiling produces a different object, so a second compile here makes
        the lookup silently never match and ``inspect`` reports "never
        executed" for a module that obviously just ran.
    """
    captured: list = []

    def global_trace(frame, event, arg):
        if frame.f_code is code and not captured:
            captured.append(frame)
            sys.settrace(None)  # stop; we already have what we came for
        return None

    # `inspect` has to *run* the module, and the module may print.  Its stdout is
    # redirected to stderr for the duration so that `--json` emits parseable
    # JSON: a tool whose machine-readable mode is corrupted by the program being
    # inspected is not machine-readable.
    sys.settrace(global_trace)
    saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        glb = {"__name__": "__framecraft_probe__", "__file__": target_path}
        exec(code, glb, glb)
    except BaseException:
        # A module that fails on import still gave us a module frame, and that
        # frame is often exactly what you wanted to inspect.  Swallow it and let
        # the caller report "no live frame" only if we really got nothing.
        pass
    finally:
        sys.stdout = saved_stdout
        sys.settrace(None)
    return captured[0] if captured else None


def _module_code(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return compile(fh.read(), path, "exec", dont_inherit=True)


def cmd_demo(ns: argparse.Namespace) -> int:
    import time

    from framecraft.trace import (
        FunctionTracer,
        MonitorTracer,
        ProfileTracer,
        SamplingTracer,
        TracerConfig,
    )

    def fib(n: int) -> int:
        return n if n < 2 else fib(n - 1) + fib(n - 2)

    def parse(text: str) -> list:
        return [int(p) for p in text.split(",") if p.strip()]

    def transform(rows: list) -> list:
        out = []
        for r in rows:
            try:
                out.append(1 / r)
            except ZeroDivisionError:
                out.append(float("inf"))
        return out

    def render(rows: list) -> str:
        return "\n".join(f"{r:.4f}" for r in rows)

    def workload() -> str:
        rows = parse("1,2,3,4,0,6")
        return render(transform(rows))

    engine = ns.engine
    if engine == "auto":
        engine = "monitoring" if HAS_MONITORING else "settrace"
    cfg = TracerConfig(events=("call", "line", "return", "exception"), buffer=True, capture_locals=True)
    tracer = {
        "settrace": lambda: FunctionTracer(cfg),
        "monitoring": lambda: MonitorTracer(cfg),
        "profile": lambda: ProfileTracer(cfg),
        "sample": lambda: SamplingTracer(cfg, interval=0.0005),
    }[engine]()

    print(f"fc-trace demo -- engine={engine}")
    print(f"python {sys.version.split()[0]}  monitoring={HAS_MONITORING}")
    print("-" * 78)
    with tracer:
        time.sleep(0.02)
        out = workload()
    print(out)
    print("-" * 78)
    print(json.dumps(tracer.stats(), indent=2))

    from framecraft.analysis import exclusive_times, hot_lines, render_table

    print("\nHOT LINES")
    print(render_table(hot_lines(tracer.events, limit=8), ["location", "count", "pct"]))
    if hasattr(tracer, "total_samples"):
        print(f"\nsamples: {tracer.total_samples:,}  effective {tracer.effective_hz:.0f} Hz")
    else:
        print("\nEXCLUSIVE TIME")
        print(render_table(exclusive_times(tracer.events, limit=8), ["name", "calls", "total_ms", "self_ms", "pct_total"]))

    if ns.html:
        from framecraft.sinks.report import HTMLReport

        HTMLReport(tracer.events, title="fc-trace demo", dropped=tracer.dropped, errors=tracer.error_count).write(
            ns.html
        )
        print(f"\nwrote {ns.html}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = list(argv) if argv is not None else sys.argv[1:]

    # For `run`, anything the run subcommand does not recognise belongs to the
    # traced script.  A literal `--` is honoured as an explicit separator and is
    # not forwarded, so `run app.py -- -v` gives the script `['-v']`.
    unknown: list[str] = []
    if args and args[0] == "run":
        ns, unknown = parser.parse_known_args(args)
        if unknown and unknown[0] == "--":
            unknown = unknown[1:]
        ns.script_args = unknown
    else:
        ns = parser.parse_args(args)

    handlers = {
        "run": cmd_run,
        "report": cmd_report,
        "graph": cmd_graph,
        "diff": cmd_diff,
        "query": cmd_query,
        "inspect": cmd_inspect,
        "demo": cmd_demo,
    }
    return handlers[ns.cmd](ns)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
