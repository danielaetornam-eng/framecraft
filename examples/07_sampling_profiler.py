#!/usr/bin/env python3
"""Example 07 -- Sampling: measure without instrumenting.

Run me::

    python examples/07_sampling_profiler.py
    python examples/07_sampling_profiler.py --seconds 3

Every other tracer in this library is *deterministic* -- it sees every event --
and therefore *biased*.  It slows the program down, and it slows different code
paths by different amounts: a line tracer adds a fixed cost per line, so a
function with many short lines is taxed harder than one with few long ones.
That distortion is exactly the thing you were trying to measure.

Sampling inverts the tradeoff.  A background thread wakes every ``interval``,
calls :func:`sys._current_frames` to snapshot every thread's stack, and
collapses each snapshot to a hashable stack key.  Nothing in your program is
instrumented, so nothing in your program is distorted.

What you lose, and what you get instead
---------------------------------------

Lost: line numbers, arguments, return values, exceptions, exact call counts,
determinism.

Gained: honest wall-clock attribution, low and *uniform* overhead, the ability
to profile code that cannot be instrumented, and stack samples from threads you
never hooked.

The statistical contract
------------------------

With ``interval=1ms`` and ``n`` samples, a frame that truly consumes fraction
``f`` of the time appears in about ``n*f`` samples.  So::

    self time of frame X     ~= n * f
    inclusive time of X      ~= n * sum(f) over X and its callees

At 1000 Hz, a function with 0.1% of runtime gets ~1 sample per second.  Never
trust a hot-spot ranking where the top entry has fewer than ~30 samples.
:func:`framecraft.analysis.confidence_note` encodes exactly that rule.

The Windows timer problem
-------------------------

``time.sleep(0.001)`` cannot deliver 1 ms on Windows: the default scheduler tick
is ~15.6 ms, so you get ~64 Hz no matter what you ask for.  That is why
:class:`~framecraft.trace.sampling.SamplingTracer` reports
:attr:`~framecraft.trace.sampling.SamplingTracer.effective_hz` and why every
report in this library prints the *achieved* rate.  A tool that claims "1 kHz
sampling profile" without checking is lying, and the resulting percentages are
coarser than the reader assumes.
"""

from __future__ import annotations

import argparse
import json
import threading
import time

from framecraft import SamplingTracer
from framecraft.analysis import render_table

# ----------------------------------------------------------------------
# A workload with a hot inner loop, which is what profilers exist to find
# ----------------------------------------------------------------------


def hotspot(n: int) -> float:
    """Deliberately expensive and deliberately the thing we want to find."""
    total = 0.0
    for i in range(n):
        total += math_sqrt(i) * 1.000001
    return total


def math_sqrt(x: float) -> float:
    return x ** 0.5


def moderate(n: int) -> int:
    total = 0
    for i in range(n):
        total += i % 7
    return total


def light(n: int) -> int:
    return sum(range(n)) % 3


