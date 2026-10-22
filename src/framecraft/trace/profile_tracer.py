"""``sys.setprofile``: call/return timing that also sees into C.

``sys.setprofile`` is the older, dumber, faster sibling of ``sys.settrace``.
It gets three things right that ``settrace`` does not, and it gets one thing
badly wrong.

What it gets right
------------------

1.  **It sees C function calls.**  ``'c_call'`` and ``'c_return'`` events fire
    for every C function CPython calls on your behalf -- ``dict.get``,
    ``list.append``, ``io.write``, ``time.time``.  ``sys.settrace`` is blind to
    all of them.  If you want to know where your program actually spends time
    *inside the interpreter*, this hook is mandatory.

2.  **It is much cheaper.**  No per-line callback, so the overhead on a
    line-heavy workload is a fraction of ``settrace``.

3.  **It reports ``c_exception``**, letting you see that a C-level API raised
    without any Python frame being involved.

What it gets wrong
------------------

**It cannot see line numbers or exceptions in Python code.**  You get
``'call'`` and ``'return'`` only.  That is why ``cProfile`` -- which is
literally this hook plus statistics -- produces flat timings with no source
context, and why you have to reach for ``line_profiler`` when you want
per-line numbers.

The mechanism
-------------

One function, ``profile(frame, event, arg)``, installed with
``sys.setprofile``.  Events:

=============  ========  =====================================================
``event``      ``arg``   meaning
=============  ========  =====================================================
``call``       ``None``  a Python frame was created
``return``     retval    a Python frame returned
``c_call``     callable  about to call a C function
``c_return``   retval    the C function returned
``c_exception`` exc       the C function raised
=============  ========  =====================================================

There is no two-level structure here: a single function sees everything.  The
one consequence you must handle is that ``c_return``/``c_exception`` give you
the *callable* but **not** the frame -- so you cannot ask "what line called
this?".  :class:`ProfileTracer` solves that by keeping its own depth counter and
attributing each C event to the innermost live Python frame, which is exactly
what a human reading a flat profile wants.
"""

from __future__ import annotations

import sys
import threading
from types import FrameType
from typing import Any

from framecraft._internal import codeinfo
from framecraft._internal.values import safe_repr
from framecraft.core.events import C_CALL, C_EXCEPTION, C_RETURN, CALL, RETURN
from framecraft.trace.base import Tracer, TracerConfig

__all__ = ["ProfileTracer"]


class _PTls(threading.local):
    def __init__(self) -> None:
        #: innermost-first list of live Python frames we called into
        self.stack: list[FrameType] = []
        #: time.perf_counter_ns at which each entry was pushed
        self.entered_ns: list[int] = []
        #: pending (callable, start_ns) per c_call awaiting its c_return
        self.pending_c: list[tuple[Any, int]] = []


