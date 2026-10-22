"""Public frame introspection: the read-only half of framecraft.

Nothing here installs a hook or allocates a tracer.  This module is the
"look at what is on the stack right now" API, and it is the correct foundation
for any tracer because it forces you to confront every sharp edge of frame
access exactly once:

*   which clock to read,
*   how to walk ``f_back`` without ``AttributeError`` at the bottom,
*   why ``f_locals`` is not a plain dict,
*   why the instruction pointer is a *byte* offset,
*   what to do about generator/coroutine frames that are suspended.

If a tracer feature ever surprises you, the answer is nearly always here.
"""

from __future__ import annotations

import sys
import time
import types
from collections.abc import Iterable, Iterator, Sequence
from types import FrameType
from typing import Any

from framecraft._internal import codeinfo
from framecraft._internal.compat import HAS_DIS_POSITIONS
from framecraft._internal.values import (
    DEFAULT_POLICY,
    DEFAULT_REDACTOR,
    CapturePolicy,
    Redactor,
    capture_value,
    safe_repr,
)
from framecraft.core.events import LocalBinding, StackFrame

__all__ = [
    "caller_frame",
    "current_frame",
    "walk",
    "stack_snapshot",
    "render_stack",
    "describe",
    "capture_locals",
    "resolve_name",
    "scope_of",
    "frame_depth",
    "is_suspended_frame",
    "suspended_frames",
    "clocks",
]


# --------------------------------------------------------------------------
# clocks
# --------------------------------------------------------------------------


def clocks(*, with_cpu: bool = False) -> dict[str, Any]:
    """Read the tracer's clocks in one call.

    This function is on the hottest path in the library -- it runs once per
    emitted event -- so the obvious implementation is *wrong*:

    .. code-block:: python

        return {
            "wall": time.perf_counter(),
            "cpu": time.process_time(),
            "perf_ns": time.perf_counter_ns(),
            "thread_id": threading.get_ident(),
            "thread_name": threading.current_thread().name,
        }

    Measured on CPython 3.14 / Windows, per call:

    ===========================  ========
    operation                    ns
    ===========================  ========
    ``time.perf_counter_ns()``      202
    ``time.perf_counter()``         190
    ``time.process_time()``        684   <-- Windows queries the OS
    ``threading.current_thread()``  324
    ``threading.get_ident()``       169
    ===========================  ========

    That is ~1570 ns of bookkeeping before a single event object exists, which
    dwarfs everything else a tracer does.  Two fixes:

    *   **Derive ``wall`` from ``perf_ns``.**  ``perf_counter_ns()`` and
        ``perf_counter()`` read the *same* clock at different resolutions, so
        ``wall = perf_ns / 1e9`` is the same value with no extra syscall-class
        call.  (They are not bit-identical: the two reads are separated in time
        by up to ~600 ns.  Since ``wall`` is only ever used for human-readable
        durations, and every *interval* computation uses ``perf_ns`` so that
        arithmetic is exact integer arithmetic, this costs nothing measurable.)

    *   **Make ``cpu`` opt-in.**  ``process_time()`` is 3.4x the cost of
        ``perf_counter_ns()`` on Windows and buys you one extra distinction
        ("waiting" vs "computing").  Under the GIL that distinction rarely
        changes an answer, so it is ``with_cpu=True`` rather than the default.
        Turn it on for a thread that blocks on I/O.

    *   ``thread_id``/``thread_name`` are cached per thread by
        :meth:`Tracer._thread_info`, not read here.
    """
    perf_ns = time.perf_counter_ns()
    out: dict[str, Any] = {
        "perf_ns": perf_ns,
        # Derived, not re-read: same clock, lower resolution, no extra call.
        "wall": perf_ns / 1e9,
        "cpu": time.process_time() if with_cpu else 0.0,
    }
    return out


# --------------------------------------------------------------------------
# walking
# --------------------------------------------------------------------------


def current_frame(depth: int = 0) -> FrameType | None:
    """The frame ``depth`` levels above the caller of this function.

    Identical to ``caller_frame(depth)``.  It exists because ``sys._getframe``
    is called ``_getframe`` and the stdlib wrapper for it reads as
    "the current frame", which it is not -- it is always somebody else's.
    """
    return caller_frame(depth)


