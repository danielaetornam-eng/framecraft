"""Tracer base class, configuration, and the scoped-installation dance.

Every tracer in ``framecraft.trace`` is a *context manager*.  That is not
cosmetic.  Tracing hooks are the closest thing Python has to a debugger, and
the single largest source of broken tracers is forgetting to uninstall:

*   a stale ``sys.settrace`` hook keeps firing after your ``with`` block ends,
    silently slowing the rest of the program and holding references to your
    buffers;
*   two tracers cannot both own ``sys.settrace`` -- the second silently wins and
    the first keeps its per-frame ``f_trace`` values, so you get a *mixture* of
    both tracers, which is the worst possible outcome because it looks
    plausible.

:class:`Tracer` therefore implements ``__enter__``/``__exit__`` with strict
save/restore of the previous hook, and :class:`HookLease` documents the
ownership rules for shared hook state.
"""

from __future__ import annotations

import itertools
import threading
import time
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from framecraft._internal import codeinfo
from framecraft._internal.values import DEFAULT_POLICY, DEFAULT_REDACTOR, CapturePolicy, Redactor
from framecraft.core import frames as framecore
from framecraft.core.events import (
    CALL,
    LINE,
    RETURN,
    TraceEvent,
)

__all__ = ["TracerConfig", "Tracer", "HookLease", "current_task_name"]

_SEQ = itertools.count(1)


class _ThreadInfo(threading.local):
    """Per-thread identity, computed once per thread rather than per event."""

    def __init__(self) -> None:
        self.tid = threading.get_ident()
        self.name = threading.current_thread().name


_THREAD_INFO = _ThreadInfo()


@dataclass
class TracerConfig:
    """Everything a tracer might want, in one place.

    Having a single config object (rather than 14 kwargs) is what makes the
    tracer matrix in the docs checkable: every tracer reads the same fields, and
    a field a tracer ignores is a bug in either the tracer or the config.
    """

    # --- what to observe ---------------------------------------------------
    events: tuple[str, ...] = (CALL, RETURN, LINE)
    #: Per-code inclusion filter: return True to trace this code object.
    include: Callable[[Any], bool] | None = None
    #: Per-code exclusion filter.  Applied *after* ``include``.
    exclude: Callable[[Any], bool] | None = None
    #: Only trace these files/packages.  Convenience over ``exclude``.
    include_paths: tuple[str, ...] = ()
    #: Never trace these.  Applied last, wins over everything.
    exclude_paths: tuple[str, ...] = ()

    # --- what to capture ---------------------------------------------------
    capture_locals: bool = False
    capture_stack: bool = False
    capture_stack_depth: int = 8
    capture_args: bool = True
    capture_return: bool = True
    capture_source: bool = False
    policy: CapturePolicy = field(default_factory=lambda: DEFAULT_POLICY)
    redactor: Redactor = field(default_factory=lambda: DEFAULT_REDACTOR)

    # --- bookkeeping -------------------------------------------------------
    #: Keep :attr:`Tracer.events` in memory.  ``False`` streams straight to sinks.
    buffer: bool = True
    #: Hard cap on buffered events; oldest are dropped (and counted) past this.
    max_events: int = 500_000
    #: Extra per-event callable.  Return False to drop the event.  This is the
    #: extension point for "only trace while a flag is set".
    filter: Callable[[TraceEvent], bool] | None = None
    #: Called instead of raising when a hook itself explodes.  Receives the
    #: exception.  If unset, tracer-internal errors are swallowed and counted
    #: (never propagated -- see the safety note in ``Tracer._dispatch``).
    on_error: Callable[[BaseException], None] | None = None
    #: Install on newly spawned threads too (``threading.settrace``).
    include_threads: bool = False
    #: Install an ``asyncio`` task factory that records task creation sites.
    trace_asyncio_tasks: bool = False
    #: Read ``time.process_time()`` per event.  Off by default because it costs
    #: 684 ns on Windows -- more than every other clock read combined -- and it
    #: only distinguishes "burned CPU" from "waited", which under the GIL rarely
    #: changes a conclusion.  Turn it on for I/O-bound threads.
    capture_cpu: bool = False
    #: Read any clock at all.  Set False for maximum-throughput tracing (counting
    #: only); every ``perf_ns`` becomes 0 and all interval maths is meaningless.
    timing: bool = True

    def wants(self, kind: str) -> bool:
        return kind in self.events

    def admits(self, code: Any) -> bool:
        """Should this code object be instrumented?

        Order matters and is deliberate:

        1. ``exclude_paths`` first.  A path you never want traced must be
           rejected before we pay for ``include_paths`` prefix matching.
        2. ``include_paths``: if non-empty, the file *must* match.  This is the
           "only trace my package" mode and it is by far the cheapest useful
           filter, because it stops instrumentation at frame entry rather than
           filtering events afterwards.
        3. ``exclude`` then ``include``: user callbacks, in that order so that
           ``include`` acts as the final narrowing step.

        **Both the filename and the patterns are normalised to forward slashes.**
        Normalising only the filename looks correct and is silently broken on
        Windows, where ``co_filename`` contains backslashes: ``C:\\src\\app.py``
        never starts with ``C:/src``, so *every* file is rejected and
        ``--include`` traces nothing while reporting success.  That is the worst
        possible failure for a filter -- it looks like the program has no
        interesting code.
        """
        filename = code.co_filename or ""
        norm = filename.replace("\\", "/")
        for pat in self._normalised_exclude_paths:
            if norm.startswith(pat) or _basename_matches(norm, pat):
                return False
        if self.include_paths:
            if not any(
                norm.startswith(p) or _basename_matches(norm, p)
                for p in self._normalised_include_paths
            ):
                return False
        if self.exclude is not None and self.exclude(code):
            return False
        if self.include is not None and not self.include(code):
            return False
        return True

    @property
    def _normalised_include_paths(self) -> tuple:
        return tuple(p.replace("\\", "/") for p in self.include_paths)

    @property
    def _normalised_exclude_paths(self) -> tuple:
        return tuple(p.replace("\\", "/") for p in self.exclude_paths)


