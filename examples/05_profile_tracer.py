#!/usr/bin/env python3
"""Example 05 -- ``sys.setprofile``: the only way to see into C.

Run me::

    python examples/05_profile_tracer.py

The gap in examples 03 and 04
-----------------------------

A line tracer is blind to everything that is not Python bytecode.  And most of
the time a Python program spends *not* executing Python bytecode: every
``d[k]``, every ``list.append``, every ``open``, every ``socket.recv``, every
``int()`` is a C function call.  ``sys.settrace`` will never tell you about any of
them.

``sys.setprofile`` is a different hook with a different signature::

    profile(frame, event, arg)

============  =========  ===================================================
event         arg        meaning
============  =========  ===================================================
``call``      None       a Python frame was created
``return``    retval     a Python frame returned
``c_call``    callable   about to call a C function
``c_return``  retval     the C function returned
``c_exception`` exc      the C function raised
============  =========  ===================================================

One function, no two-level structure, and it sees C.  What it cannot do is line
numbers or Python-level exceptions -- which is why ``cProfile``, which is
literally this hook plus statistics, produces flat profiles.

The attribution problem, and the honest fix
-------------------------------------------

``c_call`` gives you the C callable but **not** the frame that called it.  So the
hook has to maintain its own notion of "which Python frame am I inside", which is
what :class:`framecraft.trace.ProfileTracer` does with an explicit depth stack.

When a ``c_return`` arrives with no open Python frame (a C call made from
interpreter internals, e.g. during list construction), it is dropped and
counted.  Attributing it to "the nearest frame anyway" would produce numbers
that look precise and are not.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter

from framecraft import C_CALL, C_RETURN, CALL, RETURN, ProfileTracer, TracerConfig

# ----------------------------------------------------------------------
# A workload that spends most of its time in C
# ----------------------------------------------------------------------


def numeric_work(n: int) -> float:
    """Arithmetic and dict/list operations: nearly all C."""
    data = {}
    for i in range(n):
        data[i] = i * i
    values = list(data.values())
    total = sum(values)
    mean = total / len(values)
    return mean


def string_work(n: int) -> str:
    """String methods: all C, and not cheap."""
    parts = []
    for i in range(n):
        parts.append(f"{i:06d}")
    joined = ",".join(parts)
    return joined.upper().replace(",", "|").strip()


def mixed_work(n: int) -> tuple:
    a = numeric_work(n)
    b = string_work(n // 2)
    return a, len(b)


# ----------------------------------------------------------------------


def demo_python_only() -> None:
    print("=" * 78)
    print("1. THE HOOK ALONE: Python frames only")
    print("=" * 78)
    cfg = TracerConfig(events=(CALL, RETURN), include=lambda c: c.co_filename == __file__)
    with ProfileTracer(cfg) as t:
        mixed_work(200)
    print(f"  {len(t.events)} events")
    for ev in t.events[:8]:
        print(f"    {ev.summary()}  duration={ev.extra.get('duration_ns', 0) / 1000:.1f} us")
    print()
    print("  Every `return` carries extra['duration_ns']: wall time from that")
    print("  frame's 'call' to its 'return'. That is all a flat profiler needs.")
    print()
    print("  Notice what is missing: numeric_work's body is full of C calls and")
    print("  this trace cannot see a single one of them.")


def c_call_heavy(n: int) -> dict:
    """Deliberately full of operations that *stay* C calls on 3.13/3.14.

    Counter-intuitive but important: the specialising interpreter introduced in
    3.11 turns many operations that used to be C calls into dedicated
    opcodes.  ``d[k]`` is ``BINARY_SUBSCR`` on a dict, not a ``dict.__getitem__``
    C call; ``sum()`` is still a C call.  So a C-call profile *changes shape*
    between 3.10 and 3.14 for the same source, and method calls
    (``.append``, ``.items``, ``.replace``) are the durable ones.
    """
    data = {}
    acc = []
    for i in range(n):
        data[f"k{i}"] = i
        acc.append(str(i))
    keys = list(data.keys())
    joined = ",".join(acc).replace(",", ";").upper()
    return {"n": len(keys), "len": len(joined)}


def demo_with_c() -> None:
    print()
    print("=" * 78)
    print("2. trace_c=True: now the C calls appear")
    print("=" * 78)
    cfg = TracerConfig(
        events=(C_CALL, C_RETURN),
        include=lambda c: c.co_filename == __file__,
    )
    with ProfileTracer(cfg, trace_c=True) as t:
        c_call_heavy(200)
    counts: Counter = Counter()
    totals: Counter = Counter()
    pending = []
    for ev in t.events:
        if ev.kind == C_CALL:
            counts[ev.c_name] += 1
            pending.append(ev.c_name)
        elif ev.kind == C_RETURN and pending:
            totals[pending.pop()] += ev.extra.get("duration_ns", 0)
    print(f"  {len(t.events)} C events for c_call_heavy(200)")
    print()
    print(f"  {'C callable':<32} {'calls':>8}  {'total us':>10}")
    for name, n in counts.most_common(16):
        print(f"  {name:<32} {n:>8}  {totals[name] / 1000:>10.1f}")
    print()
    print("  Three things to read here:")
    print("   * VOLUME. 200 iterations produced more C calls than the entire Python")
    print("     call count of a realistic program. That is why trace_c is opt-in,")
    print("     and why profile hooks must sample rather than record.")
    print("   * ARGUMENTS. `arg` for c_call IS the callable, so you get its name,")
    print("     but you do NOT get its arguments -- sys.setprofile cannot show")
    print("     them at all. PEP 669 can; see example 12.")
    print("   * SHAPE CHANGES WITH THE INTERPRETER. `d[k]` used to be a C call to")
    print("     dict.__getitem__; on 3.11+ it is a specialised BINARY_SUBSCR and")
    print("     emits no c_call event. The same source yields a different C-call")
    print("     histogram on 3.10 and on 3.14. Treat cross-version C profiles as")
    print("     incomparable.")
    print()
    print("  Sanity check that the specialised case really is invisible:")
    with ProfileTracer(TracerConfig(events=(C_CALL,)), trace_c=True) as t2:
        d = {1: 2}
        d[1] = 3
        list(d.values())
    c_names = [e.c_name for e in t2.events]
    print(f"    dict subscript + values(): c_call events seen = {c_names}")
    print("    (no 'dict.__getitem__' -- it is a specialised opcode now)")


def demo_cost() -> None:
    print()
    print("=" * 78)
    print("3. OVERHEAD: setprofile vs settrace, measured")
    print("=" * 78)
    n = 20_000
    t0 = time.perf_counter()
    for _ in range(n):
        numeric_work(20)
    base = time.perf_counter() - t0

    rows = [("baseline (no hook)", base, 1.0)]
    for label, make in (
        ("setprofile call/return", lambda: ProfileTracer(TracerConfig(events=(CALL, RETURN)))),
        ("setprofile + C events", lambda: ProfileTracer(TracerConfig(events=(C_CALL, C_RETURN)), trace_c=True)),
        ("settrace call/return", lambda: __import__("framecraft").FunctionTracer(TracerConfig(events=(CALL, RETURN)))),
        ("settrace call/line/return", lambda: __import__("framecraft").FunctionTracer(TracerConfig(events=(CALL, "line", RETURN)))),
    ):
        tracer = make()
        t0 = time.perf_counter()
        with tracer:
            for _ in range(n):
                numeric_work(20)
        el = time.perf_counter() - t0
        rows.append((label, el, el / base))

    print(f"  {'configuration':<30} {'seconds':>9} {'vs baseline':>13}")
    print(f"  {'-' * 30} {'-' * 9} {'-' * 13}")
    for label, el, ratio in rows:
        print(f"  {label:<30} {el:>9.4f} {ratio:>12.1f}x")
    print()
    print("  `setprofile` has no per-line callback, so its cost tracks call count.")
    print("  `settrace` with line events adds a Python call per *line*, which for")
    print("  tight loops dominates everything. This is the single most important")
    print("  number in performance work: a line tracer is a distortion, not a")
    print("  measurement. See example 07 for the honest alternative.")


def demo_cprofile_parity() -> None:
    print()
    print("=" * 78)
    print("4. RELATION TO cProfile")
    print("=" * 78)
    import cProfile
    import io
    import pstats

    buf = io.StringIO()
    prof = cProfile.Profile()
    prof.enable()
    mixed_work(400)
    prof.disable()
    stats = pstats.Stats(prof, stream=buf).sort_stats("tottime")
    stats.print_stats(6)
    print(buf.getvalue())
    print("  cProfile IS sys.setprofile plus bookkeeping. The consequences are")
    print("  exactly the ones described above:")
    print("   * no line numbers, so you cannot tell which line of numeric_work is")
    print("     hot -- only which function")
    print("   * no Python exception events")
    print("   * it owns the single global setprofile slot: if anything else in the")
    print("     process installed a profile hook (a coverage plugin, ddtrace) your")
    print("     ProfileTracer replaced it, silently.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", action="store_true", help="only the overhead comparison")
    ns = ap.parse_args()
    if ns.bench:
        demo_cost()
        return 0
    demo_python_only()
    demo_with_c()
    demo_cost()
    demo_cprofile_parity()
    print("=" * 78)
    print("Next: 06 -- PEP 669 (sys.monitoring), the hook that was designed")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
