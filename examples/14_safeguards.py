#!/usr/bin/env python3
"""Example 14 -- Guards: budgets, backpressure, and never killing the program.

Run me::

    python examples/14_safeguards.py

A tracer runs inside the program it measures.  That is a privileged position and
it comes with obligations, most of which are easy to state and easy to get wrong.

The five rules this library follows
----------------------------------

1.  **A hook never raises.**  An exception escaping a trace callback propagates
    into the traced program *and* unsets the global hook (measured in
    ``examples/02``).  One typo therefore costs you a crash plus a silently dead
    tracer.  Every hook body here is wrapped by :meth:`Tracer._guard`, failures
    are counted in :attr:`Tracer.error_count`, and ``FRAMECRAFT_DEBUG=1`` makes
    them visible instead.

2.  **A tracer never retains frames.**  A frame reference keeps its entire local
    namespace alive.  Storing frames in an event is a memory leak with a
    debugger's licence.  Events hold copies
    (:class:`~framecraft.core.events.StackFrame`), never frames.

3.  **Memory is bounded.**  ``max_events`` drops oldest in chunks (not
    ``list.pop(0)``, which is O(n) and would dominate), and the count of dropped
    events is reported so the trace can admit it is incomplete.

4.  **Backpressure is the sink's problem, and the sink must be non-fatal.**  A
    sink that raises loses its event; :class:`~framecraft.sinks.TeeSink` keeps
    going with the other sinks and re-raises at the end so the failure is not
    swallowed.

5.  **The tracer excludes itself.**  Otherwise the teardown frames
    (``__exit__`` -> ``stop`` -> ``_stop`` -> ``_detach_all``) are the innermost
    frames at the moment the hook is removed, and they dominate the tail of every
    trace.

Two more that are policy, not mechanism
----------------------------------------

6.  **The previous hook is restored exactly, including ``None``.**  Leaving a
    hook installed after the ``with`` block is the classic "my program got 30x
    slower after I imported my profiler" bug.

7.  **Two tracers cannot silently share one global slot.**  ``sys.settrace`` is
    one slot per thread; the second tracer wins and the first keeps its
    per-frame ``f_trace`` values, so you get a *mixture*.  :class:`HookLease`
    makes that a loud failure at install time.
"""

from __future__ import annotations

import gc
import sys
import weakref

from framecraft import CALL, LINE, RETURN, FunctionTracer, HookLease, TracerConfig
from framecraft.sinks import CountingSink, NullSink, TeeSink

# ----------------------------------------------------------------------


def spin(n: int) -> int:
    total = 0
    for i in range(n):
        total += i
    return total


class Leaky:
    """An object that would keep a huge graph alive if a frame retained it."""

    def __init__(self) -> None:
        self.payload = bytearray(2_000_000)


# ----------------------------------------------------------------------


def demo_hook_never_raises() -> None:
    print("=" * 78)
    print("1. A HOOK BUG MUST NOT KILL THE PROGRAM")
    print("=" * 78)
    state = {"raised": 0}

    class ExplodingSink:
        def write(self, event) -> None:
            state["raised"] += 1
            if state["raised"] == 3:
                raise RuntimeError("sink bug")

    counting = CountingSink()
    combined = TeeSink(counting, ExplodingSink(), NullSink())
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN), buffer=False)) as t:
        t.add_sink(combined)
        result = spin(50)
    print(f"  traced workload returned {result} (the program survived)")
    print(f"  sink raised {state['raised']} time(s)")
    print(f"  tracer.error_count = {t.error_count}")
    print(f"  the healthy sink still received {counting.total:,} events")
    print(f"  sys.gettrace() after the with-block: {sys.gettrace()!r}")
    print()
    print("  TeeSink's policy: a failing child does not stop the others, and the")
    print("  first exception is re-raised so it surfaces in error_count instead of")
    print("  disappearing. `counting.total` being larger than the number of")
    print("  successful writes by `ExplodingSink` is exactly the point.")
    print()
    print("  Set FRAMECRAFT_DEBUG=1 to see the traceback of every swallowed hook")
    print("  error. Off by default, because a production tracer that prints")
    print("  tracebacks from inside a hook is its own outage.")


