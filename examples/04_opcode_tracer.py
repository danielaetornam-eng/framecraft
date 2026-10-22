#!/usr/bin/env python3
"""Example 04 -- Opcode-level tracing: seeing the interpreter itself.

Run me::

    python examples/04_opcode_tracer.py
    python examples/04_opcode_tracer.py --dis

What an opcode trace shows
--------------------------

A line event says "line 7 is about to run".  An opcode event says "``STORE_FAST``
slot 3 is about to run".  When the two disagree you have found the interesting
class of bug: a statement that never executes the lines you think it does.
Comprehensions, `with`, `try`, decorators, `async`, and -- since 3.11 -- the
inline caches all behave in ways the source does not suggest.

The CPython 3.14 trap that this example exists for
--------------------------------------------------

The documented way to request opcode events is to set ``f_trace_opcodes`` on the
frame and return the local hook from the global hook::

    def global_trace(frame, event, arg):
        if event == 'call':
            frame.f_trace_opcodes = True     # <-- this flag is SILENTLY LOST
            return local_trace

On CPython 3.14.6 that produces **zero** ``'opcode'`` events, with no warning.
``f_trace_lines`` survives; ``f_trace_opcodes`` does not.  Assigning
``frame.f_trace`` *before* the flags fixes it, because the flag setter then has
a live trace object to write into::

    def global_trace(frame, event, arg):
        if event == 'call':
            frame.f_trace = local_trace      # must come FIRST
            frame.f_trace_opcodes = True
            return local_trace

``FunctionTracer._apply_verbosity`` does both.  Below is the raw experiment so
you can reproduce it yourself.
"""

from __future__ import annotations

import argparse
import dis
import sys
from collections import Counter

from framecraft import OPCODE, FunctionTracer, TracerConfig
from framecraft._internal.compat import HAS_MONITORING, monitoring_events

# ----------------------------------------------------------------------
# The experiment
# ----------------------------------------------------------------------


def _naive_workload(n: int) -> int:
    total = 0
    for i in range(n):
        total += i * 2
    return total


def experiment_return_path():
    """Flags first, rely on the return value to install f_trace."""
    seen: Counter = Counter()

    def local(frame, event, arg):
        seen[event] += 1
        return local

    def glob(frame, event, arg):
        if event == "call":
            frame.f_trace_lines = True
            frame.f_trace_opcodes = True
            return local
        return None

    sys.settrace(glob)
    try:
        _naive_workload(2)
    finally:
        sys.settrace(None)
    return seen


def experiment_assign_first():
    """Assign frame.f_trace before setting the flags."""
    seen: Counter = Counter()

    def local(frame, event, arg):
        seen[event] += 1
        return local

    def glob(frame, event, arg):
        if event == "call":
            frame.f_trace = local
            frame.f_trace_lines = True
            frame.f_trace_opcodes = True
            return local
        return None

    sys.settrace(glob)
    try:
        _naive_workload(2)
    finally:
        sys.settrace(None)
    return seen


def show_experiment() -> None:
    print("=" * 78)
    print("1. THE 3.14 f_trace_opcodes TRAP (measured, not folklore)")
    print("=" * 78)
    a = experiment_return_path()
    b = experiment_assign_first()
    print(f"  flags-then-return      -> {dict(a)}")
    print(f"  assign f_trace first   -> {dict(b)}")
    print()
    if a.get("opcode", 0) == 0 and b.get("opcode", 0) > 0:
        print("  Confirmed on this interpreter: the documented order loses every")
        print("  opcode event, silently. framecraft assigns f_trace first.")
    else:
        print("  This interpreter does not exhibit the quirk (both orders emit")
        print("  opcodes). framecraft still assigns f_trace first, because relying")
        print("  on either order being fine is how tools break on the next version.")
    print()
    print("  Every hook below also sets `frame.f_trace = local` first. If you write")
    print("  your own hook, do the same.")


# ----------------------------------------------------------------------
# Code where the opcode trace is genuinely illuminating
# ----------------------------------------------------------------------


def tricky(n: int) -> list:
    """Three constructs whose bytecode surprises people."""
    squares = [i * i for i in range(n)]  # comprehension
    total = 0
    for sq in squares:
        total += sq
    return squares


