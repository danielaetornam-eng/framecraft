"""``sys.settrace``-based tracers: the call/return/line engine.

``sys.settrace`` is a two-level hook and almost every tutorial gets it subtly
wrong, so the mechanics are spelled out here once, in order, because everything
else in this file depends on them.

The two levels
--------------

**Global trace function.**  Installed with ``sys.settrace(fn)``.  Called only for
``'call'`` events -- the moment CPython creates a frame for a function call.
Its job is to *decide* whether this frame is interesting.  Returning ``None``
means "don't instrument this frame" and is the single most effective
performance lever you have: the decision happens once per call instead of once
per line.

**Local trace function.**  Whatever the global function returned.  CPython
assigns it to ``frame.f_trace``.  From then on this function is called for
``'line'``, ``'return'``, ``'exception'`` and (optionally) ``'opcode'`` events
in *that* frame::

    sys.settrace(global_fn)
        global_fn(frame, 'call', None) -> local_fn
            frame.f_trace = local_fn
            local_fn(frame, 'line', None)          must return local_fn
            local_fn(frame, 'return', retval)      must return local_fn
            local_fn(frame, 'exception', (t,v,tb)) must return local_fn

The footgun that costs everyone an afternoon
--------------------------------------------

**A local trace function must return itself.**  The return value of the local
trace function *replaces* ``frame.f_trace``.  Return ``None`` -- which is what
``def local(...): print(...)`` implicitly does -- and CPython sets
``frame.f_trace = NULL``: the frame stops being traced at the next event, with
no warning.  You get a trace with a plausible-looking hole in the middle and no
error anywhere.  Every hook in this module returns :attr:`FunctionTracer._local`.

Three more things that surprise people
--------------------------------------

1.  **Frames already on the stack are not instrumented.**  Installing a hook
    affects frames created *after* installation.  To trace a function that is
    already executing (e.g. from inside a recursive hook), walk ``f_back`` and
    set ``f_trace`` by hand -- :meth:`FunctionTracer.attach_existing` does it.

2.  **The hook is per-thread.**  ``sys.settrace`` sets the hook for the *current*
    thread only.  Use ``TracerConfig.include_threads=True`` to install into new
    threads via ``threading.settrace``, or :meth:`FunctionTracer.attach_existing`
    from any thread (``frame.f_trace`` is a property of the frame, not of the
    hook slot, so it works cross-thread).

3.  **Only one global hook.**  ``sys.settrace`` and ``sys.setprofile`` are
    independent slots but there is exactly one of each.  See
    :class:`framecraft.trace.base.HookLease`.

Call/return pairing without touching frame objects
---------------------------------------------------

You cannot set attributes on a :class:`types.FrameType` -- it has no
``__dict__``.  So how do you know that *this* ``return`` event belongs to *that*
``call`` event?  By ``id(frame)``, and it is safe:

* a frame object is alive for exactly as long as it is on the stack,
* so no two *live* frames can share an ``id``,
* and the entry is deleted on ``'return'``.

The alternative -- a per-thread LIFO stack -- is what most implementations use
and it leaks the moment tracing stops mid-call, because the matching ``return``
never arrives.  The id map is self-healing: a stale entry is simply overwritten
by the next frame that reuses the address.
"""

from __future__ import annotations

import sys
import threading
from types import FrameType
from typing import Any

from framecraft._internal import codeinfo
from framecraft._internal.values import safe_repr
from framecraft.core import frames as framecore
from framecraft.core.events import CALL, EXCEPTION, LINE, OPCODE, RETURN
from framecraft.trace.base import Tracer, TracerConfig

__all__ = ["FunctionTracer", "LineTracer", "install_thread", "uninstall_thread"]




class _Tls(threading.local):
    """Per-thread tracing bookkeeping."""

    def __init__(self) -> None:
        #: ``id(frame) -> call_id`` for frames currently executing that we traced.
        self.calls: dict[int, int] = {}
        #: Number of traced frames currently live on this thread's stack.
        self.depth: int = 0


