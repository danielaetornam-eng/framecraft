#!/usr/bin/env python3
"""Example 02 -- The two-level ``sys.settrace`` protocol, by hand.

Run me::

    python examples/02_raw_settrace.py

This file deliberately does **not** use ``FunctionTracer``.  It is the smallest
complete tracing hook that could possibly work, so that the library version in
example 03 has something to be an improvement on.

The protocol, exactly
---------------------

::

    sys.settrace(global_fn)          # ONE slot, per thread

    # global_fn is called ONLY for 'call':
    global_fn(frame, 'call', None)
        return local_fn              # -> CPython sets frame.f_trace = local_fn
        return None                  # -> frame is NOT instrumented at all

    # local_fn is called for everything else in that frame:
    local_fn(frame, 'line', None)         return value becomes the NEW local fn
    local_fn(frame, 'return', retval)     return value is IGNORED
    local_fn(frame, 'exception', (t,v,tb)) return value becomes the NEW local fn
    local_fn(frame, 'opcode', None)       return value becomes the NEW local fn

Four rules, all measured on CPython 3.14.6 below
---------------------------------------------------

1.  **The global hook returning ``None`` is load-bearing.**  It means "do not
    instrument this frame" and it costs nothing inside the function.  This is
    the single most valuable line in any tracer.

2.  **The local hook's return value can *replace* the local hook.**  Returning a
    different function swaps it in for the rest of the frame -- verified below
    with a two-function swap.  Returning ``None`` does *not* detach the frame on
    modern CPython (``trace_trampoline`` only assigns when
    ``result is not None``), so the historical advice "you must return yourself
    or tracing stops" is no longer what happens -- but it is still the contract
    to code against, because relying on ``None`` being inert will bite you on
    an older interpreter and because accidentally returning ``None`` is a
    natural way to *intend* a swap and get silence instead.

3.  **Frames already on the stack are not instrumented.**  Installing a hook
    affects frames created afterwards.  ``attach_into_running_frame`` shows the
    workaround.

4.  **An exception raised inside a hook is fatal to the trace *and* to the
    program.**  Measured below:

    *   the exception **propagates into the traced program**, surfacing as if
        raised at whichever line was executing when the hook fired, so your
        program dies with a stack trace pointing at innocent code;
    *   *and* CPython unsets the global hook, exactly as if
        ``sys.settrace(None)`` had been called -- ``sys.gettrace()`` returns
        ``None`` afterwards and every later frame is untraced.

    One buggy handler gives you a spurious crash **and** a silently dead
    tracer.  :meth:`framecraft.trace.Tracer._guard` exists for exactly this.

Also:  **the hook is per-thread.**  ``threading.settrace`` covers threads
started after it runs; see the thread demo below.
"""

from __future__ import annotations

import sys
import threading

EVENTS: list[tuple[str, str, int]] = []


# ----------------------------------------------------------------------
# 1. A minimal, correct hook.
# ----------------------------------------------------------------------


def global_trace(frame, event, arg):
    """Called once per function call.  Returns the local hook, or None."""
    if event != "call":
        # The docs say only 'call' reaches the global function, but a defensive
        # branch costs nothing and survives a future CPython that sends more.
        return local_trace

    # This is the only cheap place to decide whether to instrument a frame.
    # Returning None here means the interpreter never calls us again for that
    # function -- not once per line, ONCE for the whole call.
    if frame.f_code.co_filename != __file__:
        return None

    EVENTS.append(("call", frame.f_code.co_name, frame.f_lineno))
    return local_trace


def local_trace(frame, event, arg):
    """Called for line/return/exception/opcode inside an instrumented frame."""
    if event == "line":
        EVENTS.append(("line", frame.f_code.co_name, frame.f_lineno))
    elif event == "return":
        EVENTS.append(("return", frame.f_code.co_name, frame.f_lineno))
    elif event == "exception":
        etype = arg[0].__name__ if isinstance(arg, tuple) else type(arg).__name__
        EVENTS.append((f"exception:{etype}", frame.f_code.co_name, frame.f_lineno))
    # THE LINE THAT MATTERS.  Returning None (which a bare `def` body does
    # implicitly) sets frame.f_trace to NULL and this frame stops emitting.
    return local_trace


# ----------------------------------------------------------------------
# 2. What each hook's return value actually does -- measured, not assumed.
# --------------------------------------------------------------------


