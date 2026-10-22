#!/usr/bin/env python3
"""Example 10 -- Generators, coroutines, and frames that are not on any stack.

Run me::

    python examples/10_generators_and_coroutines.py

The odd one out
---------------

Every other example has been about frames on a stack.  A generator frame, a
coroutine frame and an async-generator frame are real frames with real locals --
and they are on *no* stack at all while suspended.  They are reachable only
through the generator/coroutine object that owns them.

Consequences that matter for tracers:

*   A stack walk cannot see them.  ``sys._current_frames()`` will never list a
    suspended coroutine, so a sampling profiler will never attribute time to one.
*   Their ``f_lasti`` is ``-1`` when suspended (they are not "about to execute"
    an instruction) and a real offset when running.
*   Capturing their locals is legal at any time, which is the cheapest way to
    see a hung coroutine's state.
*   PEP 669's ``PY_YIELD`` fires when a generator suspends; ``PY_START``/
        ``PY_RETURN`` fire on each *resume* and *completion*, not once per
        generator.  So a generator consumed ten times produces twenty
        ``PY_START`` events, not one.  A tracer that assumes one call == one
        frame will over-count generators.

Finding every suspended frame
-----------------------------

:func:`framecraft.suspended_frames` walks ``gc.get_objects()`` looking for frames
whose code has ``CO_GENERATOR`` / ``CO_COROUTINE`` / ``CO_ASYNC_GENERATOR``
set.  This is the only way to find a coroutine nobody holds a reference to, and
it is how you debug "this task is awaiting a lock nobody will ever release".
It is O(heap) and perturbs reference counts, so it belongs in a debugging tool.
"""

from __future__ import annotations

import argparse

from framecraft import CALL, LINE, RETURN, YIELD, FunctionTracer, TracerConfig
from framecraft._internal.compat import HAS_MONITORING
from framecraft.core.frames import suspended_frames

# ----------------------------------------------------------------------


def counter(limit: int):
    """A plain generator. Each `next()` resumes and suspends the frame."""
    n = 0
    while n < limit:
        received = yield n
        n += 1 if received is None else received


def chunked(items: list, size: int = 2):
    """Nested generators: the outer frame holds the inner one in a local."""
    for i in range(0, len(items), size):
        yield from items[i : i + size]


def gen_with_finally():
    """GeneratorExit handling: code that runs when a generator is abandoned."""
    try:
        yield 1
        yield 2
    finally:
        pass


async def fetch(n: int):
    """A coroutine. Suspends at `await`, with locals intact."""
    result = n * 2
    await sleep_once()
    return result


class _YieldOnce:
    """The smallest possible awaitable: one suspension, then done."""

    def __await__(self):
        yield None
        return None


async def sleep_once():
    """A tiny coroutine so this example needs no event loop."""
    await _YieldOnce()
    return None


async def outer(n: int):
    """Coroutine calling a coroutine: two suspended frames, two objects."""
    a = await fetch(n)
    b = await fetch(n + 1)
    return a + b


# ----------------------------------------------------------------------


def demo_generator_lifecycle() -> None:
    print("=" * 78)
    print("1. A GENERATOR PRODUCES MANY FRAME ENTRIES")
    print("=" * 78)
    cfg = TracerConfig(events=(CALL, LINE, RETURN), include=lambda c: c.co_name == "counter")
    with FunctionTracer(cfg) as t:
        g = counter(3)
        values = [next(g), next(g), next(g)]
        try:
            next(g)
        except StopIteration:
            pass
    print(f"  next() produced {values} (plus one StopIteration)")
    print(f"  events: {len(t.events)}")
    kinds = {}
    for ev in t.events:
        kinds[ev.kind] = kinds.get(ev.kind, 0) + 1
    print(f"  by kind: {kinds}")
    lines = [ev.lineno for ev in t.events if ev.kind == LINE]
    print(f"  line events, in order: {lines}")
    print()
    print("  One Python-level `counter(3)` call. The frame is created once, but")
    print("  'call' fires every time it is *resumed*. So:")
    print("   * `call` count != number of `counter()` expressions in your code")
    print("   * a generator consumed N times produces ~N 'call' events")
    print("   * a naive 'function call counter' built on `call` events over-counts")
    print("     generators by exactly the consumption count")
    print()
    print("  This is the single most common way a Python profiler's call counts")
    print("  look wrong. If you need 'how many times was this *called*', count")
    print("  'call' events at depth transitions, or use PEP 669 and subtract the")
    print("  resumes identified by PY_YIELD.")


