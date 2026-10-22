"""Low-level frame and code-object reflection.

This module is the "no abstractions" layer.  If a function here has a docstring
that explains a CPython detail rather than a policy decision, it belongs here.

The three ideas you must internalise to read the rest of the codebase:

1.  A **code object** is the *blueprint*: filename, name, line table, constants,
    names, varnames, freevars.  It is immutable and is shared by every
    activation of the function (and by every closure over it).
2.  A **frame** is one *activation* of a code object: it adds the instruction
    pointer (``f_lasti``), the local namespace, the closure cells, and a link to
    the caller (``f_back``).
3.  ``code -> frame`` is a many-to-one relation.  This single fact is the source
    of most tracer bugs: if you key state on the code object you will confuse
    two concurrent calls of the same function; if you key state on
    ``id(frame)`` you will be surprised when ids get recycled.
"""

from __future__ import annotations

import dis
import sys
from collections.abc import Iterator, Sequence
from types import CodeType, FrameType
from typing import Any

__all__ = [
    "describe_code",
    "code_id",
    "qualified_name",
    "current_offset",
    "offset_to_line",
    "line_at_offset",
    "instructions_at_line",
    "exception_table",
    "code_hash",
]

# Cache keyed on the code object identity.  Code objects are immutable and
# live as long as the function they belong to, so this can never go stale.
_CODE_CACHE: dict[CodeType, dict[str, Any]] = {}


def _pkg_dir(*parts: str) -> str:
    import os

    here = os.path.dirname(os.path.abspath(__file__))  # .../framecraft/_internal
    return os.path.join(os.path.dirname(here), *parts).replace("\\", "/") + "/"


#: Directories whose frames must never be traced.
#:
#: Note this is a *narrow* list, not "the whole framecraft package".  The reason
#: to exclude anything at all is that the hook's own frames would otherwise
#: dominate the tail of every trace: ``__exit__ -> stop -> _stop ->
#: _detach_all`` are the innermost frames at the moment tracing is torn down.
#: But excluding the whole package would also hide genuinely interesting user
#: code -- ``examples/`` and the CLI's own workload live inside the tree during
#: development, and silently not tracing them is worse than the noise we were
#: removing.
#:
#: Sinks and analysis code are deliberately *not* excluded.  They only ever run
#: from inside the hook, and CPython disables tracing while a trace function
#: executes, so they cannot be traced anyway.
_OWN_DIRS: tuple[str, ...] = (
    _pkg_dir("_internal"),
    _pkg_dir("core"),
    _pkg_dir("trace"),
)


def is_internal(code: CodeType) -> bool:
    """True if ``code`` is part of framecraft's tracing machinery.

    The ``replace("\\\\", "/")`` is not cosmetic: ``co_filename`` is whatever
    string was handed to ``compile()``, so on Windows it contains backslashes
    while the prefixes above are normalised to forward slashes.  Comparing raw
    makes the exclusion silently work on Linux and silently fail on Windows.
    """
    filename = code.co_filename.replace("\\", "/")
    return any(filename.startswith(d) for d in _OWN_DIRS)


def code_id(code: CodeType) -> int:
    """Return a *process-stable* identity for a code object.

    ``id()`` is wrong here: nothing keeps the code object alive for you, and
    CPython aggressively frees code objects, so a freed id can be reissued to a
    brand new object.  We keep a strong reference in :data:`_CODE_CACHE` and
    hand out a dense counter instead.

    Why a dense counter rather than the address? Because ids are printed into
    trace files and diffed between runs.  A counter makes two runs of the same
    program produce byte-identical output, which is what makes the trace-diffing
    tooling in :mod:`framecraft.analysis.diff` useful.
    """
    cached = _CODE_CACHE.get(code)
    if cached is not None:
        return cached["id"]
    new_id = len(_CODE_CACHE) + 1
    _CODE_CACHE[code] = {"id": new_id}
    return new_id


def code_hash(code: CodeType) -> str:
    """A stable 8-hex-digit fingerprint of a code object.

    Two code objects compiled from the same source at the same flags hash the
    same, even across processes.  Use this to correlate traces from different
    runs (see :mod:`framecraft.analysis.diff`).
    """
    cached = _CODE_CACHE.get(code)
    if cached is not None and "hash" in cached:
        return cached["hash"]
    import hashlib

    h = hashlib.blake2b(digest_size=4)
    h.update(code.co_name.encode("utf-8", "replace"))
    h.update(code.co_filename.encode("utf-8", "replace"))
    h.update(str(code.co_argcount).encode())
    h.update(str(code.co_firstlineno).encode())
    h.update(repr(code.co_consts).encode("utf-8", "replace"))
    h.update(repr(code.co_names).encode("utf-8", "replace"))
    if _CODE_CACHE.get(code) is not None:
        _CODE_CACHE[code]["hash"] = h.hexdigest()
    return h.hexdigest()