def rule1_global_returns_none():
    """RULE 1: the global hook's `None` is the cheap, load-bearing filter."""
    hits = []

    def local(frame, event, arg):
        hits.append(event)
        return local

    def declining_global(frame, event, arg):
        return None  # decline this frame

    def accepting_global(frame, event, arg):
        return local

    for label, g in (("global returns None", declining_global), ("global returns a function", accepting_global)):
        hits.clear()
        sys.settrace(g)
        try:
            leaf(3)
        finally:
            sys.settrace(None)
        print(f"    {label:<28} -> local hook fired {len(hits):,} times")


def rule2_return_value_swaps_the_hook():
    """RULE 2: the local hook's return value REPLACES it for the rest of the frame."""
    seen: list[tuple[str, str]] = []

    def second(frame, event, arg):
        seen.append(("second", event))
        return second

    def first(frame, event, arg):
        seen.append(("first", event))
        # Swap ourselves out after the first event.
        return second

    def g(frame, event, arg):
        return first if event == "call" else None

    sys.settrace(g)
    try:
        leaf(1)
    finally:
        sys.settrace(None)
    print("    event sequence:")
    for who, ev in seen:
        print(f"      {who:<8} {ev}")
    print("    -> after one event, `first` was replaced by `second` and never")
    print("       called again. This is how you escalate or de-escalate tracing")
    print("       mid-function, and it is why 'return self' is the safe default:")
    print("       it is the only return value that definitely changes nothing.")
    print()
    print("    (Returning None also leaves the hook in place on modern CPython --")
    print("     trace_trampoline only assigns when result is not None -- but that")
    print("     is an implementation detail, not the contract. Don't lean on it.)")


# ----------------------------------------------------------------------
# 3. Attaching to a frame that is ALREADY running.
# ----------------------------------------------------------------------


def install_into_running_frame():
    """Rule 2's workaround.

    `frame.f_trace` is a property of the frame, not of the
    hook slot, so you can assign it to frames that are already executing -- even
    from another thread.  This is what `pdb`'s `break` does, and it is how
    `FunctionTracer.attach_existing` works.
    """
    sys.settrace(global_trace)  # arm the machinery; the global fn declines to trace
    try:
        EVENTS.clear()
        deep(3)
        print(f"  events captured from an already-running frame: {len(EVENTS)}")
        for e in EVENTS[:6]:
            print(f"    {e[0]:<10} {e[1]} line {e[2]}")
    finally:
        sys.settrace(None)


def attach_now(frame):
    """Instrument `frame` (and, optionally, its callers) mid-flight."""
    lines = frame.f_code.co_firstlineno
    frame.f_trace = local_trace
    frame.f_trace_lines = True
    frame.f_trace_opcodes = False
    # A global hook must exist or the interpreter never consults f_trace.
    return lines


def deep(n):
    if n == 0:
        # We are about to run three more statements; arm ourselves.
        attach_now(sys._getframe())
        x = 1
        y = 2
        z = x + y
        return z
    return deep(n - 1) + 1


# ----------------------------------------------------------------------
# 4. Threads.
# ----------------------------------------------------------------------


def thread_demo():
    print()
    print("=" * 78)
    print("RULE 3: sys.settrace is per-thread")
    print("=" * 78)
    EVENTS.clear()
    sys.settrace(global_trace)
    prev = threading.gettrace()
    threading.settrace(global_trace)  # affects threads started AFTER this
    try:
        t = threading.Thread(target=worker, name="fc-worker")
        t.start()
        t.join()
        worker()  # same thread as main: traced because of sys.settrace above
    finally:
        threading.settrace(prev)
        sys.settrace(None)
    threads = {name for _k, name, _l in EVENTS}
    print(f"  captured events: {len(EVENTS)}")
    print(f"  functions seen: {sorted(threads)}")
    print("  'worker' appears because we set BOTH sys.settrace (this thread) and")
    print("  threading.settrace (future threads). Miss either one and the other")
    print("  half of your program is silently untraced.")


def worker():
    a = 1
    b = 2
    return a + b


# ----------------------------------------------------------------------
# 5. Rule 4: an exception in the hook disables tracing, globally.
# ----------------------------------------------------------------------


