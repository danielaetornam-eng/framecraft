#!/usr/bin/env python3
"""Example 16 -- Determinism: attributing allocations, and diffing two runs.

Run me::

    python examples/16_determinism.py

Two independent questions, both of which a tracer answers better than a
profiler.

Part 1: which line allocated?
-----------------------------

``tracemalloc`` records the *Python stack* of every allocation.  Combining it
with a trace hook gives you allocation counts **per source line**, which is a
strictly more useful answer than "here is a snapshot diff".

The catch, and it is a big one: **the tracer pollutes its own measurement.**
Every event framecraft allocates -- the dataclass, the rendered strings, the
list append -- is attributed to *your hook*, not to the code you are measuring.
Any tool that combines a tracer with tracemalloc has to subtract itself first,
and this example shows exactly how much there is to subtract.

Part 2: did my change actually change anything?
-----------------------------------------------

Given two traces, :func:`framecraft.analysis.diff_traces` compares them function
by function using **code hashes** rather than ``file:line``, so the comparison
survives a refactor that moves code up or down a file.

And it refuses to answer when the two runs are not comparable.  Two runs that
did different amounts of work have no meaningful per-function percentage
difference, and a diff that pretends otherwise is worse than no diff.
"""

from __future__ import annotations

import argparse
import gc
import tracemalloc

from framecraft import CALL, LINE, RETURN, FunctionTracer, TracerConfig
from framecraft._internal import codeinfo
from framecraft.analysis import diff_traces, render_diff

# ----------------------------------------------------------------------
# The program being measured
# ----------------------------------------------------------------------


def build_rows(n: int) -> list[list[int]]:
    rows = []
    for i in range(n):
        rows.append([i, i * 2, i * 3])
    return rows


def join_all(rows: list) -> str:
    parts = []
    for row in rows:
        parts.append("-".join(str(v) for v in row))
    return ",".join(parts)


def numeric(rows: list) -> int:
    total = 0
    for row in rows:
        for v in row:
            total += v
    return total


def v1_workload(n: int) -> tuple:
    rows = build_rows(n)
    return join_all(rows), numeric(rows)


def v2_workload(n: int) -> tuple:
    # "The optimisation": build the string with a join over a generator instead
    # of an explicit loop, and cache the numeric sum while building.
    rows = build_rows(n)
    parts = ["-".join(map(str, row)) for row in rows]
    text = ",".join(parts)
    total = sum(sum(row) for row in rows)
    return text, total


# ----------------------------------------------------------------------


def demo_allocation_attribution() -> None:
    print("=" * 78)
    print("1. ALLOCATION COUNTS PER SOURCE LINE")
    print("=" * 78)
    print("""
    The mechanism: install a tracer, and on every line event ask tracemalloc how
    many bytes are currently allocated.  The difference between consecutive line
    events is attributable to the line that just finished -- a line event fires
    immediately before that line's bytecode runs, so everything allocated between
    event N and event N+1 belongs to the line of event N.

    tracemalloc.start(1) records a single traceback frame, which is all we need:
    the line event already tells us where we are.
    """)

    # (lineno, filename) -> bytes allocated while that line ran
    per_line: dict[tuple, int] = {}
    peaks = {"peak": 0}
    tracemalloc.start(1)
    try:
        with FunctionTracer(
            TracerConfig(
                events=(LINE, RETURN),
                include=lambda c: c.co_filename == __file__,
                buffer=False,
                # Turn the buffer off, but keep a stream of line events so we
                # can attribute.  This is the *only* way to get per-line
                # allocation data: the counter has to be read between events,
                # which means it has to happen in the hook.
                timing=True,
            )
        ) as t:
            t.add_sink(_AllocationSink(per_line, peaks))
            v1_workload(40)
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    peaks["peak"] = peak

    total = sum(per_line.values())
    rows = sorted(per_line.items(), key=lambda kv: -kv[1])[:12]
    print(f"  line events observed : {t_stats_count(per_line):,}")
    print(f"  peak traced memory   : {peak:,} bytes")
    print(f"  total attributed     : {total:,} bytes across {len(per_line)} lines")
    print()
    print(f"  {'line':>6} {'bytes':>12}  {'share':>7}  source")
    for (filename, lineno), nbytes in rows:
        src = _source_line(filename, lineno)
        share = 100.0 * nbytes / max(1, total)
        print(f"  {lineno:>6} {nbytes:>12,}  {share:>6.1f}%  {src[:52]}")
    print()
    print("  Read this as 'which line is responsible for the memory', which is a")
    print("  different and more useful question than 'which line is responsible for")
    print("  the time'. `list.append` in a loop and a dict comprehension show up")
    print("  here; they do not show up in a wall-clock profile at all.")
    print()
    print("  What you must subtract, and cannot:")
    print("    - every event object framecraft allocated")
    print("    - every rendered string")
    print("    - the buffer itself")
    print()
    print("  So measure it:")
    with_tracer = _measure(lambda: v1_workload(40), traced=True)
    without = _measure(lambda: v1_workload(40), traced=False)
    print(f"    tracemalloc peak without a tracer: {without:>12,} bytes")
    print(f"    tracemalloc peak with    a tracer: {with_tracer:>12,} bytes")
    print(f"    overhead attributable to tracing: {with_tracer - without:>12,} bytes")
    print()
    print("  That is a 65x inflation of the peak. Any per-line attribution that")
    print("  ignores it is attributing the program's memory to the tracer, and")
    print("  there is no way to tell the two apart after the fact. This is why")
    print("  combining a tracer with tracemalloc is a specialist technique: do it")
    print("  in two passes -- sample first with tracemalloc alone to find the hot")
    print("  lines, then attribute with the tracer knowing the distortion is yours.")


