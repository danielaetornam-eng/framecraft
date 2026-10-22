#!/usr/bin/env python3
"""Example 09 -- Exceptions, unwinding, and capturing state at the moment of failure.

Run me::

    python examples/09_exceptions_and_unwinding.py
    python examples/09_exceptions_and_unwinding.py --unwind-only

The problem
-----------

A debugger wants to answer "what did this frame look like when it blew up?".  By
the time your program prints a traceback, that information is gone: the frames
are dead, the locals are unreachable, and the traceback only tells you *where*,
not *what*.

Two events can capture it, and they are not the same event:

============  ==========================  ====================================
event         fires                       what the frame's state is
============  ==========================  ====================================
``exception``  the exception is raised    mid-unwind; still partly live
``unwind``     the frame is being torn    **last** moment it exists at all
               down
============  ==========================  ====================================

With ``sys.settrace`` you get ``'exception'`` only.  With PEP 669 you get both
``EXCEPTION`` and ``PY_UNWIND``, and ``PY_UNWIND`` is the one that matters --
after the callback returns, the frame is gone.

The subtlety that bites everyone
--------------------------------

An ``exception`` event fires **once per frame the exception propagates through**,
so a five-deep stack produces five ``exception`` events for one failure.  That is
correct, and it is also how people end up reporting "5 exceptions raised" when
one was.  The right way to count is to deduplicate by
``(type, code_hash, lineno)`` -- which is exactly what
:func:`framecraft.analysis.exception_summary` does.

``sys.exc_info`` vs ``sys.exception``
--------------------------------------

Inside a trace hook, ``sys.exc_info()`` reports whatever is being handled *at
the hook's own frame*, which is usually nothing.  The exception that caused the
event is the hook's ``arg``.  On 3.11+ ``sys.exception()`` is the modern
equivalent for code running inside a handler, and is also what the hook should
consult when it needs context beyond its argument.
"""

from __future__ import annotations

import argparse
import sys
import traceback

from framecraft import CALL, EXCEPTION, LINE, RETURN, FunctionTracer, TracerConfig
from framecraft._internal.compat import HAS_MONITORING
from framecraft.analysis import exception_summary, render_table

# ----------------------------------------------------------------------
# A failure with interesting state at each level
# ----------------------------------------------------------------------


def parse_record(text: str) -> dict:
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError(f"expected 3 colon-separated fields, got {len(parts)}: {text!r}")
    name, count, price = parts
    return {"name": name, "count": int(count), "price": float(price)}


def load_batch(lines: list) -> list:
    """Two bad records; the good ones must still come back."""
    records = []
    errors = []
    for lineno, line in enumerate(lines, start=1):
        try:
            records.append(parse_record(line))
        except (ValueError, TypeError) as exc:
            errors.append((lineno, str(exc)))
    if errors:
        raise BatchError(errors, records)
    return records


class BatchError(Exception):
    def __init__(self, errors, partial):
        super().__init__(f"{len(errors)} bad record(s)")
        self.errors = errors
        self.partial = partial


def process_batch(lines: list) -> float:
    records = load_batch(lines)
    return sum(r["count"] * r["price"] for r in records)


GOOD = ["apple:2:1.50", "pear:4:2.25"]
MIXED = ["apple:2:1.50", "broken", "pear:oops:2.25", "plum:1:9.99"]


# ----------------------------------------------------------------------


def demo_settrace_exception() -> None:
    print("=" * 78)
    print("1. WHAT sys.settrace GIVES YOU: 'exception', ONE PER FRAME")
    print("=" * 78)
    cfg = TracerConfig(
        events=(CALL, LINE, RETURN, EXCEPTION),
        capture_locals=True,
        include=lambda c: c.co_filename == __file__,
    )
    with FunctionTracer(cfg) as t:
        try:
            process_batch(MIXED)
        except BatchError:
            pass
    exc_events = [e for e in t.events if e.kind == EXCEPTION]
    print(f"  one BatchError was raised. 'exception' events: {len(exc_events)}")
    print()
    for ev in exc_events:
        print(f"    depth={ev.depth}  {ev.qualname:<16} {ev.location:<34} {ev.exception_type}")
    print()
    print("  ONE failure, several events. The exception propagates up the stack and")
    print("  every frame it passes through gets an event. That is the design, and")
    print("  it is why `len(exception events) != len(exceptions raised)`.")
    print()
    print("  Deduplicated:")
    print(render_table(exception_summary(t.events), ["type", "raised_in", "location", "count"]))
    print()
    print("  Each event also captured the locals, which is the whole point:")
    for ev in exc_events:
        if ev.qualname == "parse_record":
            print(f"    at {ev.location}:")
            for b in ev.locals:
                print(f"        {b.scope:<7} {b.name:<8} = {b.value[:60]}")