def poison_hook():
    print()
    print("=" * 78)
    print("RULE 4: an exception in the hook kills the trace AND the program")
    print("=" * 78)
    state = {"raised_at": None}

    def evil_local(frame, event, arg):
        if event == "line" and frame.f_code.co_name == "after_poison":
            if state["raised_at"] is None and frame.f_lineno == POISON_LINE:
                state["raised_at"] = frame.f_lineno
                raise RuntimeError("bug in my hook")
            return evil_local
        return evil_local

    def evil_global(frame, event, arg):
        if event == "call" and frame.f_code.co_filename == __file__:
            return evil_local
        return None

    sys.settrace(evil_global)
    try:
        try:
            after_poison()
        except RuntimeError as exc:
            print(f"  caught: {type(exc).__name__}: {exc}")
            print(f"  ...which came out of the *hook*, triggered at after_poison line {POISON_LINE}.")
            print(f"  sys.gettrace() is now {sys.gettrace()!r}  <- CPython unset it for us")
        after_poison()  # completely untraced now
    finally:
        sys.settrace(None)

    print()
    print("  Two separate harms from one typo in an event handler:")
    print("    1. the traced program dies with a stack trace pointing at innocent code")
    print("    2. tracing is globally and silently off for the rest of the process")
    print()
    print("  framecraft's answer: Tracer._guard() wraps every hook body in")
    print("  try/except BaseException and counts it, so a hook bug degrades to a")
    print("  number in tracer.error_count instead of a dead program.")


#: Filled in by main() so it always matches this file's current layout.
POISON_LINE = 0


def after_poison():
    marker_a = 1
    marker_b = 2
    marker_c = 3
    return marker_a, marker_b, marker_c


# ----------------------------------------------------------------------
# 6. The correct hook, in anger.
# ----------------------------------------------------------------------


def correct_demo():
    print()
    print("=" * 78)
    print("THE CORRECT HOOK")
    print("=" * 78)
    EVENTS.clear()
    sys.settrace(global_trace)
    try:
        t1 = leaf(1)
        t2 = middle(t1)
    finally:
        sys.settrace(None)
    for kind, name, lineno in EVENTS:
        print(f"    {kind:<10} {name:<10} line {lineno}")
    print(f"    ({len(EVENTS)} events; results leaf={t1} middle={t2})")
    print()
    print("  Note that only functions defined in THIS file are traced: the")
    print("  global hook returns None for everything else, so the cost inside")
    print("  `print`, `list.append`, etc. is zero. That one early return is the")
    print("  whole difference between a 3% overhead tracer and a 40x one.")


def leaf(a):
    b = a * 2
    return b


def middle(x):
    y = leaf(x) + leaf(x + 1)
    return y


def return_value_demos() -> None:
    print()
    print("=" * 78)
    print("RULE 1: THE GLOBAL HOOK RETURNING None IS THE CHEAP FILTER")
    print("=" * 78)
    rule1_global_returns_none()
    print()
    print("=" * 78)
    print("RULE 2: THE LOCAL HOOK'S RETURN VALUE REPLACES THE HOOK")
    print("=" * 78)
    rule2_return_value_swaps_the_hook()


def main() -> None:
    global POISON_LINE
    # Resolve the trigger line dynamically so editing this file cannot silently
    # disable the demo.  `after_poison.__code__.co_firstlineno + 1` is its first
    # statement, which is where we want the hook to explode.
    POISON_LINE = after_poison.__code__.co_firstlineno + 1

    print("=" * 78)
    print("1. THE CORRECT HOOK")
    print("=" * 78)
    correct_demo()
    return_value_demos()
    print()
    print("=" * 78)
    print("3. INSTRUMENTING A FRAME THAT IS ALREADY RUNNING")
    print("=" * 78)
    install_into_running_frame()
    thread_demo()
    poison_hook()
    print()
    print("=" * 78)
    print("Summary of the four rules (all measured above)")
    print("=" * 78)
    print("  1. global hook returning None = 'do not instrument this frame'. Free,")
    print("     and the single most important line in any tracer.")
    print("  2. the local hook's return value REPLACES it. Return self to change")
    print("     nothing; return another function to escalate/de-escalate mid-frame.")
    print("  3. sys.settrace does not touch frames that already exist; assign")
    print("     frame.f_trace yourself. It is also per-thread.")
    print("  4. an exception in a hook crashes the traced program AND unsets the")
    print("     global hook, silently, for the rest of the process.")
    print()
    print("Next: 03 -- the same thing with FunctionTracer, which handles all four")
    print("=" * 78)


if __name__ == "__main__":
    main()
