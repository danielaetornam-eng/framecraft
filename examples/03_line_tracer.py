#!/usr/bin/env python3
"""Example 03 -- ``FunctionTracer``: the same trace, without the footguns.

Run me::

    python examples/03_line_tracer.py
    python examples/03_line_tracer.py --html out.html
    python examples/03_line_tracer.py --jsonl trace.jsonl

Example 02 built a tracer out of raw ``sys.settrace`` and had to defend against
four traps by hand.  ``FunctionTracer`` is that same tracer with the defences
built in.  This example shows what you get, and demonstrates the two knobs that
matter most: **what to trace** and **what to capture**.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

from framecraft import CALL, LINE, RETURN, FunctionTracer, TracerConfig
from framecraft.analysis import exclusive_times, hot_lines, render_table, summarise_events
from framecraft.sinks import JSONLSink, SQLiteSink, TextSink

# ----------------------------------------------------------------------
# A workload worth tracing.
# ----------------------------------------------------------------------


def parse_csv(text: str) -> list[tuple[str, int]]:
    """Splits and validates. Has a loop and a try/except."""
    rows = []
    for chunk in text.split(","):
        name, _, raw = chunk.partition("=")
        try:
            rows.append((name, int(raw)))
        except ValueError:
            continue
    return rows


def enrich(rows: list[tuple[str, int]]) -> list[tuple[str, int, int]]:
    """Per-row work; calls a helper so there is a nested frame."""
    out = []
    for name, value in rows:
        out.append((name, value, value * value))
    return out


def checksum(rows: list) -> int:
    """Leaf-ish: a comprehension and a sum."""
    total = 0
    for _name, value, squared in rows:
        total = total + value + squared
    return total


def summarise(rows: list) -> str:
    """Calls into the standard library, so the trace has foreign frames in it.

    Without this, `workload()` is 100% local code and every filtering option
    looks identical -- which is exactly the kind of demo that teaches nothing.
    """
    values = [v for _n, v, _s in rows]
    mean = statistics.fmean(values)
    return json.dumps({"n": len(values), "mean": round(mean, 3), "sorted": sorted(values)})


def workload() -> int:
    text = "a=1,b=2,c=3,broken,d=4,e=5"
    rows = parse_csv(text)
    enriched = enrich(rows)
    total = checksum(enriched)
    summarise(enriched)
    return total


# ----------------------------------------------------------------------


def demo_basic() -> None:
    print("=" * 78)
    print("A. The basics: context manager, one call site of the library")
    print("=" * 78)
    print("""
    with FunctionTracer(events=("call", "line", "return")) as t:
        workload()

    for ev in t.events:
        print(ev.summary())
""")
    with FunctionTracer(events=(CALL, LINE, RETURN)) as t:
        workload()

    ts = TextSink(kinds=(CALL, RETURN), colour=False)
    ts.stream = sys.stdout
    for ev in t.events:
        ts.write(ev)
    print()
    print(f"  {len(t.events):,} events, dropped={t.dropped}, errors={t.error_count}")
    print()
    print("  Note there is no output from framecraft's own machinery even though")
    print("  the tracer's `stop()` runs inside the with-block: FunctionTracer")
    print("  excludes its own package (framecraft/_internal, core, trace) so the")
    print("  teardown frames cannot swamp your trace.")
    print()
    print("  And note this trace is INCOMPLETE for anything outside this file:")
    print("  list.append, print, int() are all C or foreign code. Example 05 and")
    print("  06 fix that.")


def demo_filtering() -> None:
    print()
    print("=" * 78)
    print("B. Filtering: WHERE is decided once per call, not per line")
    print("=" * 78)
    print("""
    TracerConfig(
        include_paths=(...),   # only files under this prefix
        exclude_paths=(...),   # never these (wins over everything)
        include=...,           # arbitrary predicate on the code object
        exclude=...,
    )
