#!/usr/bin/env python3
"""Example 06 -- PEP 669 (``sys.monitoring``): the hook that was designed.

Run me::

    python examples/06_pep669_monitor.py
    python examples/06_pep669_monitor.py --engines      # compare with sys.settrace

Requires Python 3.12+.  On older interpreters the script explains and exits.

Why this matters
----------------

Everything in examples 02-05 is built on ``sys.settrace``, which grew a
line-event callback in 1999 and has carried the same rough edges ever since.  PEP
669 replaces it with an explicit registry:

*   a **tool id** (an integer; you claim one with ``use_tool_id``);
*   **named events** -- ``PY_START``, ``PY_RETURN``, ``PY_YIELD``, ``PY_UNWIND``,
    ``LINE``, ``CALL``, ``C_CALL``, ``C_RETURN``, ``EXCEPTION``, ``BRANCH``, ``GC``;
*   **per-code-object selection** via ``set_local_events``, checked in C;
*   **coexistence** -- pdb, coverage.py and your tool all work at once, on
    different tool ids.

Three things it gives you that ``sys.settrace`` cannot
-----------------------------------------------------

1.  **Arguments and return values, for free.**  ``PY_RETURN`` hands you the
    return value.  ``CALL`` and ``C_CALL`` hand you the callee *and* its first
    argument.  With ``sys.settrace`` you must reconstruct a signature from
    ``co_varnames`` and ``f_locals`` -- and you can never see a C call's
    arguments at all.

2.  **Exception unwinding as a first-class event.**  ``PY_UNWIND`` fires with the
    in-flight exception *before* the frame is destroyed.  That is the only
    moment at which "what did this frame look like when it failed" is answerable
    cheaply.  ``sys.settrace`` gives you ``'exception'``, but the locals are
    already in flux.

3.  **Per-function opt-in that costs nothing.**  ``set_local_events`` writes into
    the interpreter's own dispatch table.  ``settrace``'s equivalent is a Python
    predicate call per line.

The version story, which is genuinely messy
-------------------------------------------

PEP 669 landed in 3.12 and the event set has churned since:

===============  =====================================================
version          what changed
===============  =====================================================
3.12             initial: PY_START/PY_RETURN/PY_YIELD/PY_UNWIND/LINE/
                 CALL/C_CALL/C_FUNCTION/C_FUNCTION_RETURN/C_RETURN/
                 C_RAISE/EXCEPTION/BRANCH/GC
3.14             **C_CALL, C_FUNCTION, C_FUNCTION_RETURN REMOVED**,
                 replaced by ``INSTRUCTION`` (PEP 749); added
                 PY_RESUME/JUMP/BRANCH_LEFT/BRANCH_RIGHT/STOP_ITERATION/
                 RAISE/EXCEPTION_HANDLED/PY_THROW/RERAISE; reserved
                 tool ids grew (PROFILER_ID=2, OPTIMIZER_ID=5), leaving
                 only 3 and 4 for third parties
3.14.6           ``set_events`` raises
                 ``ValueError: cannot set C_RETURN or C_RAISE events
                 independently`` unless an unrelated CALL callback is
                 also registered
===============  =====================================================

Every one of those is handled in ``framecraft._internal.compat`` and
``MonitorTracer``, and demonstrated below.  That is the pattern to copy: probe
``sys.monitoring.events`` for what exists, register ``*args`` callbacks, and
unpack by position with a length check.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from framecraft._internal.compat import (
    DISABLE_EVENTS,
    HAS_MONITORING,
    RESERVED_TOOL_IDS,
    free_tool_ids,
    monitoring_event,
    monitoring_events,
)

if HAS_MONITORING:
    from framecraft import (
        C_RETURN,
        CALL,
        CALL_SITE,
        EXCEPTION,
        LINE,
        RETURN,
        MonitorTracer,
        TracerConfig,
    )


def show_capabilities() -> None:
    print("=" * 78)
    print("1. WHAT THIS INTERPRETER SUPPORTS")
    print("=" * 78)
    print(f"  python                     {sys.version.split()[0]}")
    print(f"  sys.monitoring present     {HAS_MONITORING}")
    if not HAS_MONITORING:
        print()
        print("  PEP 669 requires 3.12+. Everything below is skipped; use")
        print("  FunctionTracer (example 03) instead. framecraft's MonitorTracer")
        print("  raises a RuntimeError at construction rather than failing later.")
        return
    print(f"  reserved tool ids          {sorted(RESERVED_TOOL_IDS)}")
    print(f"  free tool ids              {free_tool_ids()}   <- only two on 3.14")
    print(f"  events present             {len(monitoring_events())}")
    for name in sorted(monitoring_events()):
        print(f"      {name:<20} {monitoring_event(name):#06x}")
    print()
    print("  Compare with PEP 669 as written, and note what is gone:")
    for gone in ("C_CALL", "C_FUNCTION", "C_FUNCTION_RETURN"):
        print(f"      {gone:<20} {'present' if gone in monitoring_events() else 'REMOVED in 3.14'}")
    print()
    print(f"  DISABLE_EVENTS constant   {DISABLE_EVENTS}")
    print("  NB: `sys.monitoring.DISABLE` is a bare object() sentinel and")
    print("  set_events(tid, sys.monitoring.DISABLE) raises TypeError. The value")
    print("  that actually works is plain 0. framecraft papers over this once, in")
    print("  compat.DISABLE_EVENTS, so no caller has to know.")


def demo_events() -> None:
    if not HAS_MONITORING:
        return
    print()
    print("=" * 78)
    print("2. THE SAME TRACE VIA PEP 669")
    print("=" * 78)

    def leaf(a, b=2):
        c = a + b
        if c > 100:
            raise ValueError(f"too big: {c}")
        return c * 2

    def middle(x):
        return leaf(x) + leaf(x + 1)

    def top():
        try:
            return middle(1000)
        except ValueError:
            return -1

    with MonitorTracer(events=(CALL, LINE, RETURN, EXCEPTION), capture_args=True, capture_locals=True) as t:
        top()
    print(f"  {len(t.events)} events, errors={t.error_count}, "
          f"unavailable={sorted(t.unavailable_events)}")
    print()
    for ev in t.events:
        phase = ev.extra.get("phase", "")
        extra = f" [{phase}]" if phase else ""
        print(f"    {ev.summary()}{extra}")
    print()
    print("  Compare with example 03's output for the same shape of program:")
    print("   * PEP 669 delivers `EXCEPTION` at the raise site AND `PY_UNWIND`")
    print("     (phase='unwind') as each frame is torn down, so you can see the")
    print("     failure propagate -- one event per frame, in order.")
    print("   * call arguments are reconstructed from the live frame here too")
    print("     (MonitorTracer._args_repr) because PY_START has no signature.")


def demo_call_event() -> None:
    if not HAS_MONITORING:
        return
    print()
    print("=" * 78)
    print("3. THE CALL EVENT: who called whom, with arguments")
    print("=" * 78)

    def helper(x):
        return x * 2

    def caller(n):
        total = 0
        for i in range(n):
            total += helper(i)
        return total

    with MonitorTracer(events=(CALL_SITE,), capture_args=True) as t:
        caller(3)
    print("  events with kind='call_site' carry the CALLEE and its FIRST ARG:")
    for ev in t.events:
        print(f"    {ev.location:<28} callee={ev.c_name:<10} arg0={ev.arg_repr}")
    print()
    print("  Nothing else in Python gives you this:")
    print("    sys.settrace 'call'  -> (frame, 'call', None).  No callee. To find")
    print("                             it you must disassemble the calling")
    print("                             instruction and resolve the operand.")
    print("    sys.setprofile 'call'-> (frame, 'call', None).  Same problem.")
    print("    sys.monitoring CALL  -> (code, offset, callable, arg0).  Done.")
    print()
    print("  This is the event you want for dependency analysis, 'why is this")
    print("  being called', and building a call graph without guessing.")


def demo_per_code() -> None:
    if not HAS_MONITORING:
        return
    print()
    print("=" * 78)
    print("4. PER-CODE-OBJECT EVENTS: the feature settrace does not have")
    print("=" * 78)

    def noisy():
        for _ in range(50):
            pass

    def interesting(x):
        y = x * 2
        z = y + 1
        return z

    with MonitorTracer(events=()) as t:
        # Globally silent. Then arm exactly one function.
        t.watch_code(interesting.__code__, ("line", "call", "return"))
        noisy()
        interesting(5)
    counts = Counter((e.qualname, e.kind) for e in t.events)
    print(f"  tracer was globally quiet; total events: {len(t.events)}")
    for (name, kind), n in sorted(counts.items()):
        print(f"    {name:<12} {kind:<8} {n}")
    print()
    print("  `set_local_events` writes into the interpreter's dispatch table, so")
    print("  this costs nothing at event time. The equivalent with sys.settrace is")
    print("  a Python predicate call per line -- 50x more expensive and only an")
    print("  approximation.")
    print()
    print("  Note the tracer ran with events=() : a MonitorTracer can be armed")
    print("  AFTER it starts. A FunctionTracer cannot, because sys.settrace has no")
    print("  per-code-object event table at all.")


def demo_unavailable() -> None:
    if not HAS_MONITORING:
        return
    print()
    print("=" * 78)
    print("5. GRACEFUL DEGRADATION FOR AN INTERPRETER BUG")
    print("=" * 78)
    print("""
    CPython 3.14.6 rejects enabling C_RETURN/C_RAISE unless an unrelated CALL
    callback is also registered:

        m, e = sys.monitoring, sys.monitoring.events
        m.use_tool_id(4, "p")
        m.register_callback(4, e.C_RETURN, cb)
        m.register_callback(4, e.C_RAISE,  cb)
        m.set_events(4, e.C_RETURN | e.C_RAISE)
        # ValueError: cannot set C_RETURN or C_RAISE events independently

    adding a *CALL* callback makes the same set_events succeed.
    """)
    from framecraft import C_CALL as _C_CALL

    t = MonitorTracer(events=(_C_CALL, C_RETURN), capture_return=True)
    try:
        with t:
            d = {1: 2}
            d.get(1)
            [1, 2, 3].append(4)
    finally:
        pass
    print("  requested events            c_call, c_return")
    print(f"  interpreter refused         {sorted(t.unavailable_events) or '(none)'}")
    print(f"  interpreter's complaint     {t._last_set_events_error or '(none)'}")
    print(f"  events actually captured    {len(t.events)}")
    print()
    print("  A tracer that treats this as fatal is unusable on the affected")
    print("  versions. framecraft drops the offending group, retries, and records")
    print("  exactly what it lost in `unavailable_events` -- so the report says so")
    print("  instead of quietly producing a thinner trace than you asked for.")
    print()
    print("  The 3.14+ replacement for C-call visibility is the INSTRUCTION event;")
    print("  see example 04 section 4.")


def compare_engines() -> None:
    print()
    print("=" * 78)
    print("6. settrace vs monitoring: cost and completeness")
    print("=" * 78)
    import time

    from framecraft import FunctionTracer

    def spin(n):
        total = 0
        for i in range(n):
            total += i
        return total

    n = 4000
    t0 = time.perf_counter()
    for _ in range(n):
        spin(50)
    base = time.perf_counter() - t0

    rows = []
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN))) as ft:
        t0 = time.perf_counter()
        for _ in range(n):
            spin(50)
        rows.append(("sys.settrace call/line/return", time.perf_counter() - t0, len(ft.events)))

    if HAS_MONITORING:
        with MonitorTracer(TracerConfig(events=(CALL, LINE, RETURN))) as mt:
            t0 = time.perf_counter()
            for _ in range(n):
                spin(50)
            rows.append(("sys.monitoring same events", time.perf_counter() - t0, len(mt.events)))

    with __import__("framecraft").SamplingTracer(interval=0.001) as st:
        t0 = time.perf_counter()
        for _ in range(n):
            spin(50)
        rows.append(("sampling (no instrumentation)", time.perf_counter() - t0, st.total_samples))

    print(f"  baseline                    {base:.4f}s")
    print()
    print(f"  {'engine':<34} {'seconds':>9} {'ratio':>8} {'events':>9}")
    for label, el, nevents in rows:
        print(f"  {label:<34} {el:>9.4f} {el / base:>7.1f}x {nevents:>9,}")
    print()
    print("  Two different kinds of number in that table:")
    print("   * the instrumenting tracers report EXACT event counts, and cost a")
    print("     large multiple of baseline. That cost is *non-uniform*: it is per")
    print("     line, so code with many short lines is taxed hardest. A line")
    print("     tracer is a distortion, not a measurement.")
    print("   * the sampler reports an APPROXIMATE count (one per timer tick) at")
    print("     near-baseline cost. It is the honest way to answer 'where does")
    print("     the time go' and the useless way to answer 'how many times is")
    print("     this function called'.")
    print()
    print("  Use both: sample to find the hot region, then instrument only that")
    print("  region to find out why.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engines", action="store_true", help="only the cost comparison")
    ns = ap.parse_args()
    if ns.engines:
        compare_engines()
        return 0
    show_capabilities()
    if not HAS_MONITORING:
        print("\nNext: 07 -- sampling, which needs no hook at all")
        return 0
    demo_events()
    demo_call_event()
    demo_per_code()
    demo_unavailable()
    compare_engines()
    print("=" * 78)
    print("Next: 07 -- sampling, which needs no hook at all")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
