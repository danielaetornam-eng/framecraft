"""Timelines: the view you want when execution is not a single stack.

A flame graph shows *aggregate* cost.  It is useless for two questions that come
up constantly in concurrent programs:

*   "What was thread B doing while thread A was blocked on that lock?"
*   "Which of these 30 concurrent requests was the slow one, and what did its
    stack look like?"

Both need *time* on the x-axis and *threads* as rows: a Gantt chart.  That is
:class:`Timeline`.

The measurement problem, stated honestly
---------------------------------------

A tracer cannot see *blocking*.  ``sys.settrace`` fires on bytecode; a thread
blocked in ``lock.acquire()`` executes no bytecode, so it produces no events.
The gap between one event and the next is exactly the interesting part.

What a timeline can honestly show:

*   **Wall-clock span** of every call, from ``call`` to ``return``.  Built
    from ``perf_ns``, this is exact.
*   **Gaps** between spans.  A gap inside a parent span means "this frame was
    doing something without emitting events": a C call, a blocking syscall, a
    sleep, or -- very often -- another thread's GIL contention.  Labelling a
    gap "uninstrumented" rather than attributing it to the child is the
    honest thing to do, and it is what :attr:`Span.uninstrumented_ns` is for.
*   **Per-thread lanes** so overlap is visible.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from framecraft.core.events import C_CALL, CALL, EXCEPTION, RETURN, TraceEvent

__all__ = ["Span", "Timeline", "build_timeline"]


@dataclass
class Span:
    """One call's wall-clock extent on one thread."""

    call_id: int
    code_hash: str
    label: str
    thread_id: int
    thread_name: str
    start_ns: int
    end_ns: int | None = None
    depth: int = 0
    parent: Span | None = None
    children: list[Span] = field(default_factory=list)
    exception_type: str | None = None
    retval_repr: str | None = None

    @property
    def duration_ns(self) -> int:
        return max(0, (self.end_ns if self.end_ns is not None else self.start_ns) - self.start_ns)

    @property
    def closed(self) -> bool:
        return self.end_ns is not None

    def child_ns(self) -> int:
        """Total wall time accounted for by direct children."""
        return sum(c.duration_ns for c in self.children if c.closed)

    def uninstrumented_ns(self) -> int:
        """Wall time inside this span that no child span accounts for.

        Non-negative and never negative: a child can overlap its parent only if
        the trace is inconsistent (a return event we dropped, or a thread hop),
        and we clamp rather than emit nonsense.
        """
        if not self.closed:
            return 0
        return max(0, self.duration_ns - self.child_ns())

    def descendants(self) -> list[Span]:
        out: list[Span] = []
        stack = list(reversed(self.children))
        while stack:
            s = stack.pop()
            out.append(s)
            stack.extend(reversed(s.children))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "code_hash": self.code_hash,
            "label": self.label,
            "thread": self.thread_id,
            "thread_name": self.thread_name,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "duration_ns": self.duration_ns,
            "depth": self.depth,
            "parent": self.parent.call_id if self.parent else None,
            "exception_type": self.exception_type,
            "retval": self.retval_repr,
            "uninstrumented_ns": self.uninstrumented_ns() if self.closed else 0,
        }