""")
    this_file = __file__.replace("\\", "/")
    configs = [
        ("no filter (whole process)", {}),
        ("include_paths=[this file]", {"include_paths": (this_file,)}),
        ("exclude_paths=[stdlib dir]", {"exclude_paths": (_stdlib_dir(),)}),
    ]
    for label, kwargs in configs:
        cfg = TracerConfig(events=(CALL, RETURN), **kwargs)
        with FunctionTracer(cfg) as t:
            workload()
        funcs = sorted({e.qualname for e in t.events if e.kind == CALL})
        files = len({e.filename for e in t.events if e.kind == CALL})
        print(f"  {label:<28} events={len(t.events):>5}  functions={len(funcs):>3}  files={files}")
        if len(funcs) <= 12:
            print(f"      {funcs}")
    print()
    print("  Read the `functions` column, not the `events` column. Without a")
    print("  filter you instrument statistics.fmean and json.dumps -- Python")
    print("  frames in the stdlib, whose line events you did not ask for and")
    print("  cannot act on. With include_paths you get exactly your code.")
    print()
    print("  The cost of an event is not uniform: a `call` event costs a Python")
    print("  function call plus a `TracerConfig.admits()` check; a `line` event")
    print("  costs the same check *per line*. So the filter is applied in the")
    print("  GLOBAL hook, once per call, and declined frames are never armed at")
    print("  all. That is why `include_paths` is a first-class knob and not a")
    print("  `filter=` callback.")


def _stdlib_dir() -> str:
    import os
    import sysconfig

    return (sysconfig.get_paths()["stdlib"] or os.path.dirname(os.__file__)).replace("\\", "/")


def demo_capture() -> None:
    print()
    print("=" * 78)
    print("C. Capture: what to pay for")
    print("=" * 78)
    variants = [
        ("locations only", {}),
        ("+ locals", {"capture_locals": True}),
        ("+ stack snapshot", {"capture_stack": True}),
        ("+ call args and returns", {"capture_args": True, "capture_return": True}),
    ]
    for label, kwargs in variants:
        cfg = TracerConfig(events=(CALL, LINE, RETURN), **kwargs)
        t0 = time.perf_counter()
        with FunctionTracer(cfg) as tr:
            workload()
        wall = time.perf_counter() - t0
        n_locals = sum(len(e.locals) for e in tr.events)
        print(
            f"  {label:<26} events={len(tr.events):>4}  locals_captured={n_locals:>4}  "
            f"traced_wall={wall * 1000:7.2f} ms"
        )
    print()
    print("  Reading that table:")
    print("   * 'locals' is the expensive one. On Python < 3.13 every read of")
    print("     frame.f_locals builds a brand-new dict by walking every name in")
    print("     the frame. For a module-level frame that dictionary is your entire")
    print("     program's globals.")
    print("   * 'stack snapshot' is O(depth) per event, so it is quadratic-ish.")
    print("     Only turn it on when you are chasing a specific event.")
    print("   * args/returns are cheap and are reconstructed from co_varnames,")
    print("     not from f_locals -- see FunctionTracer._args_repr.")


def demo_locals_detail() -> None:
    print()
    print("=" * 78)
    print("D. What captured locals actually look like")
    print("=" * 78)
    with FunctionTracer(events=(LINE,), capture_locals=True, include=lambda c: c.co_name == "checksum") as t:
        workload()
    shown = 0
    for ev in t.events:
        if not ev.locals:
            continue
        print(f"  {ev.location}  depth={ev.depth}   (iteration {shown + 1})")
        for b in ev.locals:
            print(f"      {b.scope:<7} {b.name:<9} = {b.value[:70]}")
        shown += 1
        if shown >= 3:
            break
    print()
    last = t.events[-1]
    print("  The same bindings, structurally (the form a sink can index):")
    import json as _json

    for b in last.locals[:2]:
        print(f"      {b.name:<9} {_json.dumps(b.structured)[:150]}...")
    print()
    print("  Two things to notice:")
    print("   * `scope` comes from a faithful re-implementation of CPython name")
    print("     resolution: co_varnames -> cellvars/freevars -> globals ->")
    print("     builtins. A tool that only looks in f_globals will report `len`")
    print("     as missing, because `len` is a builtin.")
    print("   * the loop variables `_name`, `value`, `squared` are co_varnames")
    print("     too, so they show up. They are the reason a naive")
    print("     `f_locals`-only capture of a module frame is often thousands of")
    print("     entries long: for `<module>`, co_varnames contains every global.")


def demo_analysis() -> None:
    print()
    print("=" * 78)
    print("E. Analysis: the trace becomes an answer")
    print("=" * 78)
    with FunctionTracer(events=(CALL, LINE, RETURN)) as t:
        for _ in range(20):
            workload()
    print("  summary")
    for k, v in summarise_events(t.events).items():
        print(f"    {k:<24} {v}")
    print()
    print("  hot lines")
    print(render_table(hot_lines(t.events, limit=8), ["location", "count", "pct"]))
    print()
    print("  exclusive time (wall clock, excluding callees)")
    print(render_table(exclusive_times(t.events, limit=8), ["name", "calls", "total_ms", "self_ms", "pct_total"]))


def demo_streaming() -> None:
    print()
    print("=" * 78)
    print("F. Streaming: sinks, and why buffering is opt-in")
    print("=" * 78)
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="framecraft-03-"))
    jl = tmp / "trace.jsonl"
    sq = tmp / "trace.sqlite"
    print(f"""
    t = FunctionTracer(events=("call","line","return"), buffer=False)
    t.add_sink(JSONLSink(path={jl.name!r}, include_locals=True))
    t.add_sink(SQLiteSink(path={sq.name!r}))
    with t:
        workload()
