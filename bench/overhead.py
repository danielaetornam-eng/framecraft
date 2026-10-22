"""framecraft's overhead benchmark.

Reports **nanoseconds per event**, not a ratio against baseline, because a
ratio inherits all the noise of a sub-millisecond baseline and tells you nothing
about the thing you actually want to compare.

Run::

    python bench/overhead.py                 # the standard matrix
    python bench/overhead.py --quick
    python bench/overhead.py --json results.json
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from collections.abc import Callable
from typing import Any

from framecraft import (
    CALL,
    LINE,
    RETURN,
    FunctionTracer,
    MonitorTracer,
    ProfileTracer,
    SamplingTracer,
    TracerConfig,
)
from framecraft._internal.compat import HAS_MONITORING
from framecraft.sinks import NullSink

# ----------------------------------------------------------------------
# The workload.  Deliberately line-heavy, because that is what makes tracers
# expensive and therefore what a benchmark has to stress.
# ----------------------------------------------------------------------


def spin(n: int) -> int:
    """A tight loop: many lines, few calls. The line tracer's worst case."""
    total = 0
    for i in range(n):
        total = total + i
    return total


def churn(n: int) -> int:
    """Many calls, few lines: the call tracer's worst case."""
    total = 0
    for _ in range(n):
        total = total + helper(1)
    return total


def helper(x: int) -> int:
    return x + 1


def measure(fn: Callable[[], Any], *, target_seconds: float, reps: int = 5) -> tuple[float, int]:
    """Return ``(best_seconds, events)`` over several repetitions.

    ``best`` rather than mean: tracer overhead is noisy in the *pessimistic*
    direction (GC, scheduler, page faults), so the minimum is the most
    reproducible estimate of the cost and the mean is dominated by outliers you
    did not cause.  The minimum is also the number you care about when asking
    "what does this cost me on a quiet machine".
    """
    best = float("inf")
    events = 0
    for _ in range(reps):
        gc.collect()
        t0 = time.perf_counter()
        events = fn()
        best = min(best, time.perf_counter() - t0)
    return best, events


def timed(body: Callable[[], Any], seconds: float, count: Callable[[], int]) -> Callable[[], Any]:
    def run() -> int:
        deadline = time.perf_counter() + seconds
        n = 0
        while time.perf_counter() < deadline:
            body()
            n += 1
        return n

    return run


def ns_per_event(seconds: float, events: int) -> float:
    return (seconds / events * 1e9) if events else float("nan")


CONFIGS: list[tuple[str, str, dict]] = [
    ("settrace  call/return", "settrace",
     {"events": (CALL, RETURN)}),
    ("settrace  call/line/return", "settrace",
     {"events": (CALL, LINE, RETURN)}),
    ("settrace  call/line/return +locals", "settrace",
     {"events": (CALL, LINE, RETURN), "capture_locals": True}),
    ("settrace  call/line/return no-timing", "settrace",
     {"events": (CALL, LINE, RETURN), "timing": False}),
    ("setprofile call/return", "profile",
     {"events": (CALL, RETURN)}),
    ("monitoring call/return", "monitoring",
     {"events": (CALL, RETURN)}),
    ("monitoring call/line/return", "monitoring",
     {"events": (CALL, LINE, RETURN)}),
    ("monitoring call/line/return no-timing", "monitoring",
     {"events": (CALL, LINE, RETURN), "timing": False}),
]