class Timeline:
    """Per-thread lanes of :class:`Span`."""

    def __init__(self) -> None:
        self.spans: list[Span] = []
        self._by_id: dict[int, Span] = {}
        self._live: dict[int, list[Span]] = {}  # thread_id -> open spans (LIFO)
        self._t0: int | None = None
        self._t1: int | None = None

    # ------------------------------------------------------------------
    def add_call(self, ev: TraceEvent) -> Span | None:
        if ev.kind not in (CALL, C_CALL):
            return None
        parent = self._live.get(ev.thread_id, [])
        parent_span = parent[-1] if parent else None
        span = Span(
            call_id=ev.call_id if ev.call_id is not None else -abs(ev.seq),
            code_hash=ev.code_hash or "",
            label=ev.qualname or ev.name or ev.c_name or "<unknown>",
            thread_id=ev.thread_id,
            thread_name=ev.thread_name,
            start_ns=ev.perf_ns,
            depth=len(parent),
            parent=parent_span,
        )
        if parent_span is not None:
            parent_span.children.append(span)
        # setdefault, NOT `self._live.get(tid, [])`.  `dict.get` with a default
        # does not store the default, so the stack we just appended to would be
        # discarded and the matching return event would find nothing.  That
        # bug produces a timeline where every span has duration zero and every
        # overlap count is zero -- which looks like "no concurrency" rather than
        # like a broken timeline, and is therefore especially hard to notice.
        self._live.setdefault(ev.thread_id, []).append(span)
        self.spans.append(span)
        self._by_id[span.call_id] = span
        if self._t0 is None:
            self._t0 = ev.perf_ns
        return span

    def add_exit(self, ev: TraceEvent) -> Span | None:
        if ev.kind not in (RETURN, EXCEPTION):
            return None
        stack = self._live.get(ev.thread_id)
        if not stack:
            return None
        span = stack.pop()
        span.end_ns = ev.perf_ns
        if ev.kind == EXCEPTION:
            span.exception_type = ev.exception_type or "<exception>"
        span.retval_repr = ev.retval_repr
        self._t1 = ev.perf_ns
        return span

    # ------------------------------------------------------------------
    @property
    def span_ns(self) -> int:
        if self._t0 is None:
            return 0
        end = self._t1 if self._t1 is not None else self._t0
        return max(0, end - self._t0)

    def threads(self) -> list[int]:
        seen: dict[int, str] = {}
        for s in self.spans:
            seen.setdefault(s.thread_id, s.thread_name)
        return list(seen)

    def thread_name(self, tid: int) -> str:
        for s in self.spans:
            if s.thread_id == tid:
                return s.thread_name
        return str(tid)

    def overlaps(self) -> list[tuple[int, int, int, int]]:
        """Intervals where two or more different threads were inside a span.

        Returns ``(start_ns, end_ns, tid_a, tid_b)``.  This is the mechanical
        definition of concurrency in a trace: two spans on different threads
        with non-empty intersection.

        Sweep line, not O(n^2).  With a few thousand spans the quadratic version
        is the slowest code in the whole library and it is trivially avoidable:
        sort the endpoints once, then walk them maintaining the set of threads
        with a span currently open.  A window is maximal, so ``len(out)`` is
        small no matter how interleaved the execution was.
        """
        events: list[tuple[int, int, int]] = []
        for s in self.spans:
            if not s.closed or s.duration_ns == 0:
                continue
            events.append((s.start_ns, 1, s.thread_id))
            events.append((s.end_ns, -1, s.thread_id))
        if not events:
            return []
        # At a shared timestamp, close before open: two spans that touch but do
        # not overlap must not be reported as concurrent.
        events.sort(key=lambda e: (e[0], e[1]))

        active: dict[int, int] = {}
        out: list[tuple[int, int, int, int]] = []
        window_start: int | None = None
        window_threads: list[int] = []

        def close(t: int) -> None:
            nonlocal window_start, window_threads
            if window_start is not None and t > window_start and len(window_threads) >= 2:
                out.append((window_start, t, window_threads[0], window_threads[1]))
            window_start = t
            window_threads = sorted(active)

        for t, delta, tid in events:
            if t != window_start:
                close(t)
            if delta == 1:
                active[tid] = active.get(tid, 0) + 1
            else:
                remaining = active.get(tid, 1) - 1
                if remaining <= 0:
                    active.pop(tid, None)
                else:
                    active[tid] = remaining
            window_threads = sorted(active)
        close(events[-1][0])
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "t0": self._t0,
            "t1": self._t1,
            "span_ns": self.span_ns,
            "threads": [{"id": t, "name": self.thread_name(t)} for t in self.threads()],
            "spans": [s.to_dict() for s in self.spans],
        }

    def __len__(self) -> int:
        return len(self.spans)

    def __repr__(self) -> str:
        return f"<Timeline spans={len(self.spans)} threads={len(self.threads())}>"


def build_timeline(events: Iterable[TraceEvent]) -> Timeline:
    """Replay ``call``/``return`` events into a :class:`Timeline`."""
    tl = Timeline()
    for ev in events:
        if ev.kind == CALL:
            tl.add_call(ev)
        elif ev.kind in (RETURN, EXCEPTION):
            tl.add_exit(ev)
    return tl
