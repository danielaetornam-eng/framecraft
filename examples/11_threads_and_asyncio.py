#!/usr/bin/env python3
"""Example 11 -- Threads, asyncio tasks, and the timeline view.

Run me::

    python examples/11_threads_and_asyncio.py
    python examples/11_threads_and_asyncio.py --html timeline.html

Three things go wrong with tracing concurrent code, and each has a specific fix
in this library.

Problem 1: the hook is per-thread
---------------------------------
``sys.settrace`` sets the hook for the **calling thread only**.  A worker pool
you did not call ``threading.settrace`` for is silently untraced, and the
symptom is a profile where the main thread does all the work and the workers
vanish.  ``TracerConfig(include_threads=True)`` covers threads started *after*
installation.  Existing threads need
:meth:`~framecraft.trace.FunctionTracer.attach_existing`, which works
cross-thread because ``frame.f_trace`` is a property of the frame, not of the
hook slot.

Problem 2: a flame graph cannot show concurrency
----------------------------------------------
A flame graph aggregates.  It cannot answer "what was thread B doing while
thread A was blocked?".  For that you want time on the x-axis and threads as
rows -- a Gantt chart.  That is
:class:`~framecraft.graph.Timeline`, and
:meth:`~framecraft.graph.Timeline.overlaps` computes the concurrency windows
with a sweep line.

Problem 3: a tracer cannot see blocking
--------------------------------------
This is the important one, and it is a *limit*, not a bug.  A tracer fires on
bytecode.  A thread blocked in ``lock.acquire()`` or ``socket.recv()``
executes no bytecode, so it produces no events.  The **gap** between events is
exactly the interesting part, and :attr:`~framecraft.graph.Span.uninstrumented_ns`
exists to name it rather than silently attributing it to whatever frame happens
to be innermost.

asyncio
-------
PEP 669 reports coroutine frames individually but cannot tell you which *task*
a coroutine belongs to.  A task factory can: it runs at task creation, which is
the only moment the creating frame still exists.
``TracerConfig(trace_asyncio_tasks=True)`` installs one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time

from framecraft import CALL, LINE, RETURN, FunctionTracer, TracerConfig
from framecraft.graph import build_timeline
from framecraft.sinks import JSONLSink

# ----------------------------------------------------------------------


def worker(n: int, out: list, lock: threading.Lock) -> None:
    """Real work, plus a real lock, plus a real sleep."""
    total = 0
    for i in range(n):
        total += i * i
    with lock:
        out.append(total)
    time.sleep(0.001)  # uninstrumentable: no bytecode runs during this


def producer(n: int, out: list) -> None:
    for i in range(n):
        out.append(("p", i))
        time.sleep(0.0005)


def consumer(n: int, src: list, out: list) -> None:
    got = 0
    for _ in range(n):
        if src:
            src.pop()
            got += 1
    out.append(got)


def threaded_workload() -> None:
    results: list = []
    lock = threading.Lock()
    threads = [threading.Thread(target=worker, args=(200 + i * 50, results, lock), name=f"w{i}")
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results


# ----------------------------------------------------------------------


def demo_thread_scope() -> None:
    print("=" * 78)
    print("1. THE HOOK IS PER-THREAD")
    print("=" * 78)
    print("  (a) sys.settrace on the main thread only:")
    out: list = []
    lock = threading.Lock()
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN), capture_locals=False)):
        t = threading.Thread(target=worker, args=(50, out, lock), name="w0")
        t.start()
        t.join()
    print("      -> 0 events from the worker: it was never hooked.")

    print()
    print("  (b) with include_threads=True (covers threads started afterwards):")
    out.clear()
    cfg = TracerConfig(events=(CALL, LINE, RETURN), include_threads=True)
    with FunctionTracer(cfg) as tr:
        t = threading.Thread(target=worker, args=(50, out, lock), name="w1")
        t.start()
        t.join()
    threads = {e.thread_name for e in tr.events}
    print(f"      -> {len(tr.events)} events across threads {sorted(threads)}")
    print()
    print("  `threading.settrace` is what `include_threads` uses. It only affects")
    print("  threads created *after* it runs, which is why it is not a solution")
    print("  for a thread pool that is already running.")
    print()
    print("  For those, use attach_existing() from the tracer's own thread:")


def demo_attach_existing() -> None:
    print()
    print("=" * 78)
    print("2. INSTRUMENTING A FRAME THAT IS ALREADY RUNNING -- AND THE TRAP IN IT")
    print("=" * 78)
    print("""
    sys.settrace only affects frames created *after* it is installed. To trace
    code that is already executing, you assign frame.f_trace directly.
    """)
    holder: dict = {}

    def slow_worker(attacker):
        started.set()
        # Grab our own frame's tracer, mid-execution.
        attacker()
        total = 0
        for _ in range(20):
            total += 1
        holder["done"] = total

    started = threading.Event()

    # --- (a) the naive cross-thread attempt ------------------------------
    with FunctionTracer(TracerConfig(events=(LINE,))) as tr:
        t = threading.Thread(target=slow_worker, args=(lambda: None,), name="naive")
        t.start()
        started.wait(2.0)
        started.set()
        time.sleep(0.005)          # let it get past the capture point
        frames = sys._current_frames().get(t.ident)
        n = tr.attach_existing(frames)
        t.join()
    naive_events = sum(1 for e in tr.events if e.thread_name == "naive")
    print("  (a) attach_existing() from the MAIN thread, no arming:")
    print(f"      frames instrumented: {n}   <- the call SUCCEEDED")
    print(f"      events from 'naive' thread: {naive_events}   <- and observed nothing")
    print()
    print()
    print("  Cross-thread, this reports ZERO events -- and no error at all. The")
    print("  frames were instrumented; the interpreter simply never consulted")
    print("  them, because the global hook lives in *thread state* and that")
    print("  thread never got one.")
    print()
    print("  This is the worst failure mode in the whole library: it looks")
    print("  exactly like a filtering bug. So measure it rather than guessing.")

    # --- (b) arming the thread from inside it ----------------------------
    with FunctionTracer(TracerConfig(events=(LINE,))) as tr2:
        def worker2():
            tr2.arm_thread()   # installs sys.settrace *here*
            tr2.attach_existing()
            total = 0
            for _ in range(20):
                total += 1
            holder["done"] = total

        t2 = threading.Thread(target=worker2, name="armed")
        t2.start()
        t2.join()
    n = sum(1 for e in tr2.events if e.thread_name == "armed")
    print("  (b) arm_thread() + attach_existing(), both from inside the thread:")
    print(f"      events from 'armed' thread: {n}")
    print()
    print("  Three rules, all measured in examples/11 and tests/test_tracers.py:")
    print("   1. assigning frame.f_trace needs a global hook in THAT thread")
    print("   2. include_threads=True (threading.settrace) covers threads started")
    print("      after the tracer; it cannot help threads that already existed")
    print("   3. for a thread that already existed, arm_thread() from inside it --")
    print("      which changes that thread's behaviour until it exits, so do it")
    print("      deliberately, not reflexively")
    print()
    print("  Use include_threads=True when you start the threads yourself, which is")
    print("  the case in demo_timeline() below. Use arm_thread() only when you")
    print("  are attaching to a thread you did not create.")


def demo_timeline() -> None:
    print()
    print("=" * 78)
    print("3. THE TIMELINE: WHY A FLAME GRAPH CANNOT SHOW CONCURRENCY")
    print("=" * 78)
    cfg = TracerConfig(events=(CALL, RETURN), include_threads=True)
    with FunctionTracer(cfg) as tr:
        threaded_workload()
    tl = build_timeline(tr.events)
    print(f"  {len(tl)} spans over {len(tl.threads())} threads, "
          f"wall span {tl.span_ns / 1e6:.2f} ms")
    print()
    base = min((s.start_ns for s in tl.spans), default=0)
    print(f"  {'thread':<10} {'depth':>5} {'start ms':>10} {'dur ms':>9} {'uninstr ms':>12}  function")
    for s in sorted(tl.spans, key=lambda s: (s.thread_id, s.start_ns))[:20]:
        print(f"  {s.thread_name:<10} {s.depth:>5} {(s.start_ns - base) / 1e6:>10.2f} "
              f"{s.duration_ns / 1e6:>9.3f} {s.uninstrumented_ns() / 1e6:>12.3f}  {s.label}")
    print()
    ov = tl.overlaps()
    total_ns = sum(e - s for s, e, _a, _b in ov)
    print(f"  concurrency windows: {len(ov)}")
    for start, end, a, b in ov[:6]:
        print(f"      {(start - base) / 1e6:>8.2f} - {(end - base) / 1e6:>8.2f} ms  "
              f"{tl.thread_name(a)} || {tl.thread_name(b)}")
    if total_ns:
        print(f"  wall time with >=2 threads inside a span: {total_ns / 1e6:.2f} ms "
              f"of {tl.span_ns / 1e6:.2f} ms ({100 * total_ns / tl.span_ns:.0f}%)")
    print()
    print("  `uninstr ms` is the important column. `time.sleep(0.001)` inside")
    print("  worker() is 1ms of wall time during which the thread executed NO")
    print("  bytecode, so the tracer recorded nothing at all. A profiler that")
    print("  silently spread that time over the enclosing frames would be lying;")
    print("  Span.uninstrumented_ns() names it instead.")


async def async_producer(n: int) -> list:
    out = []
    for i in range(n):
        out.append(i)
        await asyncio.sleep(0)
    return out


async def async_consumer(n: int) -> int:
    got = 0
    for _ in range(n):
        await asyncio.sleep(0)
        got += 1
    return got


async def async_main() -> None:
    await asyncio.gather(async_producer(4), async_consumer(4), async_producer(4))


def demo_asyncio() -> None:
    print()
    print("=" * 78)
    print("4. ASYNCIO: WHICH TASK IS THIS FRAME IN?")
    print("=" * 78)
    from framecraft._internal.compat import HAS_MONITORING

    async def run(with_task_names: bool) -> list:
        cfg = TracerConfig(events=(CALL, RETURN), trace_asyncio_tasks=with_task_names)
        tracer_cls = __import__("framecraft").MonitorTracer if HAS_MONITORING else FunctionTracer
        with tracer_cls(cfg) as t:
            await async_main()
        return t.events

    for label, enabled in (("without task tracking", False), ("with task tracking", True)):
        events = asyncio.run(run(enabled))
        tasks = {e.task for e in events if e.task}
        print(f"  {label}: {len(events)} events, tasks seen: "
              f"{sorted(t for t in tasks if t)[:6] or '(none)'}")
    print()
    print("  PEP 669 reports coroutine frames individually but has no idea which")
    print("  Task owns one. A task factory runs at *creation* time, which is the")
    print("  only moment the creating frame still exists -- so the mapping has to")
    print("  be recorded there, not recovered later.")
    print()
    print("  This is what `sys.set_coroutine_origin_tracking_depth(1)` exists for")
    print("  too: it records the origin frame on each coroutine so a stack dump of")
    print("  a suspended coroutine can include where it came from. It costs")
    print("  memory per coroutine, which is why it is off by default.")


def demo_streaming_large() -> None:
    print()
    print("=" * 78)
    print("5. TRACING CONCURRENCY REQUIRES STREAMING")
    print("=" * 78)
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="framecraft-11-"))
    path = tmp / "threads.jsonl"
    cfg = TracerConfig(events=(CALL, LINE, RETURN), include_threads=True, buffer=False)
    t = FunctionTracer(cfg)
    t.add_sink(JSONLSink(path=str(path)))
    with t:
        threaded_workload()
    print(f"  {path.stat().st_size:,} bytes for {len(threaded_workload.__doc__ or '') and 4} threads")
    print()
    print("  Four threads, a line trace, no buffer. Buffering this would hold every")
    print("  event from every thread in memory at once, and the events from four")
    print("  threads interleave into a single buffer with no way to tell which is")
    print("  which except by reading thread_id on each one.")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", help="also write an HTML report here")
    ns = ap.parse_args()
    demo_thread_scope()
    demo_attach_existing()
    demo_timeline()
    demo_asyncio()
    demo_streaming_large()
    if ns.html:
        cfg = TracerConfig(events=(CALL, RETURN), include_threads=True)
        with FunctionTracer(cfg) as t:
            threaded_workload()
        from framecraft.sinks import HTMLReport

        HTMLReport(t.events, title="threads and asyncio").write(ns.html)
        print(f"\nwrote {ns.html}")
    print("=" * 78)
    print("Next: 12 -- flame graphs and call-graph rendering")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