def caller_frame(skip: int = 0) -> FrameType | None:
    """Your caller's frame.  ``skip=1`` means *its* caller, and so on.

    ``skip=0`` -- the default -- is the frame you almost always want: the one
    that called you.  This is deliberately the same as :func:`current_frame(0)`;
    the two names exist because both read better in different places, and having
    them disagree about what "zero" means is a bug factory.

    Returns ``None`` instead of raising ``ValueError`` when you walk off the top
    of the stack, so probe-style callers degrade rather than explode.

    .. code-block:: python

        def handler(x):
            frame = caller_frame()      # handler's caller
            scope_of(frame, "config")   # 'global' -- found in *its* globals
    """
    try:
        return sys._getframe(skip + 1)
    except ValueError:
        return None


def walk(start: FrameType | None = None, *, max_depth: int | None = None) -> Iterator[FrameType]:
    """Yield frames from ``start`` (or the caller) up to the root.

    Yields index 0 = innermost.  Stops cleanly at the bottom of the stack where
    ``f_back is None``.
    """
    # skip=1, not 0: `caller_frame(0)` is *this* function's caller -- which for
    # `walk` is `walk` itself, because walk calls it.  walk wants the caller of
    # walk, so it must ask for one level higher.  Getting this wrong is silent:
    # you get a stack that starts one frame too deep and every depth is off by
    # one, which looks like correct output.
    f = start if start is not None else caller_frame(1)
    n = 0
    while f is not None:
        yield f
        f = f.f_back
        n += 1
        if max_depth is not None and n >= max_depth:
            return


def frame_depth() -> int:
    """How many Python frames are on the stack right now.

    Cheap enough to call from a hot hook.  Useful as a first assertion in any
    tool: if this surprises you, your understanding of the stack is wrong and
    every frame below is suspect.
    """
    return sum(1 for _ in walk())


def stack_snapshot(
    *,
    include_locals: bool = False,
    include_source: bool = False,
    max_frames: int | None = None,
    policy: CapturePolicy = DEFAULT_POLICY,
    redactor: Redactor = DEFAULT_REDACTOR,
) -> list[StackFrame]:
    """Copy the current stack into plain dataclasses.

    Once you have this list you are safe from frames popping out from under
    you.  That is the whole point: a *debugger* wants the live frames, but a
    *logger* wants the snapshot.
    """
    out: list[StackFrame] = []
    for i, f in enumerate(walk()):
        if max_frames is not None and i >= max_frames:
            break
        sf = StackFrame.from_frame(f, depth=i)
        if include_locals:
            sf.locals = capture_locals(f, policy=policy, redactor=redactor)
        if include_source:
            try:
                sf.extra = {}  # placeholder kept for future source capture
            except Exception:  # pragma: no cover
                sf.extra = {}
        out.append(sf)
    return out