class FunctionTracer(Tracer):
    """A ``sys.settrace`` tracer emitting ``call``/``line``/``return``/``exception``.

    Parameters
    ----------
    config:
        See :class:`~framecraft.trace.base.TracerConfig`.
    opcode_events:
        Also emit per-instruction ``opcode`` events.  Roughly 10-100x more
        events than ``line`` events, and it disables a handful of CPython
        optimisations.  Off by default; on in ``examples/05_opcode_tracer.py``.

    Per-frame precision
    --------------------
    ``frame.f_trace_lines = False`` suppresses ``'line'`` events for one frame
    while keeping ``'call'``/``'return'``; ``frame.f_trace_opcodes = False``
    suppresses ``'opcode'``.  Both are read/write and are checked in C, so using
    them is far cheaper than filtering events in Python.

    There is a third flag, ``frame.f_trace_functions`` (3.13+), that sounds like
    it should give you call/return-only tracing -- and it does -- but you
    **cannot set it from Python**.  CPython defines only a getter
    (``frame_get_trace_functions`` in ``frameobject.c``); assigning raises
    ``AttributeError: 'frame' object has no attribute 'f_trace_functions'``.
    So the call/return-only mode you want has to be spelled
    ``f_trace_lines = False``, which is what :meth:`verbosity_for` returns.
    """

    def __init__(
        self,
        config: TracerConfig | None = None,
        *,
        opcode_events: bool = False,
        **overrides: Any,
    ) -> None:
        super().__init__(config, **overrides)
        self.opcode_events = opcode_events
        self._tls = _Tls()
        #: How many frames :meth:`attach_existing` has instrumented in total.
        #: Exposed because 'it attached to N frames and saw no events' is the
        #: signature of a missing global hook, and a bare count makes that
        #: diagnosis one glance rather than an experiment.
        self.attach_count = 0

    # ------------------------------------------------------------------
    # filtering
    # ------------------------------------------------------------------
    def should_trace(self, code: Any) -> bool:
        """Decide, once per call, whether to instrument ``code``'s frame.

        Returning ``False`` means: do not set ``f_trace``, do not emit anything,
        and pay essentially zero cost inside the function.  This is where you
        put "never trace anything under ``site-packages``", and it is the
        difference between a tracer that adds 3% and one that adds 300%.

        The framecraft package itself is excluded unconditionally -- see
        :func:`framecraft._internal.codeinfo.is_internal`.  Override this method (rather than setting
        ``TracerConfig.include``) if you want to keep that behaviour and add
        your own rules on top.
        """
        if codeinfo.is_internal(code):
            return False
        return self.config.admits(code)

    def verbosity_for(self, frame: FrameType) -> tuple[bool, bool]:
        """Return ``(trace_lines, trace_opcodes)`` for a frame.

        Split out so subclasses can make per-frame decisions.  The rule of
        thumb: prefer setting ``f_trace_lines = False`` over "filter the line
        events in Python", because the flag is checked in C and costs nothing,
        while filtering in Python costs a Python call per line -- which is the
        difference between a 3x and a 30x slowdown.
        """
        lines = self.config.wants(LINE) or self.config.wants(OPCODE)
        opcodes = self.opcode_events and self.config.wants(OPCODE)
        return lines, opcodes

    def _apply_verbosity(self, frame: FrameType) -> None:
        """Arm the frame: set ``f_trace`` explicitly, then the verbosity flags.

        **Order matters, and getting it wrong is silent.**

        The documented idiom is to set the flags and then *return* the local
        trace function, letting CPython install it::

            def global_trace(frame, event, arg):
                if event == 'call':
                    frame.f_trace_opcodes = True     # <-- this flag is LOST
                    return local_trace

        On CPython 3.14.6 that produces **no** ``'opcode'`` events at all, with
        no warning.  ``f_trace_lines`` survives; ``f_trace_opcodes`` does not.
        Minimal reproducer::

            import sys
            from collections import Counter
            seen = []
            def g(frame, event, arg):
                if event == 'call':
                    frame.f_trace_lines = frame.f_trace_opcodes = True
                    return l
                return l
            def l(frame, event, arg):
                seen.append(event); return l
            def work():
                a = 1
                return a
            sys.settrace(g); work(); sys.settrace(None)
            # Counter({'line': 2, 'return': 1})  -- no 'opcode'

        Assigning ``frame.f_trace`` *before* the flags makes it work, because
        the flag setter has a live trace object to write into::

            frame.f_trace = self._local       # must come first
            frame.f_trace_lines = True
            frame.f_trace_opcodes = True
            return self._local                 # still return it, per the docs

        We do both, so the tracer is correct whether or not a future version
        fixes the underlying quirk.
        """
        frame.f_trace = self._local
        lines, opcodes = self.verbosity_for(frame)
        frame.f_trace_lines = lines
        frame.f_trace_opcodes = opcodes
        if opcodes:
            # Opcode events need the offset of every instruction.  Since 3.11
            # offsets are *byte* offsets interleaved with CACHE entries, so we
            # precompute offset->(line, opname) once per code object instead of
            # calling co_lines()/dis.get_instructions() per event.
            _opcode_index(frame.f_code)

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------
    def _global_trace(self, frame: FrameType, event: str, arg: Any) -> Any:
        """The global trace function.  Only ever sees ``'call'``."""
        if event != "call":  # pragma: no cover - CPython guarantees 'call' here
            return self._local
        code = frame.f_code
        if not self.should_trace(code):
            # The documented way to say "no thanks": returning None leaves
            # frame.f_trace unset, so nothing inside this function is traced.
            return None

        call_id = self._next_seq()
        back = frame.f_back
        parent = self._tls.calls.get(id(back)) if back is not None else None
        self._tls.calls[id(frame)] = call_id
        self._tls.depth += 1

        if self.config.wants(CALL):
            self._guard(
                lambda: self._emit(
                    self._make_event(
                        CALL,
                        frame,
                        call_id=call_id,
                        parent_call_id=parent,
                        depth=self._tls.depth - 1,
                        arg_repr=self._args_repr(frame) if self.config.capture_args else None,
                    )
                )
            )
        self._apply_verbosity(frame)
        return self._local

    def _local(self, frame: FrameType, event: str, arg: Any) -> Any:
        """The local trace function.  Sees line/return/exception/opcode."""
        if event == "line":
            if self.config.wants(LINE):
                # Inlined rather than `self._guard(lambda: ...)`: LINE fires once
                # per executed line, and the extra Python frame that a lambda
                # costs is a measurable fraction of the whole tracer.
                try:
                    self._emit(self._make_event(LINE, frame, depth=self._tls.depth))
                except BaseException as exc:
                    self._note_error(exc)
            return self._local

        if event == "opcode":
            if self.config.wants(OPCODE):
                offset = frame.f_lasti
                index = _opcode_index(frame.f_code)
                self._guard(
                    lambda: self._emit(
                        self._make_event(
                            OPCODE,
                            frame,
                            offset=offset,
                            lineno=index.lines.get(offset, frame.f_lineno),
                            opcode=index.names.get(offset),
                            depth=self._tls.depth,
                        )
                    )
                )
            return self._local

        if event == "return":
            call_id = self._tls.calls.pop(id(frame), None)
            if self._tls.depth > 0:
                self._tls.depth -= 1
            if self.config.wants(RETURN):
                try:
                    self._emit(
                        self._make_event(
                            RETURN,
                            frame,
                            call_id=call_id,
                            depth=self._tls.depth,
                            retval_repr=safe_repr(
                                arg, policy=self.config.policy, redactor=self.config.redactor
                            )
                            if self.config.capture_return
                            else None,
                        )
                    )
                except BaseException as exc:
                    self._note_error(exc)
            # NOTE: we deliberately return `self._local`, not None.  Returning
            # None here would be legal but CPython ignores it on 'return'
            # anyway; returning the function keeps the invariant obvious and
            # survives a future version that starts honouring it.
            return self._local

        if event == "exception":
            if self.config.wants(EXCEPTION):
                etype, value, _tb = arg if isinstance(arg, tuple) else (type(arg), arg, None)
                self._guard(
                    lambda: self._emit(
                        self._make_event(
                            EXCEPTION,
                            frame,
                            depth=self._tls.depth,
                            exception_type=getattr(etype, "__qualname__", str(etype)),
                            exception_repr=safe_repr(
                                value, policy=self.config.policy, redactor=self.config.redactor
                            ),
                        )
                    )
                )
            return self._local

        return self._local  # pragma: no cover - defensive

    def _args_repr(self, frame: FrameType) -> str:
        """Reconstruct the call signature from the frame's fast locals.

        At a ``'call'`` event the arguments are already bound, and for a
        non-generator function ``co_varnames[:argcount + kwonlyargcount]`` is
        exactly the parameter list *in order* -- including positional-only and
        keyword-only, which ``co_argcount`` alone would mangle.

        This is the cheapest correct way to log a call signature in
        ``sys.settrace``, and it is why ``cProfile``'s call args look like
        ``(1, 2)`` rather than ``locals()``.
        """
        code = frame.f_code
        n = code.co_argcount + code.co_kwonlyargcount
        if n == 0:
            return ""
        names = code.co_varnames[:n]
        vals = frame.f_locals
        parts = []
        for name in names:
            if name not in vals:
                continue
            parts.append(f"{name}={safe_repr(vals[name], name=name, policy=self.config.policy, redactor=self.config.redactor)}")
        return ", ".join(parts)

    # ------------------------------------------------------------------
    # mid-flight attachment
    # ------------------------------------------------------------------
    def attach_existing(self, start: FrameType | None = None, *, depth: int | None = None) -> int:
        """Instrument frames that are *already executing*.

        This is the escape hatch from footgun (1) in the module docstring.
        ``sys.settrace`` only affects frames created after installation, so a
        tracer that starts inside the function it wants to trace sees nothing.
        Walking ``f_back`` and assigning ``frame.f_trace`` directly is the
        documented way around it.

        **It only works in a thread that already has a global hook installed.**
        Measured on CPython 3.14.6, all three of these produce *zero* events:

        .. code-block:: python

            # 1. no global hook anywhere
            frame.f_trace = local; frame.f_trace_lines = True

            # 2. a global hook, but in a different thread than the frame's
            sys.settrace(global_hook)          # thread A
            other_thread_frame.f_trace = local  # frame belongs to thread B

            # 3. same as 2, via this method

        The docs state the requirement -- "a global tracing function must have
        been installed with settrace() in order to enable the runtime tracing
        machinery" -- and it is per-thread, because the hook slot is a
        thread-state field.  So:

        *   **This thread**: works.  The tracer installed ``sys.settrace`` here
            when it started.
        *   **Another thread this tracer started**: works only if
            ``TracerConfig(include_threads=True)`` was set, because that is what
            calls ``threading.settrace``.
        *   **A thread that was already running**: needs :meth:`arm_thread`
            called *from that thread*, first.

        The symptom when you get this wrong is not an error -- it is a tracer
        that instruments frames and then reports nothing, which looks identical
        to a filtering bug.  Hence :meth:`arm_thread` rather than a comment
        telling you to remember.

        Returns the number of frames instrumented.
        """
        if not self.active:
            raise RuntimeError("attach_existing() requires an active tracer")
        count = 0
        for i, frame in enumerate(framecore.walk(start)):
            if depth is not None and i >= depth:
                break
            code = frame.f_code
            if not self.should_trace(code):
                continue
            if frame.f_trace is not None:
                continue  # already instrumented by us or by someone else
            call_id = self._next_seq()
            back = frame.f_back
            self._tls.calls[id(frame)] = call_id
            self._tls.depth = i + 1
            if self.config.wants(CALL):
                self._guard(
                    lambda f=frame, c=call_id, p=back, i2=i: self._emit(
                        self._make_event(
                            CALL,
                            f,
                            call_id=c,
                            parent_call_id=self._tls.calls.get(id(p)) if p is not None else None,
                            depth=i2,
                            arg_repr=self._args_repr(f) if self.config.capture_args else None,
                        )
                    )
                )
            self._apply_verbosity(frame)
            count += 1
        self.attach_count += count
        return count

    def arm_thread(self) -> bool:
        """Arm the *calling* thread so :meth:`attach_existing` works here.

        Call this from inside a worker thread that was already running when the
        tracer started, before attaching to its frames::

            def worker():
                tracer.arm_thread()        # required: no global hook here yet
                tracer.attach_existing()
                ...

        With ``TracerConfig(include_threads=True)`` this is unnecessary for new
        threads -- ``threading.settrace`` already covered them -- and only needed
        for threads that predate the tracer.

        Note the trade-off: installing the hook changes this thread's behaviour
        until it exits.  A thread that only wants to be *observed* rather than
        traced should not call this; it should have been started with
        ``include_threads=True`` so that ``threading.settrace`` did the work at
        thread-creation time, where you could still decline.

        Returns whether the hook was installed.
        """
        return install_thread(self)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def _start(self) -> None:
        if self.config.include_threads:
            self._prev_thread_trace = threading.gettrace()
            threading.settrace(self._global_trace)
        sys.settrace(self._global_trace)

    def _stop(self) -> None:
        # Detach every f_trace we own *before* clearing the global hook, or
        # already-running frames keep calling into a stopped tracer.
        _detach_all(self._local)
        sys.settrace(None)
        self._tls.calls.clear()
        self._tls.depth = 0

    def __repr__(self) -> str:
        return (
            f"<FunctionTracer active={self.active} events={len(self.events)} "
            f"line={self.config.wants(LINE)} opcode={self.config.wants(OPCODE)} "
            f"dropped={self.dropped} errors={self.error_count}>"
        )