def qualified_name(code: CodeType) -> str:
    """A display name for ``code`` that survives classes and lambdas.

    ``co_name`` for a method called ``run`` is just ``"run"``.  ``co_qualname``
    (3.11+) gives ``"Worker.run"``.  For lambdas, ``co_name`` is ``"<lambda>"``
    for every lambda in the program, so we disambiguate by line number::

        Outer.<locals>.<lambda>:42

    Memoised, because this runs once per emitted event and the f-string work
    above is not free on the hot path -- measurable, in the same way that
    ``offset_to_line`` was.
    """
    cached = _CODE_CACHE.get(code)
    if cached is not None and "qualname" in cached:
        return cached["qualname"]
    name = getattr(code, "co_qualname", None) or code.co_name
    if code.co_name == "<lambda>":
        out = f"{name}:{code.co_firstlineno}"
    elif code.co_name == "<module>":
        out = f"<module:{_short_path(code.co_filename)}>"
    else:
        out = name
    entry = _CODE_CACHE.get(code)
    if entry is not None:
        entry["qualname"] = out
    return out


def _short_path(filename: str) -> str:
    if not filename:
        return "<unknown>"
    if filename.startswith("<") and filename.endswith(">"):
        return filename
    return filename.replace("\\", "/").rsplit("/", 1)[-1]


def describe_code(code: CodeType) -> dict[str, Any]:
    """Return a JSON-friendly description of a code object.

    This is deliberately a dict, not a dataclass: it is emitted verbatim into
    JSONL sinks and SQL rows, so the shape is part of the file format contract.
    Keep it flat, keep keys stable, and add new keys rather than renaming.
    """
    cached = _CODE_CACHE.get(code)
    if cached is not None and "desc" in cached:
        return cached["desc"]

    from framecraft._internal.compat import HAS_CODE_EXCEPTIONTABLE

    desc: dict[str, Any] = {
        "id": code_id(code),
        "hash": code_hash(code),
        "name": code.co_name,
        "qualname": qualified_name(code),
        "filename": code.co_filename,
        "short_filename": _short_path(code.co_filename),
        "firstlineno": code.co_firstlineno,
        "argcount": code.co_argcount,
        "posonlyargcount": getattr(code, "co_posonlyargcount", 0),
        "kwonlyargcount": getattr(code, "co_kwonlyargcount", 0),
        "nlocals": code.co_nlocals,
        "stacksize": code.co_stacksize,
        "flags": code.co_flags,
        "is_generator": bool(code.co_flags & 0x20),  # CO_GENERATOR
        "is_coroutine": bool(code.co_flags & 0x80),  # CO_COROUTINE
        "is_async_generator": bool(code.co_flags & 0x200),  # CO_ASYNC_GENERATOR
        "is_optimized": bool(code.co_flags & 0x1),  # CO_OPTIMIZED
        "is_newlocals": bool(code.co_flags & 0x2),  # CO_NEWLOCALS
        "is_nested": bool(code.co_flags & 0x10),  # CO_NESTED
        "has_freevars": bool(code.co_flags & 0x40),  # CO_FREEVARS
        "nlocalsplus": getattr(code, "co_nlocalsplus", None),
        "stacksize_bytes": getattr(code, "co_stacksize", 0) * 8,
        "consts": len(code.co_consts),
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
        "size_bytes": len(code.co_code),
    }
    if HAS_CODE_EXCEPTIONTABLE:
        desc["exception_table_bytes"] = len(code.co_exceptiontable)
    # `co_lines()` is a generator of (start, end, lineno).  Materialising it
    # once here turns every later "which line is this offset on?" question
    # from an O(n) rescan into a bisect.
    lines = list(code.co_lines())
    desc["line_spans"] = len([1 for _, _, ln in lines if ln is not None])
    if _CODE_CACHE.get(code) is not None:
        _CODE_CACHE[code]["desc"] = desc
    return desc


def current_offset(frame: FrameType) -> int:
    """The instruction pointer of ``frame`` as a *byte* offset into ``co_code``.

    ``f_lasti`` is a byte offset, not an instruction index.  Since 3.11 the
    bytecode stream contains inline ``CACHE`` entries, so instruction #7 may
    live at byte offset 11.  Anything that indexes a list built from
    ``dis.get_instructions()`` must convert first -- see
    :func:`instructions_at_line`, which does it for you.
    """
    return frame.f_lasti


