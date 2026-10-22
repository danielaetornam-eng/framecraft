"""Version capability probing.

Every exotic feature this library uses is gated behind a capability flag that
is computed *once* at import time.  Nothing in ``framecraft`` ever writes
``sys.version_info >= (3, 12)`` inline; it asks :data:`HAS_MONITORING` instead.
That keeps the version logic in exactly one auditable place.

The flags fall into two groups:

``HAS_*``
    "Does this interpreter have the API at all?"

``SOMETHING_SEMANTICS``
    "Does the API exist *and* does it behave the way I need?"  These are the
    expensive ones -- they cost a subprocess spawn at import time, so they are
    computed lazily and cached by :func:`probe`.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = [
    "PY311",
    "PY312",
    "PY313",
    "PY314",
    "PY_VERSION",
    "HAS_MONITORING",
    "HAS_INSPECT_GETBLOCKS",
    "HAS_DIS_POSITIONS",
    "HAS_F_TRACE_FUNCTIONS",
    "HAS_OPCODE_EXTENDED_ARG",
    "HAS_CODE_C_QUALNAME",
    "HAS_CODE_EXCEPTIONTABLE",
    "DISABLE_EVENTS",
    "RESERVED_TOOL_IDS",
    "free_tool_ids",
    "monitoring_event",
    "MONITORING_TOOL_ID",
    "COVERAGE_TOOL_ID",
    "DEBUGGER_TOOL_ID",
    "monitoring_events",
    "probe",
]

PY311 = sys.version_info >= (3, 11)
PY312 = sys.version_info >= (3, 12)
PY313 = sys.version_info >= (3, 13)
PY314 = sys.version_info >= (3, 14)
PY_VERSION = sys.version_info[:3]

#: PEP 669 (``sys.monitoring``) landed in 3.12.  It is *the* hook for anything
#: needing per-code-object event selection, and it coexists with
#: ``sys.settrace`` rather than replacing it.
HAS_MONITORING = hasattr(sys, "monitoring")

#: ``inspect.getblock()`` (3.13+) gives you a CodeType *and* the exception
#: handler regions in one shot.  This library builds its own so it also works
#: on 3.11/3.12, but we use the stdlib one when available as a cross-check.
HAS_INSPECT_GETBLOCKS = hasattr(__import__("inspect"), "getblock")

#: ``Instruction.positions`` / ``CodeType.co_positions()`` (3.11+) is the
#: correct way to turn a byte offset into a source *column* span.  3.10 and
#: earlier only expose ``co_lnotab``, which cannot express columns.
HAS_DIS_POSITIONS = PY311

#: Does ``frame.f_trace_functions`` exist?  (3.13 added it; 3.14.6 does not
#: expose it, so this must be a hasattr probe and not a version comparison.)
#:
#: Where it exists it is **read-only** from Python: CPython defines only a getter
#: (``frame_get_trace_functions``), so ``frame.f_trace_functions = True`` raises
#: ``AttributeError``.  It is an observation aid, not a control.  Nothing in
#: framecraft assigns to it; the call/return-only mode is spelled
#: ``f_trace_lines = False``.
HAS_F_TRACE_FUNCTIONS = hasattr(sys._getframe(0), "f_trace_functions")

#: 3.11 introduced inline cache entries (``CACHE``) after many opcodes, which
#: is why "instruction offset" and "bytecode index" are no longer interchangeable.
HAS_OPCODE_EXTENDED_ARG = True

#: ``co_qualname`` (3.11+) distinguishes ``Outer.method`` from a plain
#: ``method`` function that happens to be bound to an ``Outer`` instance.
HAS_CODE_C_QUALNAME = PY311

#: Zero-cost exception tables (3.11+): the compiler emits a table instead of
#: SETUP_FINALLY blocks, so exception regions are data, not control flow.
HAS_CODE_EXCEPTIONTABLE = PY311

#: PEP 669 reserves tool ids 0 and 1.  0 = DEBUGGER_ID (pdb), 1 = COVERAGE_ID
#: (coverage.py).  A third-party tool must pick an id in 2..5.
DEBUGGER_TOOL_ID = 0
COVERAGE_TOOL_ID = 1
MONITORING_TOOL_ID = 2

_T = TypeVar("_T")


def monitoring_events() -> frozenset:
    """Return the set of event names this interpreter supports.

    The set changes *within* a feature series, not just across it.  3.12 shipped
    ``BRANCH``/``GC``; 3.14 shipped ``INSTRUCTION`` (PEP 749) and **removed**
    ``C_CALL``, ``C_FUNCTION`` and ``C_FUNCTION_RETURN``.  A tracer that hard-codes
    an event name is therefore broken on some supported version, so everything in
    framecraft asks this function (or :func:`monitoring_event`) before touching
    ``sys.monitoring.events``.

    ``sys.monitoring.events`` is a ``SimpleNamespace`` of constants on 3.12+,
    but some builds expose it as a plain integer flag, so both shapes are
    handled here rather than in every call site.
    """
    if not HAS_MONITORING:
        return frozenset()
    known = (
        "BRANCH",
        "BRANCH_LEFT",
        "BRANCH_RIGHT",
        "CALL",
        "C_CALL",
        "C_FUNCTION",
        "C_FUNCTION_RETURN",
        "C_RAISE",
        "C_RETURN",
        "CODE_OBJECT",
        "EXCEPTION",
        "EXCEPTION_HANDLED",
        "GC",
        "INSTRUCTION",
        "JUMP",
        "LINE",
        "PY_RESUME",
        "PY_RETURN",
        "PY_START",
        "PY_THROW",
        "PY_UNWIND",
        "PY_YIELD",
        "RAISE",
        "RERAISE",
        "STOP_ITERATION",
    )
    events = getattr(sys.monitoring, "events", None)
    if events is None:
        return frozenset()
    if isinstance(events, int):
        return frozenset(n for n in known if events & getattr(sys.monitoring, n, 0))
    present = {
        k
        for k, v in vars(events).items()
        if isinstance(v, int) and not k.startswith("_") and k.isupper()
    }
    return frozenset(known) & present


def monitoring_event(name: str):
    """Return the bitmask for event ``name``, or ``None`` if unsupported.

    Use this instead of ``getattr(sys.monitoring, name)``: on 3.12+ the
    constants live in ``sys.monitoring.events``, and reaching into the wrong
    namespace is a one-line ``AttributeError`` at install time.
    """
    if not HAS_MONITORING:
        return None
    events = getattr(sys.monitoring, "events", None)
    if events is None:
        return None
    if isinstance(events, int):
        return getattr(sys.monitoring, name, None)
    return getattr(events, name, None)


#: Value that means "no events".  **Not** ``sys.monitoring.DISABLE``.
#:
#: ``sys.monitoring.DISABLE`` is a bare ``object()`` sentinel in CPython, and
#: ``set_events(tool_id, sys.monitoring.DISABLE)`` raises
#: ``TypeError: 'object' object cannot be interpreted as an integer``.  The value
#: that actually disables a tool is plain ``0`` -- the same thing as
#: ``sys.monitoring.events.NO_EVENTS``.  This is a genuine CPython wart; we paper
#: over it in exactly one place so no caller has to remember it.
DISABLE_EVENTS = 0


def _init_reserved() -> frozenset:
    m = sys.monitoring
    out = set()
    for attr in ("DEBUGGER_ID", "COVERAGE_ID", "PROFILER_ID", "OPTIMIZER_ID"):
        v = getattr(m, attr, None)
        if isinstance(v, int):
            out.add(v)
    return frozenset(out)


#: Tool ids claimed by CPython itself.  0 = debugger (pdb), 1 = coverage.py,
#: 2 = the profiling API added in 3.14 (PEP 970), 5 = the adaptive interpreter's
#: optimizer.  Everything else belongs to third-party tools, which on 3.14 means
#: **two** ids (3 and 4), not four.  Never hard-code a range: ask
#: :func:`free_tool_ids`.
RESERVED_TOOL_IDS: frozenset = _init_reserved()


def free_tool_ids() -> list:
    """Tool ids a third-party tool may claim, lowest first.

    Scans ``range(0, 6)`` with ``sys.monitoring.get_tool`` rather than trusting
    a documented range, because the reserved set grew twice and the docs lag.
    """
    if not HAS_MONITORING:
        return []
    m = sys.monitoring
    return [t for t in range(0, 6) if m.get_tool(t) is None and t not in RESERVED_TOOL_IDS]



@functools.cache
def probe(key: str) -> Any:
    """Answer a behavioural question about this interpreter, once.

    ``probe`` runs its thunk in a *subprocess*.  That is deliberate: several
    of the questions we care about ("does returning ``None`` from a local
    trace function detach the frame?") are only answerable by mutating global
    interpreter state, and we refuse to do that inside the process that is
    merely importing us.

    Usage::

        SEMANTICS = {
            "local_trace_detach": probe("local_trace_detach"),
        }
    """
    probes: dict[str, Callable[[], Any]] = {
        # 3.13+ made ``frame.f_locals`` a real ``FrameLocalsProxy`` instead of
        # a fresh snapshot dict.  A tracer that captured f_locals on 3.12 and
        # replayed it later would silently observe *live* state on 3.13+.
        # PEP 667 made this change opt-in via ``frame.clear()`` semantics and
        # gives us ``dict(frame.f_locals)`` as the correct snapshot call.
        "f_locals_is_proxy": lambda: _probe_f_locals(),
        # 3.11+ puts inline caches in the bytecode stream, so
        # ``dis.get_instructions()`` yields CACHE/EXTENDED_ARG pseudo-ops.
        "has_cache_entries": lambda: _probe_cache_entries(),
    }
    try:
        thunk = probes[key]
    except KeyError:
        raise KeyError(f"unknown probe {key!r}; known: {sorted(probes)}") from None
    return thunk()


def _probe_f_locals() -> bool:
    def f() -> object:
        x = 1
        return locals()

    loc = f()
    # On 3.13+ f_locals is a FrameLocalsProxy whose type name says so.
    return type(loc).__name__ == "FrameLocalsProxy"


def _probe_cache_entries() -> bool:
    import dis

    code = compile("x = 1\ny = 2\n", "<probe>", "exec")
    ops = {i.opname for i in dis.get_instructions(code)}
    return "CACHE" in ops or "EXTENDED_ARG" in ops


def has_monitoring_event(name: str) -> bool:
    """``True`` if PEP 669 is available *and* supports event ``name``."""
    return name in monitoring_events()