def _basename_matches(norm_filename: str, pattern: str) -> bool:
    p = pattern.replace("\\", "/")
    if p.endswith("/") or p.endswith("*"):
        return norm_filename.startswith(p.rstrip("*"))
    return norm_filename.rsplit("/", 1)[-1] == p


def current_task_name() -> str | None:
    """Best-effort name of the running asyncio task, or ``None``.

    ``asyncio.current_task()`` returns ``None`` outside a task, and it *raises*
    when there is no running loop.  Tracers call this on every event, so it
    must be cheap and must never raise -- hence the triple guard.  The result is
    cached per-thread between changes because ``asyncio.current_task`` walks
    ``asyncio.current_task`` internals.
    """
    try:
        import asyncio

        task = asyncio.current_task()
    except BaseException:
        return None
    if task is None:
        return None
    name = getattr(task, "get_name", None)
    if name is None:
        return None
    try:
        return name()
    except BaseException:
        return None


class HookLease:
    """Ownership token for the process-global ``sys.settrace``/``setprofile``.

    ``sys.settrace`` is a *single global slot* (per thread).  Two tracers that
    both install a hook will clobber each other.  This class makes the clobber
    explicit: :meth:`claim` raises unless you are the current owner, so a
    nested/overlapping tracer fails loudly at install time rather than
    producing a trace that is half yours and half someone else's.
    """

    _owner: Any = None
    _lock = threading.Lock()

    @classmethod
    def claim(cls, tracer: Any) -> None:
        with cls._lock:
            if cls._owner is not None and cls._owner is not tracer:
                raise RuntimeError(
                    f"tracing hook already owned by {cls._owner!r}; "
                    "sys.settrace/setprofile are single global slots. "
                    "Use framecraft.trace.compose() to stack tracers, or nest "
                    "the `with` blocks instead of overlapping them."
                )
            cls._owner = tracer

    @classmethod
    def release(cls, tracer: Any) -> None:
        with cls._lock:
            if cls._owner is tracer:
                cls._owner = None

    @classmethod
    def current(cls) -> Any:
        return cls._owner


