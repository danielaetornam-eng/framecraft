"""Sampling profiler: measure without instrumenting.

Every other tracer in this library is *deterministic* -- it sees every event --
and therefore *biased*: it slows the program down, and the amount it slows
different code paths by is not uniform.  A line tracer adds a fixed cost per
line, so a function with many short lines is taxed harder than one with few
long ones.  That distortion is exactly the thing you were trying to measure.

A sampling profiler inverts the tradeoff.  It observes from the outside:

1.  A background thread wakes every ``interval`` seconds.
2.  It calls :func:`sys._current_frames` to grab a snapshot of *every* thread's
    stack.
3.  Each (thread, stack) is collapsed to a **stack key** -- the tuple of code
    hashes, innermost first.
4.  The sample is tallied against that key.

The program under measurement is not slowed down by anything except the cost of
``sys._current_frames()``, which is O(threads) pointers and does not stop the
world.  On CPython it also cannot observe a GIL switch, so the sample is always
taken while the target thread holds the GIL -- which is a feature: you only
sample threads that are actually runnable.

What you lose, and what you get instead
---------------------------------------

Lost: line numbers, arguments, return values, exceptions, exact call counts,
determinism.
Gained: honest wall-clock attribution, low and *uniform* overhead, the ability
to profile code that cannot be instrumented (a C extension, a signal handler),
and stack samples from threads you never hooked.

The statistical contract
------------------------

With ``interval=1ms`` and ``n`` samples, a frame that truly consumes fraction
``f`` of the time appears in about ``n*f`` samples.  So:

    self time of frame X  ~=  n * f        (its exclusive samples)
    inclusive time of X   ~=  n * sum(f) over X and its callees

At 1000 Hz, a function with 0.1% of runtime gets ~1 sample per second.  Never
trust a hot-spot ranking where the top entry has fewer than ~30 samples; that
is 3% of a one-second run.  :mod:`framecraft.analysis` reports the sample count
next to every percentage precisely so you can see when a number is noise.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

from framecraft._internal import codeinfo
from framecraft.analysis.stats import confidence_note
from framecraft.core import frames as framecore
from framecraft.core.events import SAMPLE, StackFrame
from framecraft.trace.base import Tracer, TracerConfig

__all__ = ["SamplingTracer", "StackKey", "stack_key"]


class StackKey(tuple):
    """Immutable ``(code_hash, ...)`` innermost-first, plus the leaf line.

    A ``tuple`` subclass so it hashes and compares by value (needed for
    aggregation) while still carrying the attributes an aggregator wants.
    """

    __slots__ = ()

    @property
    def leaf(self) -> str | None:
        return self[0] if self else None

    def __repr__(self) -> str:
        return f"<StackKey depth={len(self)} leaf={self.leaf}>"


def stack_key(frames: Any, *, with_lines: bool = True) -> StackKey:
    """Collapse a frame iterator into a hashable stack key.

    Code *hashes* rather than ``id()`` or ``file:line`` because:

    * ``id()`` is unstable across processes (defeats diffing runs),
    * ``file:line`` is unstable across edits but stable across processes --
        which is what you want for diffing, but ambiguous when a name is
        defined twice in one file (a ``def`` inside a loop, a conditional
        import, ``exec``).

    We use the hash of ``(co_name, co_filename, co_firstlineno, co_consts,
    co_names)``, which is stable across processes and unique enough in practice.
    The leaf line number is kept separately by :class:`SamplingTracer` for
    per-line attribution.
    """
    return StackKey(codeinfo.code_hash(f.f_code) for f in frames)


class SamplingTracer(Tracer):
    """Wall-clock sampling profiler driven by a daemon thread.

    Parameters
    ----------
    interval:
        Seconds between samples.  ``0.001`` (1 kHz) is the usual choice.
        Smaller values do not give more data -- ``sys._current_frames()`` cannot
        run faster than the GIL allows.
    trace_threads:
        Sample threads other than the caller's.  On by default: a program that
        spends its time in a worker pool and you sample only the main thread is
        a program that appears to be idle.
    max_depth:
        Cap on how much of each stack to record.

    Notes
    -----
    *   The sampler thread is a **daemon**.  It cannot keep the process alive.
    *   Sampling a thread while it is inside a ``sys.settrace`` hook would
        attribute the hook's own frames to your program, so if you compose a
        sampling tracer with an instrumenting one, put the sampler first in
        :func:`framecraft.trace.compose`.
    """

    #: A sampler installs no hook and takes no global slot, so it must not claim
    #: the HookLease -- see Tracer.uses_hook_slot.
    uses_hook_slot = False

    def __init__(
        self,
        config: TracerConfig | None = None,
        *,
        interval: float = 0.001,
        trace_threads: bool = True,
        max_depth: int = 64,
        skip_own_thread: bool = True,
        **overrides: Any,
    ) -> None:
        super().__init__(config, **overrides)
        if not self.config.wants(SAMPLE):
            self.config.events = (*self.config.events, SAMPLE)
        self.interval = interval
        self.trace_threads = trace_threads
        self.max_depth = max_depth
        self.skip_own_thread = skip_own_thread
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        #: ``StackKey -> count``.  The whole product: 8 bytes of key + one int
        #: per distinct stack.  Memory is proportional to *stack diversity*,
        #: not to sample count, which is why this scales to 10^8 samples.
        self.tally: dict[StackKey, int] = {}
        #: ``code_hash -> count`` for exclusive (leaf) attribution.
        self.self_tally: dict[str, int] = {}
        #: ``code_hash -> count`` for any frame appearing anywhere in a stack.
        self.inclusive_tally: dict[str, int] = {}
        #: ``(code_hash, line) -> count`` for per-line attribution.
        self.line_tally: dict[tuple[str, int], int] = {}
        #: ``code_hash -> qualname``, recorded on first sight.
        #:
        #: Sampling is the one engine whose events do not pass through
        #: ``Tracer._make_event``, so it has no natural place to intern names.
        #: Without this map a report can only show bare hashes, which is
        #: useless to a human -- and the whole point of a profile is being read.
        self.names: dict[str, str] = {}
        self.total_samples = 0
        self.deadline_misses = 0
        self._emit_samples = False
        #: ident of the sampler thread, captured in ``_start``.  Defaults to 0
        #: (matching no real thread) so sampling cannot start before it is set.
        self._sampler_tid = 0

    # ------------------------------------------------------------------
    def _sampler_loop(self) -> None:
        self._sampler_tid = threading.get_ident()
        # `time.sleep` releases the GIL and is precise to ~1ms on Linux and
        # ~15ms on Windows by default.  On Windows, `interval=0.001` therefore
        # degrades to the 15.6ms timer tick unless the process timeBeginPeriod
        # is raised.  We measure the actual cadence and report `deadline_misses`
        # so the report never claims a sampling rate you did not achieve.
        while not self._stop_evt.wait(self.interval):
            try:
                self._sample_once()
            except BaseException:
                self.error_count += 1

    def _sample_once(self) -> None:
        frames_by_thread = sys._current_frames()
        tid = threading.get_ident()
        for thread_id, top in frames_by_thread.items():
            if not self.trace_threads and thread_id != tid:
                continue
            if self.skip_own_thread and thread_id == self._sampler_tid:
                # The sampler cannot sample itself: its own frames are
                # `threading.Thread.run` -> `Event.wait` -> `_sampler_loop`,
                # which are the innermost frames at sample time and would
                # therefore claim 100% of `self` samples.  Skipping the thread
                # is correct rather than merely tidy: those samples describe
                # framecraft, not the program under measurement.
                continue
            chain: list[Any] = []
            for f in framecore.walk(top, max_depth=self.max_depth):
                # Skip our own frames: if the sampler is somehow running inside
                # traced code, do not blame framecraft for framecraft's cost.
                if f.f_code.co_filename == __file__:
                    continue
                chain.append(f)
            if not chain:
                continue
            hashes = []
            for f in chain:
                h = codeinfo.code_hash(f.f_code)
                if h not in self.names:
                    self.names[h] = codeinfo.qualified_name(f.f_code)
                hashes.append(h)
            key = StackKey(hashes)
            self.tally[key] = self.tally.get(key, 0) + 1
            leaf = chain[0]
            self.self_tally[key[0]] = self.self_tally.get(key[0], 0) + 1
            self.line_tally[(key[0], leaf.f_lineno)] = self.line_tally.get((key[0], leaf.f_lineno), 0) + 1
            for h in set(key):
                self.inclusive_tally[h] = self.inclusive_tally.get(h, 0) + 1
            self.total_samples += 1
            if self._emit_samples:
                # Off by default: emitting an event per sample is ~100x the
                # memory of a tally increment, and only useful if you are
                # building a timeline rather than a profile.
                ev = self._make_event(SAMPLE, leaf)
                ev.thread_id = thread_id
                ev.thread_name = _thread_name(thread_id)
                ev.depth = len(chain) - 1
                ev.stack = [StackFrame.from_frame(f, depth=i) for i, f in enumerate(reversed(chain))]
                # Default-arg binding is deliberate and unnecessary: `ev` is
                # rebuilt from scratch on every iteration, so the closure
                # captures the current one.  Bound anyway, so a future edit
                # that hoists the emission out of the loop cannot silently
                # emit the same event n times.
                self._guard(lambda _e=ev: self._emit(_e))

    # ------------------------------------------------------------------
    def _start(self) -> None:
        self._stop_evt.clear()
        self._sampler_tid = threading.get_ident()  # replaced by the loop on entry
        self._thread = threading.Thread(
            target=self._sampler_loop, name="framecraft-sampler", daemon=True
        )
        self._thread.start()

    def _stop(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        self._sampler_tid = 0

    # ------------------------------------------------------------------
    @property
    def effective_hz(self) -> float:
        """Samples per second actually achieved.

        ``1000`` would mean your ``interval=0.001`` was honoured.  A value like
        ``64`` means the OS timer, not you, set the rate -- and every
        percentage in the report should be read with that in mind.
        """
        dur = self.duration_ns / 1e9
        return (self.total_samples / dur) if dur > 0 else 0.0

    def name_of(self, code_hash: str) -> str:
        """Human-readable name for a code hash, or the hash if unknown."""
        return self.names.get(code_hash, code_hash)

    def report(self, limit: int = 25) -> list[dict[str, Any]]:
        """A ranked, confidence-annotated profile.

        Sorted by *self* samples, because "where does the time go" is always a
        question about exclusive cost.  ``confidence`` is
        :func:`framecraft.analysis.confidence_note` and exists so the reader
        cannot mistake a 2-sample entry for a real finding.
        """
        total = self.total_samples or 1
        rows = []
        for h, n in self.self_tally.items():
            rows.append(
                {
                    "code_hash": h,
                    "name": self.name_of(h),
                    "self_samples": n,
                    "self_pct": round(100.0 * n / total, 2),
                    "incl_samples": self.inclusive_tally.get(h, 0),
                    "incl_pct": round(100.0 * self.inclusive_tally.get(h, 0) / total, 2),
                    "confidence": confidence_note(n),
                }
            )
        rows.sort(key=lambda r: (-r["self_samples"], r["name"]))
        return rows[:limit]

    def _t_dict(self) -> dict[str, Any]:
        d = super().stats()
        d.update(
            {
                "total_samples": self.total_samples,
                "distinct_stacks": len(self.tally),
                "effective_hz": round(self.effective_hz, 2),
                "interval_ms": self.interval * 1000,
                "min_seconds_for_1000_samples": (
                    round(1000 / self.effective_hz, 3) if self.effective_hz else None
                ),
            }
        )
        return d

    stats = _t_dict

    def __repr__(self) -> str:
        return (
            f"<SamplingTracer active={self.active} samples={self.total_samples} "
            f"stacks={len(self.tally)} hz={self.effective_hz:.0f}>"
        )


_THREAD_NAMES: dict[int, str] = {}


def _thread_name(tid: int) -> str:
    name = _THREAD_NAMES.get(tid)
    if name is None:
        for t in threading.enumerate():
            if t.ident == tid:
                name = t.name
                break
        else:
            name = f"thread-{tid}"
        _THREAD_NAMES[tid] = name
    return name