def offset_to_line(code: CodeType, offset: int) -> int | None:
    """Map a byte offset to a line number using the code's line table.

    ``None`` means "this offset is not attributable to a source line" --
    typically a ``CACHE`` slot, or the synthetic return at the end of the
    function.  A tracer that treats ``None`` as 0 will produce phantom
    "line 0" entries in your output; a tracer that treats it as an error will
    crash on any function with a ``return`` in it.
    """
    for start, end, lineno in code.co_lines():
        if start <= offset < end:
            return lineno
    return None


def line_at_offset(code: CodeType, offset: int) -> int | None:
    """Alias of :func:`offset_to_line` kept for readability at call sites."""
    return offset_to_line(code, offset)


def instructions_at_line(code: CodeType, lineno: int) -> list[dis.Instruction]:
    """Every real instruction whose source span covers ``lineno``.

    Two things make this non-trivial and worth a helper:

    *   A single *line* can be compiled into many instructions (``x = f(1)`` is
        LOAD_CONST, LOAD_NAME, CALL, STORE_FAST).
    *   A multi-line expression can attribute several instructions to
        different lines, and 3.11's position table can even give a *column*
        span, so "the instruction for line 10" is ambiguous without positions.

    We filter out ``CACHE`` and ``EXTENDED_ARG`` because they are artefacts of
    the instruction encoding, not things the programmer wrote.
    """
    out: list[dis.Instruction] = []
    for ins in dis.get_instructions(code):
        if ins.opname in ("CACHE", "EXTENDED_ARG"):
            continue
        positions = getattr(ins, "positions", None)
        if positions is None:
            if ins.starts_line == lineno or ins.line_number == lineno:
                out.append(ins)
            continue
        # positions.lineno is authoritative on 3.11+; starts_line is only set
        # on the *first* instruction of a line and is therefore unreliable for
        # multi-instruction lines.
        if positions.lineno == lineno:
            out.append(ins)
    return out


def exception_table(code: CodeType) -> list[dict[str, Any]]:
    """Decode ``co_exceptiontable`` into structured try/except regions.

    3.11 replaced ``SETUP_FINALLY`` with a data-driven table so that exception
    handling costs nothing when no exception is raised.  The table is a packed
    byte stream of variable-width varints; ``dis._parse_exception_table`` is the
    private decoder that produces the ``ExceptionTable`` namedtuple entries.
    """
    if not getattr(code, "co_exceptiontable", b""):
        return []
    rows: list[dict[str, Any]] = []
    try:
        import dis as _dis

        entries = _dis._parse_exception_table(code)
    except Exception:  # pragma: no cover - private API drift
        return [{"raw": bytes(code.co_exceptiontable).hex()}]

    for start, end, target, depth, lasti in entries:
        rows.append(
            {
                "start": start,
                "end": end,
                "target": target,
                "depth": depth,
                "lasti": lasti,
            }
        )
    return rows


def frame_locals_snapshot(frame: FrameType) -> dict[str, Any]:
    """A *copy* of ``frame.f_locals``.

    On 3.13+ (``PEP 667``) ``f_locals`` is a write-through proxy, so holding
    the returned object and reading it later gives you values that changed
    after your event fired.  Tracers want a snapshot at event time, so always
    go through ``dict()``.
    """
    return dict(frame.f_locals)


def iter_stack(frame: FrameType | None = None, limit: int | None = None) -> Iterator[FrameType]:
    """Yield ``frame`` then its callers, innermost first.

    The safe way to walk ``f_back``.  ``f_back`` can be ``None`` at the bottom
    of the stack; a naive ``while frame:`` loop that forgets this crashes the
    process rather than raising.
    """
    f = frame if frame is not None else sys._getframe(1)
    count = 0
    while f is not None:
        yield f
        if limit is not None and count >= limit:
            return
        f = f.f_back
        count += 1


def code_of(frame: FrameType) -> CodeType:
    """Typed accessor for ``frame.f_code``."""
    return frame.f_code


def locals_of_names(code: CodeType) -> Sequence[str]:
    """Names that live in the fast-locals array, in slot order.

    ``co_varnames`` covers *all* ``CO_FAST_LOCAL`` slots, including the
    compiler-generated ones (``.0`` for a bare ``except:``, and the slot that
    holds a cell for a ``cellvar``).  ``frame.f_locals`` hides those.  If you
    are doing precise slot accounting (``f_localsplus`` introspection,
    ``sys._getframe`` frame-size accounting), you need the raw list.
    """
    return code.co_varnames