def workload(seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    i = 0
    while time.perf_counter() < deadline:
        hotspot(2_000)
        moderate(2_000)
        light(2_000)
        i += 1


# ----------------------------------------------------------------------


def demo_hz(seconds: float) -> None:
    print("=" * 78)
    print("1. WHAT RATE DID WE ACTUALLY GET?")
    print("=" * 78)
    for interval in (0.0005, 0.001, 0.005):
        with SamplingTracer(interval=interval) as s:
            workload(seconds)
        print(
            f"  requested {interval * 1000:>6.2f} ms  ->  achieved "
            f"{s.effective_hz:>7.1f} Hz  ({s.total_samples:,} samples, "
            f"{s.duration_ns / 1e9:.2f}s, {len(s.tally)} distinct stacks)"
        )
    print()
    print("  If the achieved rate is far below the requested one, the OS timer")
    print("  is the limit, not framecraft. Report the achieved rate; never the")
    print("  requested one.")


def demo_profile(seconds: float) -> None:
    print()
    print("=" * 78)
    print("2. THE PROFILE")
    print("=" * 78)
    with SamplingTracer(interval=0.001) as s:
        workload(seconds)
    total = s.total_samples or 1
    print(f"  {total:,} samples at {s.effective_hz:.0f} Hz, {len(s.tally)} distinct stacks")
    need = 1000 / s.effective_hz if s.effective_hz else 0
    if need > seconds:
        print(f"  That is NOT enough to conclude anything: {s.effective_hz:.0f} Hz means")
        print(f"  you need about {need:.0f}s to collect 1000 samples. Use --seconds {need:.0f}.")
        print("  Everything below is printed to show the FORMAT, not to be believed.")
    print()
    print(render_table(s.report(14), ["name", "self_samples", "self_pct", "incl_samples", "incl_pct", "confidence"]))
    print()
    print("  Read `confidence` before you read `incl_pct`. A function at 4% of")
    print("  runtime with 6 samples is noise; the same 4% with 6000 samples is")
    print("  signal. This is the single most common way people misread a profile,")
    print("  and it is why every ranking in framecraft carries a sample count.")
    print()
    print("  self = samples where this frame was innermost.")
    print("  incl = samples where it appeared anywhere on the stack.")
    print("  incl - sum(children incl) = self, by construction.")


def demo_stacks(seconds: float) -> None:
    print()
    print("=" * 78)
    print("3. THE STACKS THEMSELVES (collapsed and folded)")
    print("=" * 78)
    from framecraft.graph import StackTree
    from framecraft.sinks import flame_svg

    tree = StackTree("all")
    with SamplingTracer(interval=0.001) as s:
        workload(seconds)
    for key, count in s.tally.items():
        tree.add_path([(f"{s.name_of(h)}  [{i}]", h) for i, h in enumerate(reversed(key))], weight=count)
    total = max(1, tree.total)
    print(f"  {len(list(tree.walk()))} tree nodes for {tree.total:,} samples")
    print()
    print("  hottest root-to-leaf paths:")
    for weight, names in tree.hottest_paths(4):
        print(f"    {weight:>6} ({100.0 * weight / total:5.2f}%)  " + " -> ".join(names))
    print()
    print("  A flame graph from samples is the honest visualisation: width is")
    print("  wall-clock share, not instruction count. Compare with example 11,")
    print("  where the flame graph is built from line events instead.")
    if not __import__("os").environ.get("FRAMECRAFT_NO_WRITE"):
        path = "07_sampling_flame.svg"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(flame_svg(tree, title="sampling profile", unit="samples"))
        print(f"\n  wrote {path}")


def demo_threads(seconds: float) -> None:
    print()
    print("=" * 78)
    print("4. SAMPLING THREADS YOU NEVER HOOKED")
    print("=" * 78)
    results: dict[str, int] = {}

    def worker(name: str, n: int) -> None:
        end = time.perf_counter() + seconds / 2
        while time.perf_counter() < end:
            hotspot(n)
        results[name] = 1

    threads = [threading.Thread(target=worker, args=(f"worker{i}", 500 + i * 250)) for i in range(3)]
    with SamplingTracer(interval=0.001) as s:
        for t in threads:
            t.start()
        workload(seconds)
        for t in threads:
            t.join()
    print(f"  {len(threads)} worker threads + main, sampled with NO hook installed")
    print("  in any of them. sys._current_frames() sees all of them.")
    print(f"  distinct stacks observed: {len(s.tally):,}")
    print(f"  deepest stack depth observed: {max((len(k) for k in s.tally), default=0)}")
    print()
    print("  This is the thing instrumenting tracers do badly. sys.settrace is")
    print("  per-thread; a thread you forgot to hook is silently invisible, and")
    print("  'the program was idle' is the wrong conclusion. Sampling cannot")
    print("  have that failure mode, because it reads the interpreter's own")
    print("  thread table rather than anything the program installed.")


def demo_stack_key(seconds: float) -> None:
    print()
    print("=" * 78)
    print("5. WHY code_hash AND NOT id() OR file:line")
    print("=" * 78)
    with SamplingTracer(interval=0.001) as s:
        workload(seconds)
    keys = list(s.tally)[:3]
    for k in keys:
        print(f"  StackKey(depth={len(k)}):")
        for h in k:
            print(f"      {s.name_of(h)}  hash={h}")
        print()
    print("  A StackKey is a tuple of code *hashes*, innermost first.")
    print()
    print("  Why not the alternatives:")
    print("   * id(code)  -- unstable across processes, so you cannot diff two")
    print("                 profiles taken from different runs.")
    print("   * 'f.py:12' -- ambiguous when a name is defined twice in one file")
    print("                 (a def inside a loop, a conditional import, exec),")
    print("                 and it changes meaning every time you edit above it.")
    print("   * hash of   -- stable across processes, unique in practice, and")
    print("     (name, file, survives the code moving up or down the file.")
    print("     firstlineno, consts, names)")
    print()
    print("  It is still a heuristic. Two distinct functions with identical")
    print("  names, files, line numbers, constants and names would collide;")
    print("  framecraft does not claim otherwise, it just makes collisions")
    print("  unlikely enough to be useful.")


def demo_stats(seconds: float) -> None:
    print()
    print("=" * 78)
    print("6. THE STATS BLOCK, AND WHY EVERY FIELD IS THERE")
    print("=" * 78)
    with SamplingTracer(interval=0.001) as s:
        workload(seconds)
    print(json.dumps(s.stats(), indent=2))
    print()
    print("  total_samples   observations taken")
    print("  distinct_stacks memory is proportional to *stack diversity*, not to")
    print("                  sample count -- which is why this scales to 10^8")
    print("                  samples on a few megabytes")
    print("  effective_hz    the rate actually achieved; see section 1")
    print("  dropped/errors  zero for a sampler, because nothing it does can fail")
    print("                  in the way an instrumented hook can")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--only", choices=["1", "2", "3", "4", "5", "6"])
    ns = ap.parse_args()

    todo = ns.only or "123456"
    if "1" in todo:
        demo_hz(ns.seconds)
    if "2" in todo:
        demo_profile(ns.seconds)
    if "3" in todo:
        demo_stacks(ns.seconds)
    if "4" in todo:
        demo_threads(ns.seconds)
    if "5" in todo:
        demo_stack_key(ns.seconds)
    if "6" in todo:
        demo_stats(ns.seconds)
    print()
    print("=" * 78)
    print("Next: 08 -- call graphs, and why a call graph is not a flame graph")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