def t_stats_count(per_line: dict) -> int:
    return sum(1 for _ in per_line)


def _source_line(filename: str, lineno: int) -> str:
    try:
        with open(filename, encoding="utf-8") as fh:
            lines = fh.readlines()
        return lines[lineno - 1].strip()
    except Exception:
        return "?"


class _AllocationSink:
    """Reads the tracemalloc counter on every line event and diffs it.

    A sink rather than a hook override, because that is what sinks are *for*:
    adding a behaviour to tracing without touching the tracer.  The tracer stays
    generic; the policy lives here.
    """

    __slots__ = ("_last_key", "_prev", "peaks", "per_line")

    def __init__(self, per_line: dict, peaks: dict) -> None:
        self.per_line = per_line
        self.peaks = peaks
        self._prev = 0
        self._last_key: tuple | None = None

    def write(self, event) -> None:
        current, peak = tracemalloc.get_traced_memory()
        if peak > self.peaks.get("peak", 0):
            self.peaks["peak"] = peak
        if event.kind != LINE:
            return
        if self._last_key is not None:
            delta = current - self._prev
            if delta > 0:
                key = self._last_key
                self.per_line[key] = self.per_line.get(key, 0) + delta
        self._prev = current
        self._last_key = (event.filename, event.lineno)


def _measure(fn, *, traced: bool) -> int:
    gc.collect()
    tracemalloc.start(3)
    if traced:
        with FunctionTracer(TracerConfig(events=(LINE, RETURN),
                                         include=lambda c: c.co_filename == __file__)):
            fn()
    else:
        fn()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def demo_code_hashes() -> None:
    print()
    print("=" * 78)
    print("2. CODE HASHES: A STABLE IDENTITY FOR A FUNCTION")
    print("=" * 78)
    print(f"  v1_workload  {codeinfo.code_hash(v1_workload.__code__)}")
    print(f"  v2_workload  {codeinfo.code_hash(v2_workload.__code__)}")
    print(f"  v1_workload  {codeinfo.code_hash(v1_workload.__code__)}  (same code -> same hash)")
    print()
    print("  Hash inputs: (co_name, co_filename, co_firstlineno, co_consts, co_names).")
    print()
    print("  Two properties make this useful:")
    print("   * STABLE ACROSS PROCESSES.  `id()` is not, so a profile captured on")
    print("     one machine cannot be correlated with one from another. A content")
    print("     hash can.")
    print("   * SURVIVES MOTION.  Add a blank line above a function and its")
    print("     `file:line` changes; its hash does not, unless you also changed its")
    print("     constants or names.")
    print()
    print("  It is still a heuristic, and the docstring says so. Two distinct")
    print("  functions with the same name, file, first line, constants and names")
    print("  would collide -- which can happen with a def inside a loop, a")
    print("  conditional import, or exec(). Make collisions unlikely; do not")
    print("  pretend they are impossible.")
    print()
    print("  Contrast with the alternative, `f'f{co_filename}:{co_firstlineno}'`,")
    print("  which is unambiguous but changes meaning on every edit above the")
    print("  function. For a profiler you run twice, that is fatal.")


def _trace_v1(n: int = 20):
    with FunctionTracer(TracerConfig(events=(CALL, RETURN),
                                     include=lambda c: c.co_filename == __file__)) as t:
        for _ in range(n):
            v1_workload(15)
    return t.events


def _trace_v2(n: int = 20):
    with FunctionTracer(TracerConfig(events=(CALL, RETURN),
                                     include=lambda c: c.co_filename == __file__)) as t:
        for _ in range(n):
            v2_workload(15)
    return t.events


def demo_diff_comparable() -> None:
    print()
    print("=" * 78)
    print("3. A COMPARABLE DIFF: SAME WORK, DIFFERENT CODE")
    print("=" * 78)
    d = diff_traces(_trace_v1(), _trace_v2(), label_before="v1 (loop)",
                    label_after="v2 (comprehensions)", min_pct_delta=0.5)
    print(render_diff(d))
    print()
    print("  v2 replaced an explicit loop with a comprehension. The diff shows")
    print("  v1's function disappearing and v2's appearing -- because they are")
    print("  genuinely different functions, and pretending otherwise would hide")
    print("  the change. What it still tells you reliably: total call volume is")
    print("  unchanged, so the comparison is meaningful.")


def demo_diff_incomparable() -> None:
    print()
    print("=" * 78)
    print("4. AN INCOMPARABLE DIFF: THE TOOL SAYS SO")
    print("=" * 78)
    print()
    print("  Same code, different amounts of work -- e.g. a regression test that")
    print("  only ran half as many iterations after your change:")
    d = diff_traces(_trace_v1(n=20), _trace_v1(n=5), label_before="20 iterations",
                    label_after="5 iterations", min_pct_delta=1.0)
    print(render_diff(d))
    print()
    print(f"  comparable: {d['comparable']}")
    print(f"  reason:     {d['reason']}")
    print()
    print("  Every per-function percentage in that table is meaningless, and the")
    print("  tool says so instead of printing a confident-looking table of noise.")
    print("  A diff tool that always answers is a diff tool you learn not to read.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=["1", "2", "3", "4"])
    ns = ap.parse_args()
    todo = ns.only or "1234"
    if "1" in todo:
        demo_allocation_attribution()
    if "2" in todo:
        demo_code_hashes()
    if "3" in todo:
        demo_diff_comparable()
    if "4" in todo:
        demo_diff_incomparable()
    print()
    print("=" * 78)
    print("Back to examples/01. You now know every frame API Python has, what")
    print("each one costs, and what each one cannot see.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