class ProfileTracer(Tracer):
    """``sys.setprofile``-based tracer with optional C-call attribution.

    Emits ``call``/``return`` for Python frames, plus ``c_call``/``c_return``/
    ``c_exception`` for C functions.  Each event carries ``duration_ns`` in
    ``extra`` when its partner is known, so you can build a flat inclusive-time
    profile without post-processing.

    Parameters
    ----------
    config:
        Defaults to events ``(call, return)``.
    trace_c:
        Include C-call events.  These are *enormously* higher volume than
        Python call events in numeric or I/O code (``d[k]`` alone can be three
        C calls), so they are opt-in.
    """

    def __init__(
        self,
        config: TracerConfig | None = None,
        *,
        trace_c: bool = False,
        **overrides: Any,
    ) -> None:
        super().__init__(config, **overrides)
        self.trace_c = trace_c
        self._tls = _PTls()

    # ------------------------------------------------------------------
    def _should(self, code: Any) -> bool:
        """Framecraft's own frames are excluded; see :func:`codeinfo.is_internal`."""
        return not codeinfo.is_internal(code) and self.config.admits(code)

    def _profile(self, frame: FrameType, event: str, arg: Any) -> None:
        try:
            if event == "call":
                code = frame.f_code
                if not self._should(code):
                    # The hook still fires for every Python call; we just do
                    # nothing.  There is no supported way to suppress tracing
                    # for a single frame with setprofile (unlike settrace's
                    # global-function-returns-None trick) -- that asymmetry is
                    # the main reason setprofile is still around but rarely
                    # used alone.
                    return
                import time

                call_id = self._next_seq()
                self._tls.stack.append(frame)
                self._tls.entered_ns.append(time.perf_counter_ns())
                if self.config.wants(CALL):
                    self._guard(
                        lambda: self._emit(
                            self._make_event(
                                CALL,
                                frame,
                                call_id=call_id,
                                depth=len(self._tls.stack) - 1,
                            )
                        )
                    )
                return

            if event == "return":
                if not self._tls.stack:
                    return
                popped = self._tls.stack.pop()
                start = self._tls.entered_ns.pop()
                if popped is frame and self.config.wants(RETURN):
                    import time

                    dur = time.perf_counter_ns() - start
                    self._guard(
                        lambda: self._emit(
                            self._make_event(
                                RETURN,
                                frame,
                                depth=max(0, len(self._tls.stack)),
                                retval_repr=safe_repr(
                                    arg, policy=self.config.policy, redactor=self.config.redactor
                                )
                                if self.config.capture_return
                                else None,
                                extra={"duration_ns": dur},
                            )
                        )
                    )
                return

            if not self.trace_c:
                return

            if event == "c_call":
                import time

                # Attribute to the innermost live Python frame: this is the
                # line-number answer that setprofile cannot give us natively.
                owner = self._tls.stack[-1] if self._tls.stack else None
                self._tls.pending_c.append((arg, time.perf_counter_ns()))
                if self.config.wants(C_CALL) and owner is not None:
                    self._guard(
                        lambda a=arg, o=owner: self._emit(
                            self._make_event(
                                C_CALL,
                                o,
                                code=o.f_code,
                                depth=len(self._tls.stack) - 1,
                                c_name=_c_name(a),
                                arg_repr=_c_args(a, self.config),
                            )
                        )
                    )
                return

            if event == "c_return":
                if not self.config.wants(C_RETURN):
                    return
                owner = self._tls.stack[-1] if self._tls.stack else None
                if owner is None:
                    return
                fn, start = self._tls.pending_c.pop() if self._tls.pending_c else (None, 0)
                import time

                dur = time.perf_counter_ns() - start
                self._guard(
                    lambda: self._emit(
                        self._make_event(
                            C_RETURN,
                            owner,
                            code=owner.f_code,
                            depth=len(self._tls.stack) - 1,
                            c_name=_c_name(fn) if fn is not None else None,
                            retval_repr=safe_repr(
                                arg, policy=self.config.policy, redactor=self.config.redactor
                            )
                            if self.config.capture_return
                            else None,
                            extra={"duration_ns": dur},
                        )
                    )
                )
                return

            if event == "c_exception":
                if not self.config.wants(C_EXCEPTION):
                    return
                owner = self._tls.stack[-1] if self._tls.stack else None
                if owner is None:
                    return
                self._guard(
                    lambda: self._emit(
                        self._make_event(
                            C_EXCEPTION,
                            owner,
                            code=owner.f_code,
                            depth=len(self._tls.stack) - 1,
                            c_name="<c_exception>",
                            exception_type=type(arg).__qualname__,
                            exception_repr=safe_repr(
                                arg, policy=self.config.policy, redactor=self.config.redactor
                            ),
                        )
                    )
                )
        except BaseException as exc:
            self.error_count += 1
            if self.config.on_error is not None:
                try:
                    self.config.on_error(exc)
                except BaseException:
                    pass

    # ------------------------------------------------------------------
    def _start(self) -> None:
        if self.config.include_threads:
            self._prev_thread_trace = threading.getprofile()
            threading.setprofile(self._profile)
        sys.setprofile(self._profile)

    def _stop(self) -> None:
        sys.setprofile(None)
        self._tls.stack.clear()
        self._tls.entered_ns.clear()
        self._tls.pending_c.clear()


def _c_name(fn: Any) -> str:
    """A readable name for a C function ``arg`` from a ``c_call`` event.

    ``arg`` is the callable itself.  For builtins that means the builtin
    function object (``<built-in function len>``); for ``__init__`` style slots
    it can be a bound method-wrapper.  We normalise to something you can group
    on.
    """
    name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None)
    if name:
        return str(name)
    return repr(fn)


def _c_args(fn: Any, config: TracerConfig) -> str | None:
    """Best-effort argument rendering for a ``c_call``.

    Builtin functions do not expose their arguments at all -- there is no
    frame, no locals, nothing.  ``sys.setprofile`` simply does not give you
    this.  If you need C-call arguments you must use PEP 669, whose ``C_CALL``
    callback receives ``arg0`` explicitly.
    """
    return None
