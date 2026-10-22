"""PEP 669 (``sys.monitoring``): the tracing API that was designed, not grown.

Everything in :mod:`framecraft.trace.function_tracer` works by accident --
``sys.settrace`` grew a line-event callback in 1999 and has carried the same
rough edges ever since.  PEP 669 replaces it with an explicit registry:

*   A **tool id** (an integer in ``2..5``; ``0`` is reserved for ``pdb`` and
    ``1`` for ``coverage.py``).  You claim one with
    ``sys.monitoring.use_tool_id``.
*   **Named events**, not a stringly-typed ``'call'``/``'line'``/``'return'``
    triad: ``PY_START``, ``PY_RETURN``, ``PY_YIELD``, ``PY_UNWIND``,
    ``LINE``, ``CALL``, ``C_CALL``, ``C_RETURN``, ``C_RAISE``,
    ``C_FUNCTION``, ``C_FUNCTION_RETURN``, ``EXCEPTION``, ``BRANCH``, ``GC``,
    ``CODE_OBJECT``.
*   **Per-code-object selection** via ``set_local_events(code, events)``, so you
    can say "line events in *this* function" without a Python-level filter.
*   **Fast paths.**  When no tool is listening, the interpreter's check is a
    single integer compare against a table.  This is why ``sys.settrace`` has a
    100x tax and PEP 669 has roughly 2-10x.

Three things PEP 669 gives you that ``sys.settrace`` cannot
-----------------------------------------------------------

1.  **Arguments and return values for free.**  ``PY_RETURN`` gets the return
    value; ``CALL``/``C_CALL`` get the callable *and* ``arg0``.  With
    ``settrace`` you must reconstruct the signature from ``co_varnames`` and
    ``f_locals``, and you can never see a C call's arguments at all.

2.  **Exception unwinding as a first-class event.**  ``PY_UNWIND`` fires with
    the in-flight exception *before* the frame is destroyed, which is exactly
    what you want for "log the locals at the moment the stack was torn down".
    ``settrace`` gives you ``'exception'`` but the locals are already in flux.

3.  **Coexistence.**  ``coverage.py``, ``pdb`` and your tool can all be active
    at once, on different tool ids.  ``sys.settrace`` has one slot, full stop.

The gotchas, all of which will bite you
---------------------------------------

*   **Tool ids are global and finite.**  Two ``MonitorTracer`` instances in one
    process cannot both use id 2.  :class:`MonitorTracer` allocates the lowest
    free id and releases it on ``stop()``.
*   **You must ``free_tool_id`` on exit** or every subsequent ``use_tool_id(2, ...)``
    in the process fails.
*   ``set_events`` replaces (it is not additive across calls) but multiple
    callbacks can be registered for the *same* event, and they run in
    registration order.
*   ``BRANCH`` events are the raw material for **coverage**; they are only
    emitted for jump instructions, not for straight-line code.
*   Local events require the code object to be *instrumentable*; you cannot set
    local events for code objects you cannot get a reference to.
*   Callbacks must not raise.  An exception here is fatal to the traced program
    and is reported as ``sys.monitoring`` internal error, not as your bug.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import Any

from framecraft._internal import codeinfo
from framecraft._internal.compat import (
    DISABLE_EVENTS,
    HAS_MONITORING,
    free_tool_ids,
    monitoring_event,
)
from framecraft._internal.values import safe_repr
from framecraft.core import frames as framecore
from framecraft.core.events import (
    C_CALL,
    C_EXCEPTION,
    C_RETURN,
    CALL,
    CALL_SITE,
    EXCEPTION,
    LINE,
    OPCODE,
    RAISE_SITE,
    RETURN,
    YIELD,
)
from framecraft.trace.base import Tracer, TracerConfig
from framecraft.trace.function_tracer import _opcode_index

__all__ = ["MonitorTracer", "HAS_MONITORING", "EVENT_MAP"]


if HAS_MONITORING:
    #: framecraft kind -> PEP 669 event names that can produce it.
    #:
    #: The lists are ordered "best first", and several are *alternatives*
    #: because the C-call family was restructured twice:
    #:
    #: *   3.12/3.13 had ``C_CALL`` (builtins and methods), ``C_FUNCTION``
    #:     (C functions only), ``C_FUNCTION_RETURN`` and ``C_RETURN``.
    #: *   3.14 **removed** ``C_CALL``/``C_FUNCTION``/``C_FUNCTION_RETURN`` and
    #:     replaced them with ``INSTRUCTION`` (PEP 749), which fires for every
    #:     bytecode *and* every C call.
    #:
    #: :meth:`MonitorTracer._start` registers whichever names this interpreter
    #: actually has, so the same config string produces the same framecraft
    #: event kinds on 3.12 and 3.14.
    EVENT_MAP: dict[str, tuple[str, ...]] = {
        CALL: ("PY_START",),
        RETURN: ("PY_RETURN",),
        LINE: ("LINE",),
        OPCODE: ("INSTRUCTION",),
        YIELD: ("PY_YIELD",),
        # NOTE: `EXCEPTION` does not exist on CPython 3.14 (it was removed
        # alongside C_CALL/C_FUNCTION in the PEP 749 rework).  The surviving
        # exception-family events are RAISE, PY_UNWIND, PY_THROW, RERAISE and
        # EXCEPTION_HANDLED.  RAISE is included because it is the only event
        # that fires for an exception which is raised *and handled inside the
        # same frame* -- PY_UNWIND does not, because the frame is not torn
        # down.  Without RAISE, `try: raise X except X: pass` is completely
        # invisible to a PEP 669 tracer on 3.14.
        EXCEPTION: ("EXCEPTION", "RAISE", "PY_UNWIND"),
        C_CALL: ("C_CALL", "C_FUNCTION"),
        C_RETURN: ("C_RETURN", "C_FUNCTION_RETURN"),
        C_EXCEPTION: ("C_RAISE",),
        # PEP 669 has no `sys.settrace`/`sys.setprofile` equivalent for these
        # two, which is exactly why they get their own kinds.
        CALL_SITE: ("CALL",),
        RAISE_SITE: ("RAISE",),
    }
else:  # pragma: no cover - 3.11 and older
    EVENT_MAP = {}


class MonitorTracer(Tracer):
    """A PEP 669 tracer.

    Examples
    --------
    ::

        with MonitorTracer(events=("call", "line", "return")) as t:
            work()
        print(len(t.events))

    On Python < 3.12 the constructor raises :class:`RuntimeError` with an
    explanation and a pointer to :class:`FunctionTracer`, rather than failing
    mysteriously later.
    """

    def __init__(
        self,
        config: TracerConfig | None = None,
        *,
        tool_id: int | None = None,
        tool_name: str = "framecraft",
        **overrides: Any,
    ) -> None:
        if not HAS_MONITORING:
            raise RuntimeError(
                "sys.monitoring requires Python 3.12+. On this interpreter use "
                "framecraft.trace.FunctionTracer, which implements the same "
                "call/line/return vocabulary on top of sys.settrace."
            )
        super().__init__(config, **overrides)
        self.tool_id: int = -1
        self.tool_name = tool_name
        self._tool_id_request = tool_id
        self._registered: dict[str, Callable[..., Any]] = {}
        self._local_event_codes: set[Any] = set()
        #: code object -> should we report it?  See :meth:`_admits`.
        self._admits_cache: dict[Any, bool] = {}
        #: Human-readable statement of what ``TraceEvent.depth`` means for
        #: this tracer.  Reports print it rather than letting the reader
        #: assume "distance from the bottom of the stack".
        self.depth_semantics = "admitted frames live beneath this one on this thread"
        #: Per-thread depth counter.  PEP 669 callbacks do not receive a
        #: frame, and walking f_back per event is O(depth) -- measured at
        #: roughly 2x the total cost of a settrace line tracer, because the
        #: walk happens in Python on the hot path.  A counter is O(1), and
        #: :meth:`_locate` is only called when a caller genuinely needs the
        #: live frame (capture_locals / capture_stack / call arguments).
        self._tls = threading.local()
        #: OR of every event mask this config asked for.  Computed in _start.
        self._global_mask: int = 0
        #: Event names this interpreter refused to enable, with the interpreter's
        #: own complaint in ``_last_set_events_error``.  Never silently empty.
        self.unavailable_events: set = set()
        self._last_set_events_error: str = ""
        self._task_origins: dict[Any, dict[str, Any]] = {}
        self._task_factory: Callable[..., Any] | None = None

    # ------------------------------------------------------------------
    # tool id allocation
    # ------------------------------------------------------------------
    @staticmethod
    def _free_tool_ids() -> list[int]:
        """Delegate to :func:`framecraft._internal.compat.free_tool_ids`.

        On 3.12/3.13 the answer is ``[2, 3, 4, 5]``.  On 3.14 it is ``[3, 4]``,
        because ``PROFILER_ID`` and ``OPTIMIZER_ID`` claimed 2 and 5.  Hard-coding
        ``range(2, 6)`` works on one of those and raises ``ValueError: tool 2 is
        already in use`` on the other -- a portability bug you would only ever
        find in production.
        """
        return free_tool_ids()

    def _acquire_tool_id(self) -> int:
        m = sys.monitoring
        if self._tool_id_request is not None:
            tid = self._tool_id_request
            existing = m.get_tool(tid)
            if existing is not None and existing != self.tool_name:
                raise RuntimeError(
                    f"monitoring tool id {tid} is already in use by {existing!r}; "
                    f"free ids are {self._free_tool_ids()}"
                )
            if existing is None:
                m.use_tool_id(tid, self.tool_name)
            return tid
        free = self._free_tool_ids()
        if not free:
            raise RuntimeError(
                "no free sys.monitoring tool id (only 2..5 are available and "
                "all are claimed). Finish and free another tool first."
            )
        tid = free[0]
        m.use_tool_id(tid, self.tool_name)
        return tid

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def _start(self) -> None:
        m = sys.monitoring
        self.tool_id = self._acquire_tool_id()

        # 1. Register a callback for EVERY event this interpreter has a handler
        #    for, whether or not the caller asked for it.  Registration is
        #    cheap and, crucially, it is what makes `watch_code` work: local
        #    events can only be enabled for events that have a callback.  The
        #    event *mask* is the control, not registration.
        registered_any = False
        for name, fn in self._handler_table().items():
            if monitoring_event(name) is None:
                continue
            try:
                m.register_callback(self.tool_id, monitoring_event(name), fn)
            except ValueError:
                # "The callback can only be set for one event at a time" --
                # this name is already claimed by an earlier run of this
                # tracer.  Losing one event beats failing to start.
                continue
            self._registered[name] = fn
            registered_any = True

        if not registered_any:  # pragma: no cover - means an alien interpreter
            self.stop()
            raise RuntimeError("no usable sys.monitoring events on this interpreter")

        # 2. Compute the global mask from what the caller actually asked for.
        self._global_mask = 0
        for kind in self.config.events:
            for name in EVENT_MAP.get(kind, ()):
                v = monitoring_event(name)
                if v is not None:
                    self._global_mask |= v

        # 3. An empty event set is legitimate: it means "globally silent, arm me
        #    per code object later with watch_code()".  Only complain if the
        #    caller asked for something and the interpreter cannot do it.
        if self._global_mask and self.config.events:
            supported = {
                n for k in self.config.events for n in EVENT_MAP.get(k, ()) if monitoring_event(n) is not None
            }
            if not supported:
                requested = sorted({n for k in self.config.events for n in EVENT_MAP.get(k, ())})
                self.stop()
                raise RuntimeError(
                    f"none of the requested PEP 669 events are supported on Python "
                    f"{sys.version_info.major}.{sys.version_info.minor}: {requested}"
                )

        self.depth_is_exact = self._needs_frame()
        self._set_events_with_fallback(self._global_mask)
        if self.config.trace_asyncio_tasks:
            self._install_task_factory()

    def _handler_table(self) -> dict:
        """``{pep669_event_name: callback}`` for everything we can handle."""
        out: dict = {}
        for name in self._ALL_EVENTS:
            cb = self._callback_for(name, "")
            if cb is not None and monitoring_event(name) is not None:
                out[name] = cb
        return out

    #: Every PEP 669 event framecraft has a handler for.  Probe each one for
    #: existence at start time; the ones that vanished (C_CALL and friends in
    #: 3.14) are simply skipped.
    _ALL_EVENTS = (
        "PY_START",
        "PY_RETURN",
        "PY_YIELD",
        "LINE",
        "INSTRUCTION",
        "CALL",
        "C_CALL",
        "C_FUNCTION",
        "C_RETURN",
        "C_FUNCTION_RETURN",
        "C_RAISE",
        "EXCEPTION",
        "EXCEPTION_HANDLED",
        "PY_UNWIND",
        "PY_THROW",
        "RAISE",
    )

    def _set_events_with_fallback(self, mask: int) -> int:
        """Enable as much of ``mask`` as this interpreter will accept.

        ``sys.monitoring.set_events`` validates the mask and raises
        ``ValueError`` for combinations it considers inconsistent.  One of those
        validations is wrong on CPython 3.14.6:

        .. code-block:: python

            import sys
            m, e = sys.monitoring, sys.monitoring.events
            m.use_tool_id(4, "p")
            m.register_callback(4, e.C_RETURN, lambda *a: None)
            m.register_callback(4, e.C_RAISE,  lambda *a: None)
            m.set_events(4, e.C_RETURN | e.C_RAISE)
            # ValueError: cannot set C_RETURN or C_RAISE events independently
            #
            # ...but adding an unrelated CALL registration makes it pass:
            m.register_callback(4, e.C_CALL, lambda *a: None)   # C_CALL is gone in 3.14
            # (registering the *CALL* event is what actually fixes it)

        A tracer that treats that as fatal is unusable on the affected
        versions.  So we degrade instead: drop the offending group, retry, and
        record exactly what was lost in :attr:`unavailable_events` so the report
        can say so instead of quietly producing a thinner trace than you asked
        for.

        Returns the mask that was actually enabled.
        """
        m = sys.monitoring
        try:
            m.set_events(self.tool_id, mask)
            return mask
        except ValueError as exc:
            self._last_set_events_error = str(exc)

        c_ret, c_raise = monitoring_event("C_RETURN"), monitoring_event("C_RAISE")
        if c_ret and (mask & c_ret or mask & c_raise):
            mask = mask & ~(c_ret | c_raise)
            for name in ("C_RETURN", "C_FUNCTION_RETURN", "C_RAISE"):
                self.unavailable_events.add(name)
            try:
                m.set_events(self.tool_id, mask)
                return mask
            except ValueError:
                pass

        # Generic fallback: give up events one at a time (highest-value first,
        # i.e. the order they were registered) until the interpreter accepts
        # what is left.
        remaining = list(self._registered)
        while remaining and mask:
            dropped = remaining.pop()
            v = monitoring_event(dropped)
            if v:
                mask &= ~v
            try:
                m.set_events(self.tool_id, mask)
            except ValueError:
                continue
            self.unavailable_events.add(dropped)
        return mask

    @staticmethod
    def _mask_of(names) -> int:
        """OR of the masks for the given event names, skipping absent ones."""
        out = 0
        for n in names:
            v = monitoring_event(n)
            if v:
                out |= v
        return out

    def _stop(self) -> None:
        if self.tool_id < 0:
            return
        m = sys.monitoring
        m.set_events(self.tool_id, DISABLE_EVENTS)
        for name in self._registered:
            mask = monitoring_event(name)
            if mask is None:
                continue
            try:
                m.register_callback(self.tool_id, mask, None)
            except ValueError:  # pragma: no cover
                pass
        for code in self._local_event_codes:
            try:
                m.set_local_events(self.tool_id, code, DISABLE_EVENTS)
            except (ValueError, TypeError):  # pragma: no cover
                pass
        self._local_event_codes.clear()
        self._registered.clear()
        self._task_origins.clear()
        self._tls.depth = 0
        self._admits_cache.clear()
        self.depth_semantics = ""
        try:
            m.free_tool_id(self.tool_id)
        except (ValueError, RuntimeError):  # pragma: no cover
            pass
        self.tool_id = -1

    def _wanted_event_names(self) -> list[str]:
        out: list[str] = []
        for kind in self.config.events:
            out.extend(EVENT_MAP.get(kind, ()))
        return out

    # ------------------------------------------------------------------
    # per-code-object events (the feature settrace does not have)
    # ------------------------------------------------------------------
    def watch_code(self, code: Any, kinds: tuple[str, ...] = ("line",)) -> None:
        """Enable ``kinds`` for exactly one code object, regardless of filters.

        This is PEP 669's killer feature and it has no ``sys.settrace``
        equivalent.  ``TracerConfig.admits`` is a *Python* predicate consulted at
        frame entry; ``watch_code`` writes into the interpreter's own dispatch
        table, so it costs nothing at event time.

        Typical use: trace ``mypkg.hotspots.compute`` in detail while leaving the
        other 4000 functions in the process completely untouched::

            with MonitorTracer(events=()) as t:
                t.watch_code(some_function.__code__, ("line", "call", "return"))
                compute()

        Local events survive ``set_events(tool_id, 0)``, so a tracer can be
        globally quiet and still instrument individual functions.
        """
        if self.tool_id < 0:
            raise RuntimeError("watch_code() requires an active tracer")
        m = sys.monitoring
        mask = 0
        for k in kinds:
            for name in EVENT_MAP.get(k, ()):
                v = monitoring_event(name)
                if v is None or name not in self._registered:
                    continue
                mask |= v
        if mask:
            m.set_local_events(self.tool_id, code, mask)
            self._local_event_codes.add(code)

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------
    def _callback_for(self, name: str, kind: str) -> Callable[..., Any] | None:
        """Build the ``*args`` callback for PEP 669 event ``name``.

        Every callback is declared ``*args`` on purpose.  The arity of these
        callbacks is **not stable across CPython versions**, even within 3.14:

        ============  ==================================  ====================
        event         documented signature               3.14.6 observed
        ============  ==================================  ====================
        ``PY_START``  ``(code, offset)``                 2 args
        ``PY_RETURN`` ``(code, offset, retval)``         3 args
        ``LINE``      ``(code, lineno)``                 2 args
        ``CALL``      ``(code, offset, callable, arg0)`` 4 args
        ``C_RETURN``  ``(code, offset, callable, arg0,   4 args -- **no
                      retval)``                          retval**
        ``INSTRUCTION`` ``(code, offset, opname, arg,   2 args -- **no opname**
                           argrepr)``
        ============  ==================================  ====================

        Declaring fixed parameters means a patch release can turn your tracer
        into a ``TypeError`` storm that you cannot fix.  Declaring ``*args`` and
        unpacking by position with a length check means you degrade to "I did
        not get the return value" instead.  See :meth:`_payload` for the
        unpacking.
        """
        handlers = {
            "PY_START": self._h_py_start,
            "PY_RETURN": self._h_py_return,
            "PY_YIELD": self._h_py_yield,
            "LINE": self._h_line,
            "INSTRUCTION": self._h_instruction,
            "CALL": self._h_call,
            "C_CALL": self._h_c_call,
            "C_FUNCTION": self._h_c_call,
            "C_RETURN": self._h_c_return,
            "C_FUNCTION_RETURN": self._h_c_return,
            "C_RAISE": self._h_c_raise,
            "EXCEPTION": self._h_exception,
            "EXCEPTION_HANDLED": self._h_exception,
            "PY_UNWIND": self._h_unwind,
            "RAISE": self._h_raise,
            "PY_THROW": self._h_unwind,
        }
        fn = handlers.get(name)
        if fn is None:
            return None

        # One closure per event, not two.  The obvious spelling is
        #     lambda *a: self._guard(lambda: fn(*a))
        # which costs an extra Python frame per event.  PEP 669 fires LINE once
        # per executed line, so at a few million events that frame is
        # measurable; the try/except below is functionally identical to
        # `_guard` and removes a whole call level.
        tracer = self

        def callback(*args: Any) -> None:
            try:
                fn(*args)
            except BaseException as exc:
                tracer._note_error(exc)

        return callback

    @staticmethod
    def _payload(args: tuple, n: int) -> tuple:
        """Return the first ``n`` positional arguments, padding with ``None``.

        This is the whole reason the callbacks are ``*args``: a version that
        drops ``retval`` from ``C_RETURN`` degrades to ``retval=None`` rather
        than raising ``TypeError`` inside a hook.
        """
        if len(args) >= n:
            return args[:n]
        return tuple(args) + (None,) * (n - len(args))

    def _admits(self, code: Any) -> bool:
        """Should we report events for this code object?  Memoised.

        Two costs are being avoided here, and both are per-*event* costs, which
        is the whole problem:

        1.  ``codeinfo.is_internal`` does a string ``.replace("\\", "/")`` plus a
            ``startswith`` -- a fresh string allocation per event.
        2.  ``TracerConfig.admits`` does the same normalisation again, then
            ``len(globals)``, tuple membership, and finally your own
            ``include``/``exclude`` predicates.

        PEP 669 calls this predicate once per *line*, so it is the single
        hottest Python-level operation in the whole tracer.  Memoising on the
        code object is correct because the answer depends only on the code
        object (which is immutable) and on ``self.config``, which cannot change
        while the tracer is active.  Measured on CPython 3.14, this alone took
        the tracer from slower-than-``sys.settrace`` to faster than it.

        The cache is cleared on ``stop()`` so a long-lived tracer that is
        started and stopped repeatedly does not grow without bound.
        """
        # Keyed on the code object itself, not on id() or a tuple. CodeType has
        # no __eq__/__hash__, so dict lookup is identity-based *and* the dict
        # holds a strong reference -- which is what makes id() safe here. A
        # tuple key would allocate on every event for no benefit.
        cached = self._admits_cache.get(code)
        if cached is None:
            cached = (not codeinfo.is_internal(code)) and self.config.admits(code)
            self._admits_cache[code] = cached
        return cached

    def _depth_now(self) -> int:
        """O(1) stack depth for this thread.

        Incremented on ``PY_START`` and decremented on ``PY_RETURN`` /
        ``PY_UNWIND``.  It can drift if a callback raises or if we decline to
        trace a frame inconsistently, so :meth:`_locate` remains available as
        the authoritative version for when correctness beats speed.
        """
        return getattr(self._tls, "depth", 0)

    def _push_depth(self) -> None:
        self._tls.depth = getattr(self._tls, "depth", 0) + 1

    def _pop_depth(self) -> None:
        self._tls.depth = max(0, getattr(self._tls, "depth", 0) - 1)

    def _needs_frame(self) -> bool:
        """Is the live frame required for this event?

        Only ``capture_locals``, ``capture_stack`` and ``capture_args`` need it.
        Everything else can be built from the code object alone -- and skipping
        the walk in that case is the difference between a monitoring tracer that
        is faster than ``sys.settrace`` and one that is twice as slow.
        """
        c = self.config
        return bool(c.capture_locals or c.capture_stack or c.capture_args)

    def _ctx(self, code: Any) -> tuple:
        """Return ``(frame, depth)`` for an event about ``code``.

        Two independent questions, two independent answers:

        *frame*
            Only needed for ``capture_locals`` / ``capture_stack`` / call
            arguments.  Found by identity search up ``f_back``.  Works for every
            event kind **including** ``PY_UNWIND``, which is surprising and
            useful -- see :meth:`_locate`.

        *depth*
            Always the counter, never the frame position.  Walking ``f_back``
        counts the frames *below* the match, which is only the stack depth when
            the match happens to be the innermost frame -- true for ``LINE``,
            false for ``PY_UNWIND`` and ``PY_RETURN``, where the frame has
            already been detached from the interpreter's frame chain.  And the
            counter is what a flame graph and a stack tree need: consistent
            *relative* nesting among traced frames.

            So ``depth`` means "how many admitted frames are live beneath this
            one on this thread", which is the definition a trace consumer
            actually needs, and
            :attr:`MonitorTracer.depth_semantics` says so in the report.
        """
        frame = self._locate(code)[0] if self._needs_frame() else None
        return frame, self._depth_now()

    def _locate(self, code: Any) -> tuple:
        """Find the live frame executing ``code``, and its depth.

        PEP 669 callbacks receive a *code object*, not a frame.  The obvious
        approach -- assume the frame is a fixed number of ``f_back`` hops above
        the callback -- is wrong, and wrong in a way that breaks the moment you
        refactor, because the hop count depends on how many Python frames your
        own dispatch chain happens to add::

            <the traced frame>          <- offset 0 in this chain
            <lambda>                    <- registered callback
            _guard                      <- exception guard
            _h_line                     <- handler
            _locate / walk              <- us

        Today that is 4 hops.  A version bump, an ``async def`` handler, or one
        extra helper makes it 5 and every line number silently shifts.  So we do
        not guess: the frame is guaranteed to be on the stack while its event
        fires, and code objects are singletons, so identity comparison finds it
        exactly.  :meth:`_locate` is why that stays true.

        Cost is O(depth), which is why ``_admits`` runs first: an untraced code
        object costs one prefix comparison and nothing else.

        Two things are worth knowing about this function.

        First, it finds the frame even for ``PY_UNWIND``, which is not obvious:
        ``PY_UNWIND`` fires while the frame is being destroyed, so you would
        expect it to be gone.  It is not -- ``frame.f_back`` is still intact, and
        CPython keeps the frame alive for the duration of the callback.  That is
        precisely what makes ``PY_UNWIND`` the only event at which a failed
        frame's locals can be captured (see ``examples/09``).

        Second, the returned depth **excludes framecraft's own dispatch frames**.  We are
        always three or four Python frames deeper than the code being reported --
        ``caller_frame`` -> ``_ctx`` -> ``_h_line`` -> ``callback`` -- so a naive
        index is off by exactly that much, and the offset changes whenever a
        handler is refactored.  Skipping leading frames whose code belongs to
        framecraft's own machinery makes the number mean "depth in the *traced
        program*", which is the only definition anyone reading a trace wants.
        """
        depth = -1
        for f in framecore.walk():
            if f.f_code is code:
                return f, depth + 1
            if not codeinfo.is_internal(f.f_code):
                depth += 1
        return None, 0

    def _frame_for(self, code: Any) -> Any | None:
        return self._locate(code)[0]

    def _depth(self, code: Any) -> int:
        return self._locate(code)[1]

    # ------------------------------------------------------------------
    # Handlers.  One per PEP 669 event, each taking ``*args`` from
    # `_callback_for` and pulling its payload out with `_payload`.
    #
    # The recurring pattern is deliberate:
    #
    #     if not self._admits(code): return          # one prefix compare
    #     frame = self._locate(code) if needs else None
    #
    # `_admits` runs first because an untraced code object must cost exactly
    # one string-prefix test, and because it is the only way out of a code
    # object we do not want to hear about.
    # ------------------------------------------------------------------
    def _h_py_start(self, *args: Any) -> None:
        code, offset = self._payload(args, 2)
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                CALL,
                frame,
                code=code,
                offset=offset,
                lineno=_line_at(code, offset),
                call_id=self._next_seq(),
                depth=_depth,
                arg_repr=self._args_repr(frame) if (frame is not None and self.config.capture_args) else None,
            )
        )
        self._push_depth()

    def _h_py_return(self, *args: Any) -> None:
        code, offset, retval = self._payload(args, 3)
        if not self._admits(code):
            return
        self._pop_depth()
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                RETURN,
                frame,
                code=code,
                offset=offset,
                lineno=_line_at(code, offset),
                depth=_depth,
                retval_repr=self._r(retval),
            )
        )

    def _h_py_yield(self, *args: Any) -> None:
        code, offset, retval = self._payload(args, 3)
        if not self._admits(code):
            return
        # PY_YIELD suspends the frame but does NOT pop it: the frame is still
        # live (that is the whole point of a generator frame), so the depth
        # counter is deliberately left alone.  Getting this wrong makes every
        # subsequent depth in the trace wrong by one per yield.
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                YIELD,
                frame,
                code=code,
                offset=offset,
                lineno=_line_at(code, offset),
                depth=_depth,
                retval_repr=self._r(retval),
            )
        )

    def _h_line(self, *args: Any) -> None:
        # Hot path: LINE fires once per executed line.  `_payload` unpacking is
        # inlined here and the handlers are all `*args`, so a version that grows
        # an extra argument degrades to an unused trailing value rather than a
        # TypeError inside a hook.
        code, line = args[0], args[1]
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        self._emit(self._make_event(LINE, frame, code=code, lineno=line, depth=_depth))

    def _h_instruction(self, *args: Any) -> None:
        """``INSTRUCTION`` (PEP 749, 3.14+): one event per executed bytecode.

        The documented signature is
        ``(code, instruction_offset, opname, arg, argrepr)`` but 3.14.6 passes
        only ``(code, instruction_offset)``.  We therefore *look the opcode up
        ourselves* from the offset, which we would have to do on the documented
        version anyway, and which is immune either way.

        This is the 3.14 replacement for ``C_CALL``: C calls are encoded as
        ``C_CALL``/``C_FUNCTION`` instructions, so an ``INSTRUCTION`` tracer
        still sees them.  ``examples/04_opcode_tracer.py`` demonstrates the
        opcode-level equivalent of ``sys.settrace(opcode_events=True)``.
        """
        code, offset, *_rest = self._payload(args, 5)
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        index = _opcode_index(code)
        self._emit(
            self._make_event(
                OPCODE,
                frame,
                code=code,
                offset=offset,
                lineno=index.lines.get(offset) or (frame.f_lineno if frame is not None else None),
                opcode=index.names.get(offset),
                depth=_depth,
                extra={"pep669_event": "INSTRUCTION"},
            )
        )

    def _h_call(self, *args: Any) -> None:
        """``CALL``: a call *site*, with the callee and its first argument.

        Nothing else in Python gives you this.  ``sys.settrace``'s ``'call'``
        event has no callable at all (you must find the callee by
        disassembling the calling instruction), and ``sys.setprofile``'s
        ``'call'`` event has none either.  If you want "who calls this, with
        what", this is the event.
        """
        code, offset, callable_, arg0 = self._payload(args, 4)
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                CALL_SITE,
                frame,
                code=code,
                offset=offset,
                lineno=_line_at(code, offset),
                depth=_depth,
                c_name=getattr(callable_, "__qualname__", None) or repr(callable_),
                arg_repr=self._r(arg0),
                extra={"pep669_event": "CALL"},
            )
        )

    def _h_c_call(self, *args: Any) -> None:
        """``C_CALL`` / ``C_FUNCTION`` (3.12-3.13 only; removed in 3.14)."""
        code, offset, callable_, arg0 = self._payload(args, 4)
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                C_CALL,
                frame,
                code=code,
                offset=offset,
                depth=_depth,
                c_name=getattr(callable_, "__qualname__", None) or repr(callable_),
                arg_repr=self._r(arg0),
                extra={"pep669_event": "C_CALL"},
            )
        )

    def _h_c_return(self, *args: Any) -> None:
        """``C_RETURN`` / ``C_FUNCTION_RETURN``.

        3.12/3.13 pass ``(code, offset, callable, arg0, retval)``; 3.14 passes
        only ``(code, offset, callable, arg0)``.  ``_payload(args, 5)`` yields
        ``retval=None`` on the newer shape instead of raising, which is the
        entire reason callbacks are declared ``*args``.
        """
        code, offset, callable_, arg0, retval = self._payload(args, 5)
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                C_RETURN,
                frame,
                code=code,
                offset=offset,
                depth=_depth,
                c_name=getattr(callable_, "__qualname__", None) or repr(callable_),
                retval_repr=self._r(retval) if self.config.capture_return else None,
                arg_repr=self._r(arg0),
                extra={"pep669_event": "C_RETURN", "retval_available": retval is not None},
            )
        )

    def _h_c_raise(self, *args: Any) -> None:
        # `_c_raise`'s payload is (code, offset, callable, arg0). The
        # exception object does not exist yet -- C_RAISE fires *at* the
        # call -- so there is nothing to render but the callable's name.
        code, offset, callable_, _arg0 = self._payload(args, 4)
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                C_EXCEPTION,
                frame,
                code=code,
                offset=offset,
                depth=_depth,
                c_name=getattr(callable_, "__qualname__", None) or repr(callable_),
                exception_type="<C exception>",
                extra={"pep669_event": "C_RAISE"},
            )
        )

    def _h_exception(self, *args: Any) -> None:
        """``EXCEPTION`` / ``EXCEPTION_HANDLED``: raised here / handled here."""
        code, offset, exc = self._payload(args, 3)
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                EXCEPTION,
                frame,
                code=code,
                offset=offset,
                lineno=_line_at(code, offset),
                depth=_depth,
                exception_type=type(exc).__qualname__ if exc is not None else "<unknown>",
                exception_repr=self._r(exc),
                extra={"phase": "raised", "pep669_event": "EXCEPTION"},
            )
        )

    def _h_raise(self, *args: Any) -> None:
        """``RAISE`` (3.14): a ``raise`` statement is about to execute.

        Fires *before* the exception object exists, so there is no ``exc``
        argument -- which is the point: it covers raise sites, including ones
        that are never reached, and it is the only exception-family event that
        fires for a raise which is handled inside the same frame.

        The event kind depends on what the caller asked for.  Both kinds describe
        the same observation, so this is a presentation choice, not a semantic
        one; ``EXCEPTION`` is the friendlier name if you were not filtering for
        ``raise_site`` specifically.
        """
        code, offset = self._payload(args, 2)
        if not self._admits(code):
            return
        frame, _depth = self._ctx(code)
        kind = EXCEPTION if self.config.wants(EXCEPTION) else RAISE_SITE
        self._emit(
            self._make_event(
                kind,
                frame,
                code=code,
                offset=offset,
                lineno=_line_at(code, offset),
                depth=_depth,
                exception_type="<raise statement>"
                if kind == EXCEPTION
                else None,
                extra={"phase": "raise-opcode", "pep669_event": "RAISE"},
            )
        )

    def _h_unwind(self, *args: Any) -> None:
        """``PY_UNWIND`` / ``PY_THROW``: the frame is being torn down.

        This is the *last* moment at which this frame's locals exist.  After the
        callback returns, the frame is gone.  That makes ``PY_UNWIND`` the
        correct event for "capture state at the moment of failure" --
        ``examples/09_exceptions_and_unwinding.py`` builds exactly that.
        """
        code, offset, exc = self._payload(args, 3)
        if not self._admits(code):
            return
        self._pop_depth()
        frame, _depth = self._ctx(code)
        self._emit(
            self._make_event(
                EXCEPTION,
                frame,
                code=code,
                offset=offset,
                lineno=_line_at(code, offset),
                depth=_depth,
                exception_type=type(exc).__qualname__ if exc is not None else "<unknown>",
                exception_repr=self._r(exc),
                extra={"phase": "unwind", "pep669_event": "PY_UNWIND"},
            )
        )

    def _r(self, value: Any) -> str | None:
        """``safe_repr`` with this tracer's policy, or ``None`` if not wanted."""
        if not self.config.capture_return:
            return None
        return safe_repr(value, policy=self.config.policy, redactor=self.config.redactor)

    def _args_repr(self, frame: Any) -> str:
        if frame is None:
            return ""
        code = frame.f_code
        n = code.co_argcount + code.co_kwonlyargcount
        if n == 0:
            return ""
        vals = frame.f_locals
        parts = []
        for name in code.co_varnames[:n]:
            if name in vals:
                parts.append(
                    f"{name}={safe_repr(vals[name], name=name, policy=self.config.policy, redactor=self.config.redactor)}"
                )
        return ", ".join(parts)

    def _install_task_factory(self) -> None:
        """Record which asyncio task created each coroutine.

        PEP 669 reports coroutine frames individually but cannot tell you which
        *task* a coroutine belongs to.  Wrapping the task factory gives us that
        mapping at creation time, which is the only cheap way to keep it (by the
        time a coroutine is running, the creating frame is long gone).

        See :meth:`framecraft.trace.base.TracerConfig.trace_asyncio_tasks`.
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet; nothing to hook
        self._prev_task_factory = loop.get_task_factory()
        outer = self
        inner = self._prev_task_factory

        def factory(loop: Any, coro: Any, **kwargs: Any) -> Any:
            try:
                outer._task_origins[coro] = {
                    "name": _coro_name(coro),
                    "created_at": framecore.clocks()["wall"],
                    "created_by": [f.location for f in framecore.stack_snapshot(max_frames=4)],
                }
            except BaseException:
                pass
            if inner is not None:
                return inner(loop, coro, **kwargs)
            return asyncio.Task(coro, loop=loop, **kwargs)

        self._task_factory = factory
        loop.set_task_factory(factory)


def _line_at(code: Any, offset: Any) -> int | None:
    """Byte offset -> source line, from a per-code-object cache.

    ``codeinfo.offset_to_line`` scans ``co_lines()``, which is O(instructions).
    Calling it once per LINE event made a PEP 669 tracer *slower* than
    ``sys.settrace`` -- the interpreter's own dispatch is cheap, and we were
    throwing that away by rescanning a line table on every event.

    ``_OpcodeIndex`` already builds the offset->line map, so reuse it rather
    than keeping a second one.  Falls back to ``co_firstlineno`` when the offset
    is not attributable (a CACHE slot, or the synthetic return).
    """
    if offset is None:
        return code.co_firstlineno
    line = _opcode_index(code).lines.get(offset)
    return line if line is not None else code.co_firstlineno


def _coro_name(coro: Any) -> str:
    """A readable name for a coroutine/awaitable passed to a task factory."""
    cr = getattr(coro, "cr_code", None)
    if cr is not None:
        return codeinfo.qualified_name(cr)
    return type(coro).__qualname__