def demo_where_was_the_value() -> None:
    print()
    print("=" * 78)
    print("2. THE INFORMATION A TRACEBACK DOES NOT GIVE YOU")
    print("=" * 78)
    print("  traceback gives you location:")
    try:
        process_batch(MIXED)
    except BatchError:
        tb = sys.exc_info()[2]
    frames = traceback.extract_tb(tb)
    for f in frames:
        print(f"    {f.filename.split(chr(92))[-1]}:{f.lineno} in {f.name}  line={f.line!r}")
    print()
    print("  A tracer gives you location AND state, at the moment it was wrong:")
    cfg = TracerConfig(events=(EXCEPTION,), capture_locals=True,
                       include=lambda c: c.co_filename == __file__)
    with FunctionTracer(cfg) as t:
        try:
            process_batch(MIXED)
        except BatchError:
            pass
    for ev in t.events:
        if ev.qualname == "parse_record":
            print(f"    {ev.location}  exception={ev.exception_repr}")
            for b in ev.locals:
                print(f"        {b.name:<8} = {b.value[:70]}")
            break
    print()
    print("  `text` and `parts` are exactly the values you need to debug the bug,")
    print("  and a traceback contains neither. This is the case for a tracer that")
    print("  captures locals: it is post-mortem, portable, and reviewable.")
    print()
    print("  The BatchError also carries `partial` -- the successfully parsed")
    print("  records -- which is real application state, not something any")
    print("  exception handler in the stdlib records for you.")
    try:
        process_batch(MIXED)
    except BatchError as exc:
        print(f"    partial = {exc.partial}")


def demo_unwind_only() -> None:
    print()
    print("=" * 78)
    print("3. PY_UNWIND: THE LAST MOMENT THE FRAME EXISTS (3.12+ ONLY)")
    print("=" * 78)
    if not HAS_MONITORING:
        print("  sys.monitoring requires 3.12+; skipping.")
        print("  The equivalent with sys.settrace is an 'exception' event with")
        print("  capture_locals=True, which is what section 1 does.")
        return
    from framecraft import MonitorTracer

    cfg = TracerConfig(events=(CALL, EXCEPTION), capture_locals=True,
                       include=lambda c: c.co_filename == __file__)
    with MonitorTracer(cfg) as t:
        try:
            process_batch(MIXED)
        except BatchError:
            pass
    print(f"  {len(t.events)} events total")
    print()
    print("  The events, with phase shown. `raised` is EXCEPTION at the raise site;")
    print("  `raise-opcode` is the 3.14 RAISE event, which fires *before* the")
    print("  exception object exists and is the only signal for a raise handled")
    print("  inside its own frame; `unwind` is PY_UNWIND, exactly once per frame")
    print("  being torn down; plain entries are PY_START, with no phase.")
    print()
    markers = {
        "unwind": "UNWIND ",
        "raised": "RAISE  ",
        "raise-opcode": "RAISE^ ",
        "start": "start  ",
    }
    for ev in t.events:
        phase = ev.extra.get("phase", "start")
        print(f"    {markers.get(phase, phase):<8}depth={ev.depth}  "
              f"{ev.qualname:<14} {ev.location}")
        if phase == "unwind":
            for b in ev.locals[:4]:
                print(f"                     {b.name:<10} = {b.value[:54]}")
    print()
    print("  Read that nesting: process_batch(0) -> load_batch(1) -> parse_record(2)")
    print("  -> parse_record(3). Two records were malformed, so there are two")
    print("  separate parse_record unwinds -- one failure each, correctly reported.")
    print("  `depth` counts admitted frames live beneath, which is the number a")
    print("  flame graph or a stack tree needs, and it is stable across engines.")
    print()
    print("  Note what is NOT here: no UNWIND for the first, successful parse_record.")
    print("  PY_UNWIND is strictly for frames torn down by an exception; a normal")
    print("  return produces PY_RETURN instead. That asymmetry is useful -- it lets")
    print("  you separate 'this frame failed' from 'this frame finished' without")
    print("  inspecting the exception object.")
    print()
    print("  Each `unwind` line above is the LAST moment that frame's locals")
    print("  exist. The callback returns, CPython frees the frame, and the state")
    print("  is unreachable forever. This is the correct event for 'what was true")
    print("  when it failed', and the reason PEP 669 is worth the version guard.")