def demo_no_frame_retention() -> None:
    print()
    print("=" * 78)
    print("2. THE TRACER MUST NOT RETAIN FRAMES")
    print("=" * 78)

    def make_leaky():
        obj = Leaky()
        return spin(10), obj

    gc.collect()
    before = len(gc.get_objects())
    with FunctionTracer(TracerConfig(events=(LINE,), capture_locals=True,
                                     include=lambda c: c.co_name == "make_leaky")) as t:
        make_leaky()
    gc.collect()
    after = len(gc.get_objects())
    print(f"  traced objects before {before:,}, after {after:,} (delta {after - before:+,})")
    print(f"  events kept: {len(t.events)}")
    big = sum(len(b.value) for ev in t.events for b in ev.locals)
    print(f"  captured local text: {big:,} characters")
    print()
    print("  The 2 MB `payload` bytearray is gone: the event holds a *string*")
    print("  rendering of `obj`, not the object. Holding a frame would keep")
    print("  `obj` -- and everything `obj` references -- alive for as long as the")
    print("  trace existed.")

    # Prove it directly with a weakref to a large object that the traced frame
    # held. (A frame itself cannot be weak-referenced -- `TypeError: cannot
    # create weak reference to 'frame' object` -- so we watch the *contents*.)
    refs: list = []

    def traced_target():
        big = Leaky()  # 2 MB
        refs.append(weakref.ref(big))
        return spin(10)

    with FunctionTracer(TracerConfig(events=(LINE,), capture_locals=True,
                                     include=lambda c: c.co_name == "traced_target")) as t2:
        traced_target()
    gc.collect()
    print(f"  weakref to a 2 MB object held by a traced frame: "
          f"{'still alive' if refs[0]() is not None else 'collected'}")
    print("  the trace still shows it, as text:")
    for ev in t2.events:
        for b in ev.locals:
            if b.name == "big":
                print(f"      {b.name} = {b.value}")
    print()
    print("  So the trace is reviewable after the fact while the memory is")
    print("  released immediately. That is the whole design: capture a rendering,")
    print("  never a reference.")


def demo_bounded_memory() -> None:
    print()
    print("=" * 78)
    print("3. BOUNDED MEMORY, AND ADMITTING DROPS")
    print("=" * 78)
    for max_events in (100, 1000, 5000):
        with FunctionTracer(TracerConfig(events=(LINE,), max_events=max_events,
                                         include=lambda c: c.co_name == "spin")) as t:
            spin(2000)
        s = t.stats()
        print(f"  max_events={max_events:<6} kept={len(t.events):<6} dropped={t.dropped:<6} "
              f"coverage={s['coverage_ratio'] * 100:.3f}%")
    print()
    print("  `coverage_ratio` is kept/(kept+dropped), and it appears in every")
    print("  report. A trace that silently lost 99% of its events and said")
    print("  nothing is worse than no trace, because it looks authoritative.")
    print()
    print("  Note the drop is chunked: the oldest 10% go at once. `list.pop(0)`")
    print("  would be O(n) per event and would turn the overflow path into the")
    print("  dominant cost of the whole tracer.")


def demo_restore_hooks() -> None:
    print()
    print("=" * 78)
    print("4. THE PREVIOUS HOOK IS RESTORED EXACTLY")
    print("=" * 78)
    calls = []

    def pre_existing(frame, event, arg):
        calls.append(event)
        return pre_existing

    sys.settrace(pre_existing)
    try:
        before = sys.gettrace()
        with FunctionTracer(TracerConfig(events=(CALL,))):
            spin(5)
        after = sys.gettrace()
        print(f"  a foreign hook was installed:  {before is pre_existing}")
        print(f"  after framecraft's with-block:  {after is pre_existing}")
        print(f"  restored correctly: {after is pre_existing}")
        print(f"  events the foreign hook saw: {len(calls)}")
    finally:
        sys.settrace(None)
    print(f"  and None restores to None: {sys.gettrace() is None}")
    print()
    print("  Both halves matter. Restoring the wrong hook breaks someone else's")
    print("  debugger; restoring None when something was there before breaks the")
    print("  same thing in the other direction. And detaching every `f_trace` we")
    print("  set *before* clearing the global hook is what stops already-running")
    print("  frames calling into a stopped tracer.")