def compare_line_vs_opcode() -> None:
    print()
    print("=" * 78)
    print("2. LINE EVENTS vs OPCODE EVENTS for the same function")
    print("=" * 78)
    cfg = TracerConfig(events=(OPCODE,), include=lambda c: c.co_name == "tricky")
    with FunctionTracer(cfg, opcode_events=True) as t:
        tricky(3)
    line_cfg = TracerConfig(events=("line",), include=lambda c: c.co_name == "tricky")
    with FunctionTracer(line_cfg) as tl:
        tricky(3)

    op_events = [e for e in t.events if e.kind == OPCODE]
    line_events = [e for e in tl.events if e.kind == "line"]
    print(f"  line events   : {len(line_events):>4}   (one per source line executed)")
    print(f"  opcode events : {len(op_events):>4}   (one per instruction)")
    print(f"  ratio         : {len(op_events) / max(1, len(line_events)):.1f}x")
    print()
    print("  opcode-by-opcode:")
    by_line: dict = {}
    for e in op_events:
        by_line.setdefault(e.lineno, []).append(e.opcode)
    for lineno in sorted(by_line):
        print(f"    line {lineno}: {' '.join(by_line[lineno])}")
    print()
    print("  Things worth noticing:")
    print("   * the comprehension statement is ~22 instructions but ONE line event.")
    print("     A line tracer tells you a loop ran 3 times; the opcode trace shows")
    print("     FOR_ITER four times -- three hits plus the exhausted iterator.")
    print("     That off-by-one is exactly the thing that hides a `for` bug.")
    print("   * `STORE_FAST_LOAD_FAST` and `LOAD_FAST_BORROW_LOAD_FAST_BORROW` are")
    print("     3.13+ superinstructions: the peephole optimiser fused pairs of")
    print("     opcodes. A tool that hard-codes opcode names from 3.11 will fail")
    print("     to recognise them, and must fall back to the offset, not the name.")
    print()
    comp_line = comprehension_line_number()
    print(f"  source line {comp_line} is:  squares = [i * i for i in range(n)]")
    shown = 0
    for ins in dis.get_instructions(tricky.__code__):
        if ins.opname in ("CACHE", "EXTENDED_ARG"):
            continue
        if getattr(ins, "positions", None) and ins.positions.lineno == comp_line:
            print(f"      offset {ins.offset:<4} {ins.opname:<22} {ins.argrepr}")
            shown += 1
    if shown == 0:
        print("      (no instructions attributed to that line -- positions table)")
    print()
    print("  And under PEP 709 (3.12+) the comprehension was inlined into `tricky`,")
    print("  so there is no separate <listcomp> code object to trace. On 3.11 and")
    print("  earlier there is, and a naive tracer that filters by co_filename would")
    print("  silently miss it if it also filtered by co_name != '<listcomp>'.")


def comprehension_line_number() -> int:
    """Find the line of the `squares = [...]` statement, robustly."""
    import inspect

    src, start = inspect.getsourcelines(tricky)
    for i, line in enumerate(src):
        if line.strip().startswith("squares ="):
            return start + i
    return start + 1


def show_inline_caches() -> None:
    print()
    print("=" * 78)
    print("3. WHY f_lasti IS A BYTE OFFSET AND NOT AN INSTRUCTION INDEX")
    print("=" * 78)
    print("  3.11 introduced inline caches: every instruction is followed by")
    print("  CACHE slots that the specialising interpreter overwrites. So the")
    print("  instruction at index 5 can live at byte offset 12.")
    print()
    raw = list(dis.get_instructions(tricky.__code__))
    print(f"  {'idx':>4} {'offset':>7}  {'opname':<24} positions")
    for i, ins in enumerate(raw[:14]):
        pos = ins.positions
        pos_s = f"{pos.lineno}:{pos.col_offset}-{pos.end_col_offset}" if pos and pos.lineno else "-"
        print(f"  {i:>4} {ins.offset:>7}  {ins.opname:<24} {pos_s}")
    print()
    offsets = [ins.offset for ins in raw if ins.opname not in ("CACHE", "EXTENDED_ARG")]
    print(f"  real instruction offsets: {offsets}")
    import itertools

    print(f"  deltas between them     : {[b - a for a, b in itertools.pairwise(offsets)][:12]}")
    print()
    print("  Any code that does `instructions[f_lasti]` is indexing by byte offset")
    print("  into a list indexed by instruction number. It works until it doesn't.")
    print("  framecraft precomputes `byte offset -> (line, opname)` once per code")
    print("  object (`_OpcodeIndex`) and looks that up instead.")


def show_pep669_equivalent() -> None:
    print()
    print("=" * 78)
    print("4. THE PEP 669 EQUIVALENT: INSTRUCTION events (3.14+)")
    print("=" * 78)
    if not HAS_MONITORING:
        print("  sys.monitoring requires 3.12+; skipping.")
        return
    if "INSTRUCTION" not in monitoring_events():
        print("  This interpreter has no INSTRUCTION event (3.12/3.13).")
        print("  Use opcode_events=True on FunctionTracer instead.")
        print(f"  available: {sorted(monitoring_events())}")
        return
    from framecraft import MonitorTracer

    with MonitorTracer(events=(OPCODE,), include=lambda c: c.co_name == "tricky") as t:
        tricky(3)
    events = [e for e in t.events if e.kind == OPCODE]
    print(f"  {len(events)} INSTRUCTION events for tricky(3)")
    print(f"  example: {events[0].summary() if events else '(none)'}")
    print()
    print("  INSTRUCTION is the 3.14 replacement for the C-call events that PEP 669")
    print("  originally had (C_CALL/C_FUNCTION/C_FUNCTION_RETURN were removed), and")
    print("  it is also the modern replacement for sys.settrace's 'opcode' event.")
    print("  Its documented callback signature is (code, offset, opname, arg,")
    print("  argrepr), but 3.14.6 passes only (code, offset) -- so framecraft looks")
    print("  the opcode up from its own cached disassembly instead of trusting the")
    print("  arity. See MonitorTracer._h_instruction.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dis", action="store_true", help="only disassemble")
    ns = ap.parse_args()
    if ns.dis:
        dis.dis(tricky)
        return 0
    show_experiment()
    compare_line_vs_opcode()
    show_inline_caches()
    show_pep669_equivalent()
    print()
    print("=" * 78)
    print("Next: 05 -- the setprofile hook, and seeing into C functions")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