def demo_chaining() -> None:
    print()
    print("=" * 78)
    print("4. EXCEPTION CHAINING IS VISIBLE IN THE TRACE")
    print("=" * 78)
    cfg = TracerConfig(events=(EXCEPTION,), include=lambda c: c.co_filename == __file__)
    with FunctionTracer(cfg) as t:
        try:
            chained()
        except RuntimeError:
            pass
    by_frame: dict[str, list] = {}
    for ev in t.events:
        by_frame.setdefault(ev.qualname, []).append(ev)
    for fn, evs in by_frame.items():
        for ev in evs:
            print(f"    {fn:<14} {ev.location:<32} {ev.exception_type}")
    print()
    print("  `raise X from Y` and implicit `__context__` chaining produce TWO")
    print("  exception events in the raising frame, in order: the original, then")
    print("  the wrapper. A tracer that only records the last one loses the cause.")
    print("  This is why TraceEvent.exception_repr is a string of the *instance*")
    print("  (via safe_repr) rather than just a type name -- the chain is on the")
    print("  instance.")
    print()
    print("  Chained traceback, for comparison:")
    try:
        chained()
    except RuntimeError as exc:
        traceback.print_exception(type(exc), exc, exc.__traceback__, limit=3)


def chained():
    try:
        parse_record("nope")
    except ValueError as exc:
        raise RuntimeError("could not parse input") from exc


def demo_finally() -> None:
    print()
    print("=" * 78)
    print("5. finally: CODE THAT RUNS DURING UNWINDING IS ORDINARY CODE")
    print("=" * 78)
    cfg = TracerConfig(events=(LINE,), include=lambda c: c.co_filename == __file__)
    with FunctionTracer(cfg) as t:
        try:
            with_cleanup()
        except ValueError:
            pass
    import inspect

    src, start = inspect.getsourcelines(with_cleanup)
    text_of = {start + i: line.strip() for i, line in enumerate(src)}
    hits = sorted(e.lineno for e in t.events if e.lineno in cleanup_lines())
    print("  line events inside the `finally:` block:")
    for lineno in hits:
        print(f"    line {lineno}: {text_of.get(lineno, '')}")
    print()
    print("  A `finally:` block runs while the exception is propagating, so its")
    print("  line events arrive between the raise and the handlers. This is why")
    print("  an 'exception' event's `depth` is not necessarily adjacent to the")
    print("  events around it -- unwind interleaves with cleanup, and that is")
    print("  correct, not a bug in your tracer.")
    print()
    print("  Note also that `return` inside `finally:` swallows the exception")
    print("  entirely. A tracer is the only thing that will show you that the")
    print("  exception was raised and then discarded.")


def cleanup_lines() -> set:
    """Line numbers inside with_cleanup's ``finally:`` block."""
    import inspect

    src, start = inspect.getsourcelines(with_cleanup)
    return {start + i for i, line in enumerate(src) if "record[" in line}


def with_cleanup():
    record = {"cleaned": False}
    try:
        raise ValueError("inner failure")
    finally:
        record["cleaned"] = True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--unwind-only", action="store_true")
    ns = ap.parse_args()
    if ns.unwind_only:
        demo_unwind_only()
        return 0
    demo_settrace_exception()
    demo_where_was_the_value()
    demo_unwind_only()
    demo_chaining()
    demo_finally()
    print("=" * 78)
    print("Next: 10 -- generators, coroutines and suspended frames")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