def demo_suspended_state() -> None:
    print()
    print("=" * 78)
    print("2. READING THE LOCALS OF A SUSPENDED FRAME")
    print("=" * 78)
    g = counter(10)
    # Before the first resume the frame has run only the generator prologue, so
    # f_lasti points at the RESUME instruction (offset 2), not at -1. -1 means
    # "no current instruction", which is what you see on a frame that is
    # suspended *before* it ever started.
    print(f"  before first next(): f_lasti = {g.gi_frame.f_lasti}  (prologue only)")
    next(g)
    next(g)
    fr = g.gi_frame
    print(f"  after two next():    f_lasti = {fr.f_lasti}   f_lineno = {fr.f_lineno}")
    print(f"  f_locals = {dict(fr.f_locals)}")
    print()
    print("  Nothing is missing and nothing is corrupt. The frame is simply not")
    print("  on any stack. Reading it is legal, cheap, and the only way to see")
    print("  where a stuck generator got to.")
    print()
    print("  Where the frame IS reachable, in order of directness:")
    print("    1. the generator object:  gen.gi_frame")
    print("    2. the generator's locals via the frame")
    print("    3. gc, if you lost the generator:  suspended_frames()")
    print()
    print("  `gi_frame` is the supported accessor. `ag_frame` for async generators,")
    print("  `cr_frame` for coroutines. All three are on the frame *object* types,")
    print("  not free functions.")
    g.close()


def demo_nested_generators() -> None:
    print()
    print("=" * 78)
    print("3. NESTED GENERATORS: frames holding frames")
    print("=" * 78)
    g = chunked([1, 2, 3, 4, 5], 2)
    next(g)  # outer is now suspended holding `items`, `i`, `size`, and the inner gen
    frame = g.gi_frame
    print(f"  outer frame locals: {sorted(frame.f_locals)}")
    inner = frame.f_locals.get("items")
    print(f"  the inner generator object is reachable as {type(inner).__name__}")
    print()
    print("  `yield from` delegates to a sub-generator. The inner generator's")
    print("  frame is live while the outer is suspended, and the outer frame's")
    print("  locals hold the reference that keeps it alive. This is why deleting")
    print("  a generator mid-iteration can collect its whole delegation chain:")
    print("  the inner frame is only reachable through the outer frame.")
    print()
    frames = suspended_frames()
    print(f"  suspended_frames() currently finds {len(frames)} frame(s) in the heap")
    for f in frames[:6]:
        code = f.f_code
        print(f"      {code.co_qualname:<34} {code.co_filename.split(chr(92))[-1]}:{f.f_lineno}")
    print()
    print("  Note which frames appear: NOT the ones currently executing (those")
    print("  are on a stack), and NOT frames belonging to functions that have")
    print("  returned. Only genuinely parked frames.")
    g.close()


def demo_coroutines() -> None:
    print()
    print("=" * 78)
    print("4. COROUTINES: TWO SUSPENDED FRAMES, TWO OBJECTS")
    print("=" * 78)
    coro = outer(5)
    print(f"  outer(5) returned {type(coro).__name__}, not a result.")
    print(f"  coro.cr_frame exists: {coro.cr_frame is not None}, f_lasti = {coro.cr_frame.f_lasti}")
    print()
    print("  Driving it by hand to reach the first await:")
    try:
        coro.send(None)
    except StopIteration:
        pass
    inner = coro.cr_frame
    print(f"  after first resume: outer frame is {inner.f_code.co_qualname}, "
          f"f_lasti={inner.f_lasti}")
    print(f"  its locals: { {k: v for k, v in inner.f_locals.items() if k != 'self'} }")
    print()
    print("  `await fetch(n)` created a *second* coroutine object with its own")
    print("  frame. That one completed, so it is gone; `outer`'s frame is parked")
    print("  at the await. Neither was ever on a stack for long enough for a")
    print("  sampling profiler to see it -- which is why coroutine bugs need a")
    print("  hook-based tracer, not a profiler.")
    print()
    print("  This is the concrete reason `sys.set_coroutine_origin_tracking_depth`")
    print("  exists: with it enabled, the frame that *created* a coroutine is")
    print("  recorded on it, so a stack dump of a suspended coroutine can include")
    print("  its origin. It costs a little memory per coroutine and is off by")
    print("  default for that reason.")