#: The name most people expect.  ``FunctionTracer`` traces function
#: *activation*; this alias exists because that is what it does day to day.
LineTracer = FunctionTracer


# ----------------------------------------------------------------------
# per-thread install helpers
# ----------------------------------------------------------------------


def install_thread(tracer: FunctionTracer) -> bool:
    """Install ``tracer``'s global hook on the **calling** thread.

    ``sys.settrace`` writes to a thread-state field, so it affects the calling
    thread and nothing else.  To trace a worker that already exists you must
    therefore call this *from inside that worker*.

    Calling this is a prerequisite for :meth:`FunctionTracer.attach_existing`
    observing anything on that thread: assigning ``frame.f_trace`` only arms the
    per-frame hook, and the interpreter needs the global hook present before it
    consults it.

    Returns ``True`` if the hook was installed.  It is a no-op for an inactive
    tracer, because arming a hook for a tracer that is not running would leave
    a dangling reference behind -- and a dangling hook is worse than none.
    """
    if not tracer.active:
        return False
    sys.settrace(tracer._global_trace)
    return True


def uninstall_thread() -> None:
    """Remove the global trace hook on the calling thread."""
    sys.settrace(None)


def _detach_all(local_fn: Any) -> int:
    """Clear any ``f_trace`` equal to ``local_fn`` in every running thread.

    ``sys._current_frames()`` is the only supported way to reach another
    thread's frames; ``gc.get_objects()`` would also work but is far slower and
    perturbs reference counts on the whole heap.
    """
    count = 0
    for frame in list(sys._current_frames().values()):
        f = frame
        while f is not None:
            if f.f_trace is local_fn:
                f.f_trace = None
                count += 1
            f = f.f_back
    return count


# ----------------------------------------------------------------------
# per-code-object caches
# ----------------------------------------------------------------------


class _OpcodeIndex:
    """``byte offset -> line`` and ``byte offset -> opname`` for one code object.

    Both maps are built once per code object.  Without them an opcode tracer
    pays a full ``dis.get_instructions()`` walk *per instruction executed*.
    """

    __slots__ = ("lines", "names")

    def __init__(self, code: Any) -> None:
        self.lines: dict[int, int | None] = {start: ln for start, end, ln in code.co_lines()}
        names: dict[int, str] = {}
        try:
            import dis

            for ins in dis.get_instructions(code):
                if ins.opname in ("CACHE", "EXTENDED_ARG"):
                    continue
                names[ins.offset] = ins.opname
        except Exception:  # pragma: no cover - malformed code objects
            pass
        self.names = names


_OPCODE_CACHE: dict[Any, _OpcodeIndex] = {}


def _opcode_index(code: Any) -> _OpcodeIndex:
    idx = _OPCODE_CACHE.get(code)
    if idx is None:
        idx = _OpcodeIndex(code)
        _OPCODE_CACHE[code] = idx
    return idx