def render_stack(*, max_frames: int | None = 12, marker: str = "->") -> str:
    """A ``traceback``-lookalike rendering of the current stack.

    Note the direction: frame 0 is *innermost* and is printed **last**, exactly
    like ``traceback.print_stack()``.  Tracers that print stacks in the wrong
    order are the single most common source of confusion in debugging tools,
    because the reader's instinct is to read top-down as "call order" when it
    is actually the reverse.
    """
    frames = list(walk())
    if max_frames is not None and len(frames) > max_frames:
        frames = frames[:max_frames]
    lines: list[str] = []
    for f in reversed(frames):
        code = f.f_code
        head = f"  File \"{code.co_filename}\", line {f.f_lineno}, in {codeinfo.qualified_name(code)}"
        lines.append(head)
        if code.co_name == "<module>":
            lines.append(f"    {marker} <module>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# namespaces
# --------------------------------------------------------------------------


def scope_of(frame: FrameType, name: str) -> str:
    """Classify ``name`` as seen from ``frame``: local/free/global/builtin.

    This mirrors CPython's own name resolution order, and it is the reason
    ``scope_of`` is not just ``"local" if name in frame.f_locals else ...``:

    1.  ``co_varnames`` -- the fast-locals array.  Includes
        compiler-allocated slots you cannot see in ``f_locals``.
    2.  ``co_cellvars``/``co_freevars`` -- closure cells.  Their *names* are
        visible in ``f_locals`` but they live in ``f_localsplus`` beyond the
        varnames.
    3.  ``f_globals`` then ``f_builtins`` -- the only two namespaces that fall
        back to a dictionary lookup.

    The distinction between (1) and (2) matters when writing an execution
    recorder that needs to know *which storage slot* to write into if it ever
    wanted to implement a debugger's "set variable" command.
    """
    code = frame.f_code
    if name in code.co_varnames:
        return "local"
    if name in code.co_cellvars or name in code.co_freevars:
        return "free"
    if name in frame.f_globals:
        return "global"
    try:
        import builtins

        if name in builtins.__dict__:
            return "builtin"
    except Exception:  # pragma: no cover
        pass
    return "unknown"


def resolve_name(frame: FrameType, name: str) -> tuple[str, Any]:
    """Resolve ``name`` in ``frame``'s lexical chain, returning ``(scope, value)``.

    Looks the name up in exactly the order Python's ``LOAD_NAME`` would.  Note
    it does **not** implement the full semantics of ``LOAD_NAME``: there is no
    ``__builtins__`` dict injected into frames whose code has no globals dict,
    and there is no ``getattr`` fallback.  For tracing purposes that is
    irrelevant -- if a name is not in the four places, it does not exist yet.
    """
    scope = scope_of(frame, name)
    if scope in ("local", "free"):
        # CPython's PyFrame_FastToLocalsWithError copies cellvars *and*
        # freevars into the f_locals dict under their own names, so both are
        # reachable here.  They are not reachable from co_varnames alone,
        # which is the usual source of "my tracer misses closure variables".
        return scope, frame.f_locals.get(name)
    if scope == "global":
        return scope, frame.f_globals.get(name)
    if scope == "builtin":
        import builtins

        return scope, getattr(builtins, name, None)
    return scope, None


def capture_locals(
    frame: FrameType,
    *,
    policy: CapturePolicy = DEFAULT_POLICY,
    redactor: Redactor = DEFAULT_REDACTOR,
    only: Iterable[str] | None = None,
    structured: bool = True,
) -> list[LocalBinding]:
    """Snapshot the local namespace of ``frame``, safely.

    What you get depends on the frame, and the difference is not a detail:

    *   **Function frame.**  ``f_locals`` holds the *fast locals only*: the
        parameters and the variables assigned in the body.  Globals and builtins
        are **not** in it -- they live in ``f_globals`` and ``f_builtins``.  So
        a function frame yields exactly its locals, and a tool that claims to
        have captured a function's whole namespace from ``f_locals`` alone is
        wrong.
    *   **Module frame.**  ``f_locals`` *is* ``f_globals``, so a module frame
        yields every global the module has ever defined -- which is why
        capturing one on every line event is the single most expensive thing a
        tracer can do.  Note that ``co_varnames`` is **empty** for a module code
        object: module-level names are never "fast locals", they are globals.
        That is why the second pass below exists at all, and why a tracer that
        iterates ``co_varnames`` alone sees an empty module frame and concludes
        there is nothing to capture.

    Two things this adds over iterating ``f_locals`` yourself:

    *   **Scope classification.** Names found in ``f_locals`` but absent from
        ``co_varnames`` are not fast locals -- they are globals (module frames)
        or names injected by ``exec``.  Each is labelled, so a reader can tell a
        real local from a global that leaked into the capture.
    *   **Redaction.** Handled here rather than in the sink, so that *every* sink
        inherits it.  A sink-level redactor is a redactor you will forget.

    ``only`` restricts the result to the named bindings, and accepts names from
    any scope.
    """
    code = frame.f_code
    varnames = code.co_varnames
    out: list[LocalBinding] = []
    seen: set[str] = set()

    wanted: Sequence[str] | None = None
    if only is not None:
        wanted = list(only)
        wanted_set = set(wanted)
        varnames = [n for n in varnames if n in wanted_set]
        # `only` may name freevars or cells too, which are not in co_varnames.
        varnames = list(varnames) + [n for n in wanted if n not in varnames]

    # Reading f_locals is the expensive part, so exactly once per capture.
    snapshot = frame.f_locals

    for name in varnames:
        if name in seen:
            continue
        seen.add(name)
        if name.startswith(".0") or name.startswith("."):
            # Compiler temporaries: `.0` is the bare-except target, `.1`/`.2`
            # etc. come from `with`/`async with` and exception temporaries.
            # Recording them is pure noise, but *counting* them is useful, so we
            # expose them via describe() rather than capture_locals().
            continue
        if name not in snapshot:
            continue
        value = snapshot[name]
        out.append(
            LocalBinding(
                name=name,
                value=safe_repr(value, name=name, policy=policy, redactor=redactor),
                structured=capture_value(value, name=name, policy=policy, redactor=redactor) if structured else None,
                scope=scope_of(frame, name),
            )
        )

    # Names present in f_locals but not in co_varnames: injected by exec(), or
    # (module frames) globals.  Mark them so the reader knows they are not real
    # fast-locals.
    if only is None:
        limit = policy.max_items * 3
        for name, value in snapshot.items():
            if name in seen or name.startswith("__"):
                continue
            scope = scope_of(frame, name)
            if scope == "local":
                continue
            if len(out) >= limit:
                break
            out.append(
                LocalBinding(
                    name=name,
                    value=safe_repr(value, name=name, policy=policy, redactor=redactor),
                    structured=capture_value(value, name=name, policy=policy, redactor=redactor) if structured else None,
                    scope=scope,
                )
            )
    return out


# --------------------------------------------------------------------------
# suspended frames
# --------------------------------------------------------------------------


def _pos_attr(pos: Any, *names: str) -> int | None:
    """First present attribute among ``names`` on ``dis.Positions``, else None.

    3.14 renamed ``Positions.col`` to ``col_offset`` and ``end_col`` to
    ``end_col_offset`` with no deprecation alias.  Centralising the lookup here
    means one place to update when 3.15 renames something else.
    """
    if pos is None:
        return None
    for n in names:
        v = getattr(pos, n, None)
        if v is not None:
            return v
    return None


def is_suspended_frame(frame: FrameType) -> bool:
    """Is this frame parked at a ``yield``/``await``?

    True for generator frames, coroutine frames, and async-generator frames.
    A suspended frame is a *real* frame on *no* stack: it is reachable only
    through the generator/coroutine object that owns it.  This is why "why can't
    my tracer see inside my generator?" is a common question -- it *is* on the
    stack, just not the C-level stack the tracer walks.
    """
    code = frame.f_code
    flags = code.co_flags
    return bool(flags & (0x20 | 0x80 | 0x200))  # GENERATOR | COROUTINE | ASYNC_GENERATOR


def suspended_frames() -> list:
    """Every suspended (generator/coroutine/async-generator) frame in the heap.

    This is a genuinely useful -- and genuinely alarming -- trick.  It finds
    generators nobody holds a reference to *through the stack*, because it goes
    through the garbage collector instead.  It is how you debug "this task is
    awaiting a lock nobody will ever release": no traceback can show you that,
    because the coroutine is not on any stack.

    Implementation note, because the obvious version does not work: you cannot
    look for frame objects in ``gc.get_objects()``.  A frame is only itself
    GC-tracked when it happens to hold references to other tracked objects, so
    the frames you most want to find -- small ones -- are usually invisible.
    What *is* tracked is the generator/coroutine object, and the frame hangs off
    that.  So we collect owners and read ``gi_frame`` / ``cr_frame`` /
    ``ag_frame``.

    Scope: **only suspended frames are returned.**  Frames that are currently
    executing are on a real stack and ``sys._current_frames()`` already finds
    them; frames that have returned are gone.  There is no flag to widen this,
    because "find everything on the heap" is what ``gc.get_objects()`` is for
    and duplicating it here would be slower and less honest.

    Caveats, stated plainly: ``gc.get_objects()`` is O(heap) and perturbs
    reference counts.  It belongs in a debugging tool, never in a hot path.
    """
    import gc

    found: list = []
    seen: set = set()

    for obj in gc.get_objects():
        # NOTE the narrow try.  An earlier version wrapped the whole loop body in
        # `except BaseException: continue`, which silently swallowed a NameError
        # caused by a missing import and made the function return an empty list
        # forever.  `gc.get_objects()` yields arbitrary objects, so *attribute
        # access* can fail; nothing else may.
        frame = None
        try:
            if isinstance(obj, types.GeneratorType):
                frame = obj.gi_frame
            elif isinstance(obj, types.CoroutineType):
                frame = obj.cr_frame
            elif isinstance(obj, types.AsyncGeneratorType):
                frame = obj.ag_frame
        except BaseException:
            continue
        if frame is None or id(frame) in seen:
            continue
        if _is_currently_running(frame):
            continue
        seen.add(id(frame))
        found.append(frame)
    return found


def _is_currently_running(frame: FrameType) -> bool:
    """True if ``frame`` is on some thread's live stack right now."""
    for top in sys._current_frames().values():
        f = top
        while f is not None:
            if f is frame:
                return True
            f = f.f_back
    return False


# --------------------------------------------------------------------------
# full description
# --------------------------------------------------------------------------


def describe(frame: FrameType | None = None, *, include_locals: bool = True) -> dict[str, Any]:
    """Everything worth knowing about one frame, as a nested dict.

    This is the function the interactive REPL examples use to "open the hood"
    on a frame, and the function behind ``fc-trace inspect``.  It is
    intentionally exhaustive: it is the reference implementation of "what does
    a frame expose", and the docs quote its output verbatim.
    """
    if frame is None:
        frame = caller_frame()
        if frame is None:
            return {"error": "no frame"}

    code = frame.f_code
    out: dict[str, Any] = {
        "code": codeinfo.describe_code(code),
        "instruction_pointer": {
            "f_lasti": frame.f_lasti,
            "f_lineno": frame.f_lineno,
            "line_at_lasti": codeinfo.offset_to_line(code, frame.f_lasti),
        },
        "namespace_sizes": {
            "locals": len(frame.f_locals),
            "globals": len(frame.f_globals),
            "builtins": len(frame.f_builtins),
        },
        "trace_hooks": {
            "f_trace": frame.f_trace is not None,
            "f_trace_lines": getattr(frame, "f_trace_lines", None),
            "f_trace_opcodes": getattr(frame, "f_trace_opcodes", None),
            "f_trace_functions": getattr(frame, "f_trace_functions", None),
        },
        "links": {
            "has_back": frame.f_back is not None,
            "back_name": frame.f_back.f_code.co_name if frame.f_back is not None else None,
            "depth_from_here": sum(1 for _ in walk(frame)),
        },
        "closure": {
            "freevars": list(code.co_freevars),
            "cellvars": list(code.co_cellvars),
        },
        "exceptions": {
            "is_generator": bool(code.co_flags & 0x20),
            "is_coroutine": bool(code.co_flags & 0x80),
            "is_async_generator": bool(code.co_flags & 0x200),
        },
    }

    # Locals.  Built by name lookup against co_varnames rather than by
    # iterating f_locals, so the *order* matches the compiler's slot order and
    # slot index == index in this list.  That is a genuinely useful invariant for
    # anyone correlating with f_localsplus.
    if include_locals:
        snapshot = frame.f_locals
        rows = []
        for idx, name in enumerate(code.co_varnames):
            if name not in snapshot:
                continue
            rows.append(
                {
                    "slot": idx,
                    "name": name,
                    "scope": scope_of(frame, name),
                    "value": safe_repr(snapshot[name], name=name),
                    "structured": capture_value(snapshot[name], name=name),
                }
            )
        out["locals"] = rows

    # Line table.  This is the ground truth that `dis` renders as the margin.
    if HAS_DIS_POSITIONS:
        out["line_table"] = [
            {"start": s, "end": e, "line": ln} for s, e, ln in code.co_lines()
        ]
        out["exception_table"] = codeinfo.exception_table(code)
        sample_instructions = []
        for ins in __import__("dis").get_instructions(code):
            if ins.opname in ("CACHE", "EXTENDED_ARG"):
                continue
            pos = getattr(ins, "positions", None)
            # `dis.Positions` was renamed in 3.14: `col`/`end_col` became
            # `col_offset`/`end_col_offset`.  There is no deprecation shim, so a
            # single hard-coded attribute name breaks on exactly one side of
            # 3.13/3.14.  Probe both, once, via getattr with a default.
            col = _pos_attr(pos, "col", "col_offset")
            end_col = _pos_attr(pos, "end_col", "end_col_offset")
            sample_instructions.append(
                {
                    "offset": ins.offset,
                    "opname": ins.opname,
                    "arg": ins.arg,
                    "argrepr": ins.argrepr,
                    # 3.13 added `Instruction.line_number`; on 3.11/3.12 the
                    # line is only on the first instruction of a line.
                    "line": pos.lineno if pos and pos.lineno is not None
                    else getattr(ins, "line_number", ins.starts_line),
                    "col": col,
                    "end_col": end_col,
                }
            )
        out["instructions"] = sample_instructions
    return out