def demo_gc_sweep() -> None:
    print()
    print("=" * 78)
    print("5. WHY gc.get_objects() IS NEEDED AT ALL (AND WHY MY FIRST ATTEMPT FAILED)")
    print("=" * 78)
    print("""
    The obvious demo -- park a coroutine, drop the reference, find it with the
    GC -- does not work, and it is worth being explicit about why:

        parked = []
        def park(n):
            coro = outer(n); coro.send(None); parked.append(coro)
        park(1)
        del parked                       # <- the coroutine is FREED here
        suspended_frames()               # <- finds nothing, and correctly so
    """)
    parked: list = []

    def park(n: int) -> None:
        coro = outer(n)
        coro.send(None)      # run it to the first await, then park it
        parked.append(coro)

    park(1)
    print(f"  before del: {len(suspended_frames())} suspended frame(s) in this module")
    parked.clear()
    after = len([f for f in suspended_frames() if f.f_code.co_filename == __file__])
    print(f"  after  del: {after} suspended frame(s) in this module")
    print()
    print("  CPython is reference counted, not just garbage collected. Dropping")
    print("  the last reference frees the object immediately; the cyclic GC never")
    print("  sees it. `del` here is a free(), not a leak.")
    print()
    print("  So the tool is for the case that actually exists: an object kept")
    print("  alive by a REFERENCE CYCLE, or held by a container whose handle you")
    print("  have lost. Neither is findable by walking names, and only the second")
    print("  is findable at all without a cycle.")

    # --- the case that actually matters: a cycle ---------------------------
    cycle: dict = {}
    coro = outer(1)
    coro.send(None)
    cycle["coro"] = coro
    cycle["self"] = cycle          # <- the cycle keeps it alive
    del coro
    print()
    print("  Now with a genuine reference cycle (a dict that contains itself):")
    found = [f for f in suspended_frames() if f.f_code.co_filename == __file__]
    print(f"  suspended_frames() finds {len(found)} frame(s):")
    for f in found:
        locals_ = {k: str(v)[:26] for k, v in f.f_locals.items() if k != "self"}
        print(f"      {f.f_code.co_qualname:<14} line {f.f_lineno:<4} locals={locals_}")
    print()
    print("  That is the shape of every 'the program hangs and no traceback")
    print("  explains it' bug involving tasks: a coroutine parked at an `await`")
    print("  nobody will ever satisfy, held alive by something you cannot name.")
    print()
    print("  What this is for: `asyncio.all_tasks()` lists Tasks, not bare")
    print("  coroutines. A coroutine that was awaited directly, or created by a")
    print("  library that manages its own scheduling, is invisible to every other")
    print("  API in the standard library. This is the only one that finds it.")
    print()
    print("  Caveats, stated plainly: gc.get_objects() is O(heap) and perturbs")
    print("  reference counts. It belongs in a debugging tool, never on a service")
    print("  handling requests.")
    print()
    before = len(suspended_frames())
    cycle.clear()
    import gc as _gc

    _gc.collect()
    after_collect = len(suspended_frames())
    print(f"  After clearing the cycle and collecting: {before} -> {after_collect}")
    print("  (some other module's coroutines remain; the point is that the ones")
    print("   above are gone, so the sweep reflects liveness rather than history.)")


def demo_pep669_yield() -> None:
    print()
    print("=" * 78)
    print("6. PY_YIELD (3.12+): SUSPENSION AS AN EVENT")
    print("=" * 78)
    if not HAS_MONITORING:
        print("  sys.monitoring requires 3.12+; skipping.")
        return
    from framecraft import MonitorTracer

    cfg = TracerConfig(events=(CALL, YIELD, RETURN), capture_locals=True,
                       include=lambda c: c.co_name == "counter")
    with MonitorTracer(cfg) as t:
        g = counter(3)
        for _ in range(4):
            try:
                next(g)
            except StopIteration:
                break
    for ev in t.events:
        print(f"    {ev.kind:<10} depth={ev.depth}  line {ev.lineno}  "
              f"retval={ev.retval_repr}  locals={ {b.name: b.value for b in ev.locals} }")
    print()
    print("  PY_YIELD fires with the value being yielded and the frame still")
    print("  populated -- so 'what was suspended here' is answerable from the")
    print("  event alone, with no reference to the generator object needed.")
    print()
    print("  Compare example 01: a `sys.settrace` tracer gets NO 'yield' event at")
    print("  all on 3.12+. `sys.settrace` has no way to express suspension; it just")
    print("  stops producing events for the frame until it is resumed. So a")
    print("  settrace-based execution recorder cannot tell 'suspended' from")
    print("  'finished', which is a real limit on what an execution trace can")
    print("  represent.")
    g.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=["1", "2", "3", "4", "5", "6"])
    ns = ap.parse_args()
    todo = ns.only or "123456"
    if "1" in todo:
        demo_generator_lifecycle()
    if "2" in todo:
        demo_suspended_state()
    if "3" in todo:
        demo_nested_generators()
    if "4" in todo:
        demo_coroutines()
    if "5" in todo:
        demo_gc_sweep()
    if "6" in todo:
        demo_pep669_yield()
    print("=" * 78)
    print("Next: 11 -- threads, asyncio tasks, and the timeline view")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