def build(engine: str, kwargs: dict) -> Any:
    """Construct a tracer with a NullSink and buffering on.

    Buffering is deliberately ON for the benchmark even though production traces
    should stream: it lets us count events *exactly* instead of estimating them,
    and a list append is far cheaper than the hook dispatch we are measuring, so
    it does not distort the comparison between engines.
    """
    cfg = TracerConfig(buffer=True, **kwargs)
    if engine == "settrace":
        return FunctionTracer(cfg)
    if engine == "profile":
        return ProfileTracer(cfg)
    if engine == "monitoring":
        return MonitorTracer(cfg)
    raise ValueError(engine)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=0.5, help="per-configuration duration")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--workload", choices=["spin", "churn"], default="spin")
    ap.add_argument("--json", help="also write the numbers here")
    ns = ap.parse_args()
    if ns.quick:
        ns.seconds, ns.reps = 0.2, 3

    body = (lambda: spin(50)) if ns.workload == "spin" else (lambda: churn(50))
    label = f"{ns.workload}(50)"
    print(f"framecraft overhead benchmark -- workload {label}, "
          f"{ns.seconds}s x {ns.reps} reps, best-of\n")

    # --- baseline ----------------------------------------------------------
    def measure_baseline() -> tuple[float, int]:
        """Best-of-N unhooked cost per iteration, in nanoseconds."""
        best, iters = float("inf"), 0
        for _ in range(ns.reps):
            gc.collect()
            t0 = time.perf_counter()
            deadline = t0 + ns.seconds
            it = 0
            while time.perf_counter() < deadline:
                body()
                it += 1
            best = min(best, (time.perf_counter() - t0) / it * 1e9)
            iters = it
        return best, iters

    base_ns, base_iters = measure_baseline()
    print(f"  {'baseline (no hook), measured FIRST':<42} {base_iters:>11,} iters  "
          f"{base_ns:>7.0f} ns/iter")
    print()
    print(f"  {'configuration':<42} {'iterations':>11} {'events':>11} {'ns/event':>10} {'slowdown':>9}")
    print(f"  {'-' * 42} {'-' * 11} {'-' * 11} {'-' * 10} {'-' * 9}")

    results: dict[str, Any] = {
        "python": sys.version.split()[0],
        "workload": label,
        "baseline_ns_per_iter": round(base_ns, 1),
    }


    for name, engine, kwargs in CONFIGS:
        if engine == "monitoring" and not HAS_MONITORING:
            continue
        tracer = build(engine, kwargs)
        tracer.add_sink(NullSink())

        # Each configuration runs for the same *wall time*, so the raw seconds
        # are all equal and comparing them is meaningless.  What matters is how
        # many iterations of the workload it managed, i.e. the per-iteration
        # cost, and the slowdown factor that implies.
        best_ns_per_iter, events, iters = float("inf"), 0, 0
        for _ in range(ns.reps):
            tracer.events.clear()
            gc.collect()
            t0 = time.perf_counter()
            deadline = t0 + ns.seconds
            it = 0
            with tracer:
                while time.perf_counter() < deadline:
                    body()
                    it += 1
            el = time.perf_counter() - t0
            # Exact: the buffer holds every event the engine emitted.
            events = len(tracer.events)
            iters = it
            if el / it < best_ns_per_iter:
                best_ns_per_iter = el / it
        slowdown = best_ns_per_iter * 1e9 / base_ns
        print(f"  {name:<42} {iters:>11,} {events:>11,} "
              f"{ns_per_event(best_ns_per_iter, events):>10.0f} {slowdown:>8.1f}x")
        results[name] = {
            "iterations": iters,
            "events": events,
            "ns_per_iteration": round(best_ns_per_iter * 1e9, 1),
            "ns_per_event": round(ns_per_event(best_ns_per_iter, events), 1),
            "slowdown": round(slowdown, 2),
        }

    # --- sampling ----------------------------------------------------------
    sampler = SamplingTracer(interval=0.001)
    sampler.add_sink(NullSink())
    best_ns_per_iter = float("inf")
    iters = 0
    for _ in range(ns.reps):
        sampler.tally.clear()
        sampler.total_samples = 0
        gc.collect()
        t0 = time.perf_counter()
        deadline = t0 + ns.seconds
        it = 0
        with sampler:
            while time.perf_counter() < deadline:
                body()
                it += 1
        iters = it
        best_ns_per_iter = min(best_ns_per_iter, (time.perf_counter() - t0) / it)
    slowdown = best_ns_per_iter * 1e9 / base_ns
    print(f"  {'sampling 1 kHz (no instrumentation)':<42} {iters:>11,} "
          f"{sampler.total_samples:>11,} {'approx':>10} {slowdown:>8.1f}x")
    results["sampling"] = {
        "iterations": iters,
        "samples": sampler.total_samples,
        "effective_hz": round(sampler.effective_hz, 1),
        "ns_per_iteration": round(best_ns_per_iter * 1e9, 1),
        "slowdown": round(slowdown, 2),
    }
    print()
    print(f"  sampler achieved {sampler.effective_hz:.0f} Hz for a 1 ms requested interval.")
    if sampler.effective_hz < 400:
        print("  That is the OS timer, not framecraft, setting the rate: Windows")
        print("  defaults to a ~15.6 ms tick and `time.sleep` will not beat it.")
        print("  Always report effective_hz; a '1 kHz' profile that got 64 Hz is")
        print("  still statistically valid, just coarser -- and a tool that")
        print("  claims 1 kHz without checking is lying.")

    print()
    # --- baseline again ----------------------------------------------------
    # Re-measuring the baseline at the *end* is not redundant: it exposes
    # ordering effects (CPU frequency ramp, page-cache warming, a background
    # process finishing) that would otherwise be silently attributed to the
    # tracer under test.  If the two baselines differ by more than a few
    # percent, every slowdown number here is suspect and the tool says so.
    base_ns_2, base_iters_2 = measure_baseline()
    drift = (base_ns_2 - base_ns) / base_ns * 100
    print(f"  {'baseline (no hook), measured LAST':<42} {base_iters_2:>11,} iters  "
          f"{base_ns_2:>7.0f} ns/iter   ({drift:+.1f}% vs first)")
    results["baseline_ns_per_iter_last"] = round(base_ns_2, 1)
    if abs(drift) > 5:
        print()
        print(f"  !! baseline drifted {drift:+.1f}% over the run, so the slowdown")
        print("     column is not trustworthy here. Increase --seconds/--reps, or")
        print("     interleave baseline and tracer measurements.")
    print()

    print("  Reading this table:")
    print("   * 'slowdown' is iterations-per-second lost, and is the number that")
    print("     matters if you are deciding whether to trace in production.")
    print("   * 'ns/event' is the comparable per-event cost. A ratio to baseline")
    print("     inherits the workload's own cost and is easy to misread.")
    print("   * Every configuration ran for the same wall time, so the iteration")
    print("     counts -- not the seconds -- are what differ.")
    print("   * Bare interpreter dispatch for a *no-op* hook is ~5-15x baseline")
    print("     (measured in examples/06). Everything above that is Python-level")
    print("     work inside the callback, which is why this library memoises")
    print("     per-code-object verdicts and caches offset->line maps.")
    print("   * no-timing shows what removing one clock read per event buys.")
    print("   * Sampling is the only approximate row: it counts timer ticks, not")
    print("     observations. Its event count cannot be compared with the others.")

    if ns.json:
        with open(ns.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nwrote {ns.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