def demo_hook_lease() -> None:
    print()
    print("=" * 78)
    print("5. TWO TRACERS CANNOT SILENTLY SHARE ONE GLOBAL SLOT")
    print("=" * 78)
    a = FunctionTracer(TracerConfig(events=(CALL,)))
    b = FunctionTracer(TracerConfig(events=(CALL,)))
    with a:
        try:
            with b:
                pass
        except RuntimeError as exc:
            print(f"  second tracer refused to start: {exc}")
        print(f"  first tracer still works: {a.active}")
        print(f"  HookLease.current(): {HookLease.current()!r}")
    print(f"  after both exit: {HookLease.current()!r}")
    print()
    print("  What would happen without the lease: `b` would install its own")
    print("  global hook, `a` would keep the `f_trace` values it already set on")
    print("  running frames, and every new frame would be traced by `b`. You")
    print("  would get a trace that is half yours and half someone else's -- and")
    print("  it looks completely plausible.")
    print()
    print("  `compose()` is the supported way to run several tracers: it enters")
    print("  them in order, exits in reverse, and rejects unconstructible")
    print("  combinations up front.")


def demo_streaming_vs_buffer() -> None:
    print()
    print("=" * 78)
    print("6. STREAMING IS THE ONLY SCALABLE MODE")
    print("=" * 78)
    import shutil
    import tempfile
    import time
    from pathlib import Path

    from framecraft.sinks import JSONLSink

    tmp = Path(tempfile.mkdtemp(prefix="framecraft-14-"))

    rows = []
    for buffer in (True, False):
        path = tmp / ("buf.jsonl" if buffer else "stream.jsonl")
        cfg = TracerConfig(
            events=(LINE,),
            include=lambda c: c.co_name == "spin",
            buffer=buffer,
            max_events=10_000_000,
        )
        sink = JSONLSink(path=str(path), flush_every=4096)
        t = FunctionTracer(cfg)
        t.add_sink(sink)
        t0 = time.perf_counter()
        with t:
            for _ in range(200):
                spin(300)
        el = time.perf_counter() - t0
        sink.close()
        rows.append((buffer, len(t.events), el, path.stat().st_size, t.error_count))

    print(f"  {'buffer':<8} {'events':>10} {'retained':>10} {'seconds':>9} {'file bytes':>12} {'errors':>7}")
    for buffer, kept, el, size, errs in rows:
        print(f"  {buffer!s:<8} {kept:>10,} {kept:>10,} {el:>9.3f} {size:>12,} {errs:>7}")
    print()
    b, s = rows[0], rows[1]
    print(f"  Identical events on disk ({b[3]:,} vs {s[3]:,} bytes); the only")
    print(f"  difference is that the buffering run also held {b[1]:,} event")
    print(f"  objects in memory. At ~200 bytes each that is about "
          f"{b[1] * 200 / 1e6:.0f} MB per run of this length.")
    print()
    print("  Extrapolate: a service doing a million line events per minute holds")
    print("  a quarter of a million dataclasses per minute of uptime. That is not")
    print("  a cache, it is a leak with a debugger's licence.")
    print()
    print("  `buffer=True` exists for the post-hoc renderers -- the HTML report and")
    print("  the call graph both need the whole run before they can draw anything.")
    print("  For everything else, stream.")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    demo_hook_never_raises()
    demo_no_frame_retention()
    demo_bounded_memory()
    demo_restore_hooks()
    demo_hook_lease()
    demo_streaming_vs_buffer()
    print("=" * 78)
    print("Next: 15 -- build a real execution recorder end to end")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