""")
    t = FunctionTracer(events=(CALL, LINE, RETURN), buffer=False)
    t.add_sink(JSONLSink(path=str(jl), include_locals=True))
    t.add_sink(SQLiteSink(path=str(sq), reset=True))
    with t:
        for _ in range(5):
            workload()
    print(f"  jsonl   {jl.stat().st_size:>9,} bytes")
    print(f"  sqlite  {sq.stat().st_size:>9,} bytes  (WAL not yet checkpointed)")
    print(f"  in-memory buffer kept: {len(t.events):,} events  (buffer=False)")
    print()
    print("  For a program that runs for an hour, buffering a line trace is a")
    print("  memory leak with a debugger's licence: at ~1M events/minute you")
    print("  hold a quarter of a million dataclasses per minute of runtime.")
    print("  buffer=False plus a streaming sink is the only scalable answer.")

    from framecraft.analysis import load_jsonl

    reloaded = load_jsonl(str(jl))
    print()
    print(f"  round-tripped {len(reloaded):,} events back from JSONL "
          f"(first: {reloaded[0].summary()})")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", help="also write a JSONL trace here")
    ap.add_argument("--html", help="also write an HTML report here")
    ap.add_argument("--only", choices=["a", "b", "c", "d", "e", "f"], help="run one section")
    ns = ap.parse_args()

    sections = {
        "a": demo_basic,
        "b": demo_filtering,
        "c": demo_capture,
        "d": demo_locals_detail,
        "e": demo_analysis,
        "f": demo_streaming,
    }
    for key, fn in sections.items():
        if ns.only and ns.only != key:
            continue
        fn()

    if ns.jsonl or ns.html:
        with FunctionTracer(events=(CALL, LINE, RETURN), capture_locals=True) as t:
            workload()
        if ns.jsonl:
            s = JSONLSink(path=ns.jsonl, include_locals=True)
            for ev in t.events:
                s.write(ev)
            s.close()
            print(f"\nwrote {ns.jsonl}")
        if ns.html:
            from framecraft.sinks import HTMLReport

            HTMLReport(t.events, dropped=t.dropped, errors=t.error_count,
                       include_locals=True).write(ns.html)
            print(f"wrote {ns.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