class Tracer(ABC):
    """Base class: buffering, filtering, draining, and safe hook invocation.

    Deliberately **not** using ``@abstractmethod`` on ``_start``/``_stop``.  The
    abstract methods are private, and making them abstract would prevent
    ``Tracer`` from being used directly in tests that only exercise buffering and
    filtering -- which is most of it.  A subclass that forgets to implement one
    gets a ``NotImplementedError`` naming itself, at ``start()``, which is a
    better failure than an import-time ``TypeError``.

    Subclasses implement :meth:`_start` and :meth:`_stop`.  The base class owns
    the three things that are easy to get wrong in every hand-rolled tracer:

    *   **Never raise from a hook.**  An exception escaping a trace callback
        propagates into the interpreter's eval loop and kills the traced
        program with a message that does not mention your tracer.  Every hook in
        this library funnels through :meth:`_guard`, which counts the failure,
        optionally notifies ``config.on_error``, and returns.  A tracer that
        silently dies halfway is the worst kind of tracer, so we count, and
        :attr:`error_count` is part of every report.
    *   **Never retain frames.**  See :meth:`_event`.
    *   **Never grow without bound.**  :attr:`max_events` drops oldest and keeps
        :attr:`dropped` so the report can say "0.3% of events were dropped"
        rather than quietly lying about completeness.
    """

    def __init__(self, config: TracerConfig | None = None, **overrides: Any) -> None:
        self.config = config or TracerConfig()
        for k, v in overrides.items():
            if not hasattr(self.config, k):
                raise TypeError(f"unknown TracerConfig field {k!r}")
            setattr(self.config, k, v)
        self.events: list[TraceEvent] = []
        self.dropped: int = 0
        self.error_count: int = 0
        self.active: bool = False
        self._sinks: list[Any] = []
        self._prev_trace: Any = None
        self._prev_profile: Any = None
        self._prev_thread_trace: Any = None
        self._prev_task_factory: Any = None
        self._started_ns: int = 0
        self._stopped_ns: int = 0

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def add_sink(self, sink: Any) -> Tracer:
        """Register a sink.  Sinks receive every event as it is emitted.

        Sinks are called *after* the buffer, and a sink that raises is isolated
        the same way hooks are.
        """
        self._sinks.append(sink)
        return self

    def __enter__(self) -> Tracer:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    #: Does this tracer occupy one of the process-global hook slots
    #: (`sys.settrace` / `sys.setprofile`)?  Overridden to `False` by
    #: :class:~framecraft.trace.SamplingTracer, which installs nothing.
    #:
    #: This matters because :class:HookLease is a *global* lock: a sampler
    #: that claims it blocks every real tracer in the process while holding no
    #: resource at all.  Composing a sampler with a settrace tracer is the
    #: recommended way to measure honestly, and it must not fail.
    uses_hook_slot: bool = True

    def start(self) -> Tracer:
        if self.active:
            return self
        if self.uses_hook_slot:
            HookLease.claim(self)
        self._prev_trace = __import__("sys").gettrace()
        self._prev_profile = __import__("sys").getprofile()
        self._started_ns = time.perf_counter_ns()
        try:
            self._start()
            self.active = True
        except BaseException:
            if self.uses_hook_slot:
                HookLease.release(self)
            raise
        return self

    def stop(self) -> None:
        if not self.active:
            return
        try:
            self._stop()
        finally:
            self.active = False
            self._stopped_ns = time.perf_counter_ns()
            if self.uses_hook_slot:
                HookLease.release(self)
            self._restore_hooks()

    def _start(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError(f"{type(self).__name__} must implement _start()")

    def _stop(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError(f"{type(self).__name__} must implement _stop()")

    def _restore_hooks(self) -> None:
        """Put the interpreter back exactly the way we found it.

        Note we restore even if it was ``None``.  Leaving our hook installed
        after the ``with`` block is the classic "my program got 30x slower after
        I imported my profiler" bug.
        """
        import sys

        sys.settrace(self._prev_trace)
        sys.setprofile(self._prev_profile)
        if self._prev_thread_trace is not None or self.config.include_threads:
            import threading

            threading.settrace(self._prev_thread_trace)
        if self.config.trace_asyncio_tasks and self._prev_task_factory is not None:
            import asyncio

            try:
                asyncio.get_running_loop().set_task_factory(self._prev_task_factory)
            except BaseException:
                pass

    # ------------------------------------------------------------------
    # internals subclasses use
    # ------------------------------------------------------------------
    def _next_seq(self) -> int:
        return next(_SEQ)

    def _note_error(self, exc: BaseException) -> None:
        """Record a failure that happened *inside* a hook.

        Split out of :meth:`_guard` so that hot callbacks can inline their own
        ``try/except`` and call this directly.  The rule is non-negotiable: an
        exception escaping a hook propagates into the traced program and
        unsets the global hook (measured in ``examples/02_raw_settrace.py``), so
        every hook body in this library is wrapped.  A tracer that silently dies
        halfway through is worse than one that crashes, because a crash is
        visible and a dead tracer looks like a program that does nothing.
        """
        self.error_count += 1
        on_error = self.config.on_error
        if on_error is not None:
            try:
                on_error(exc)
            except BaseException:
                pass
        # Debug escape hatch: set FRAMECRAFT_DEBUG=1 to see tracer bugs instead
        # of hiding them.  Hidden by default on purpose -- see above.
        import os

        if os.environ.get("FRAMECRAFT_DEBUG"):
            import traceback

            traceback.print_exception(exc)

    def _guard(self, fn: Callable[[], Any]) -> None:
        """Run ``fn``, swallowing (and counting) any exception it raises."""
        try:
            fn()
        except BaseException as exc:
            self._note_error(exc)

    def _emit(self, event: TraceEvent) -> bool:
        """Route an event to the filter, the buffer, and the sinks.

        The order is deliberate: filter first (cheapest rejection before we pay
        for capture — although capture has already happened by now, which is why
        :attr:`TracerConfig.filter` is documented as a *late* filter, not a way
        to make tracing free), then buffer, then sinks.
        """
        if self.config.filter is not None:
            try:
                if not self.config.filter(event):
                    return False
            except BaseException as exc:
                self.error_count += 1
                if self.config.on_error:
                    self.config.on_error(exc)
                return False
        if self.config.buffer:
            if len(self.events) >= self.config.max_events:
                # Drop oldest in O(n) chunks rather than pop(0) per event:
                # list.pop(0) is O(n) and would make the drop path dominate.
                del self.events[: len(self.events) // 10]
                self.dropped += 1
            self.events.append(event)
        for sink in self._sinks:
            self._guard(lambda s=sink: s.write(event))
        return True

    def _make_event(
        self,
        kind: str,
        frame: FrameType | None = None,
        *,
        code: Any = None,
        offset: int | None = None,
        lineno: int | None = None,
        call_id: int | None = None,
        parent_call_id: int | None = None,
        depth: int | None = None,
        **payload: Any,
    ) -> TraceEvent:
        """Assemble a :class:`TraceEvent`, honouring the capture policy.

        Every tracer funnels event construction through here so that the
        "capture locals / stack / args" semantics are identical no matter which
        hook produced the event.  Divergent capture behaviour between tracers is
        a nightmare to debug because the traces *look* the same.
        """
        code = code if code is not None else (frame.f_code if frame is not None else None)

        # Every microsecond here is paid once per emitted event, so each piece
        # is the cheapest correct version of itself:
        #   * one clock read (perf_counter_ns); `wall` is derived from it
        #   * process_time only when the caller asked for it
        #   * thread id/name cached per thread, not per event
        # See framecraft.core.frames.clocks for the measurements.
        if self.config.timing:
            perf_ns = time.perf_counter_ns()
            wall = perf_ns / 1e9  # same clock, lower resolution, no extra call
            cpu = time.process_time() if self.config.capture_cpu else 0.0
        else:
            wall = cpu = perf_ns = 0.0
        info = _THREAD_INFO
        ev = TraceEvent(
            seq=self._next_seq(),
            kind=kind,
            wall=wall,
            cpu=cpu,
            perf_ns=perf_ns,
            thread_id=info.tid,
            thread_name=info.name,
            **payload,
        )
        if code is not None:
            ev.code_id = codeinfo.code_id(code)
            ev.code_hash = codeinfo.code_hash(code)
            ev.name = code.co_name
            ev.qualname = codeinfo.qualified_name(code)
            ev.filename = code.co_filename
        if frame is not None:
            if offset is None:
                offset = frame.f_lasti
            if lineno is None:
                lineno = frame.f_lineno
        ev.offset = offset
        ev.lineno = lineno
        ev.call_id = call_id
        ev.parent_call_id = parent_call_id
        if depth is not None:
            ev.depth = depth
        elif frame is not None:
            ev.depth = sum(1 for _ in framecore.walk(frame))

        if self.config.capture_locals and frame is not None:
            ev.locals = framecore.capture_locals(
                frame, policy=self.config.policy, redactor=self.config.redactor
            )
        if self.config.capture_stack and frame is not None:
            ev.stack = framecore.stack_snapshot(
                include_locals=False,
                max_frames=self.config.capture_stack_depth,
            )
        if self.config.trace_asyncio_tasks:
            ev.task = current_task_name()
        return ev

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    @property
    def duration_ns(self) -> int:
        end = self._stopped_ns or framecore.clocks()["perf_ns"]
        return max(0, end - self._started_ns)

    def drain(self) -> list[TraceEvent]:
        """Return and clear the buffer.

        Use this if you enabled sinks *and* buffering and want the buffer gone.
        A profiler that keeps a quarter-million events alive after the run is a
        memory leak with a debugger's licence.
        """
        out = self.events
        self.events = []
        return out

    def stats(self) -> dict[str, Any]:
        from framecraft.analysis import summarise_events

        return summarise_events(self.events, dropped=self.dropped, errors=self.error_count)

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} active={self.active} events={len(self.events)} "
            f"dropped={self.dropped} errors={self.error_count}>"
        )
