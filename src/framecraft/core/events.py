"""The event vocabulary shared by every tracer and every sink.

If you read one module to understand the whole library, read this one.  A
:class:`TraceEvent` is the *only* thing a tracer emits and the *only* thing a
sink consumes, which means you can mix and match: a PEP 669 tracer, a
``sys.settrace`` tracer and a sampling tracer all produce events that the same
graph builder, the same statistics module and the same JSONL sink accept.

Event kinds, and which API can produce them:

===============  =========================  ===========================
``kind``         ``sys.settrace``/profile   PEP 669 ``sys.monitoring``
===============  =========================  ===========================
``call``         yes (frame's ``call``)     ``PY_START``
``return``       yes (``return``)           ``PY_RETURN``
``line``         yes (``line``)             ``LINE``
``opcode``       yes (``opcode``)           n/a (use LINE+offset)
``exception``    yes                        ``EXCEPTION`` / ``PY_UNWIND``
``yield``        yes (see gotchas)          ``PY_YIELD``
``c_call``       ``setprofile`` only        ``C_CALL``
``c_return``     ``setprofile`` only        ``C_RETURN``
``c_exception``  ``setprofile`` only        ``C_RAISE``
``sample``       n/a (stack walk)           n/a (stack walk)
===============  =========================  ===========================
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from framecraft._internal.codeinfo import code_hash, code_id, qualified_name

__all__ = [
    "EventKind",
    "LocalBinding",
    "StackFrame",
    "TraceEvent",
    "KINDS",
]

#: Canonical, sorted event-kind constants.  Use these instead of raw strings so
#: a typo becomes an AttributeError at import time rather than a silently
#: dropped event at run time.
CALL = "call"
RETURN = "return"
LINE = "line"
OPCODE = "opcode"
EXCEPTION = "exception"
YIELD = "yield"
C_CALL = "c_call"
C_RETURN = "c_return"
C_EXCEPTION = "c_exception"
SAMPLE = "sample"
#: PEP 669's ``CALL`` event: a call *site*, carrying the callee and its first
#: argument.  Deliberately distinct from ``CALL`` (= ``PY_START``, a frame
#: entry) because the two answer different questions and conflating them makes
#: every trace ambiguous: ``call`` means "we entered this function",
#: ``call_site`` means "this line invoked that object".
CALL_SITE = "call_site"
#: PEP 669's ``RAISE`` (3.14+): a ``raise`` statement about to execute, with no
#: exception object yet.  Useful for coverage of raise sites.
RAISE_SITE = "raise_site"

EventKind = str

KINDS: tuple[str, ...] = (
    CALL,
    RETURN,
    LINE,
    OPCODE,
    EXCEPTION,
    YIELD,
    C_CALL,
    C_RETURN,
    C_EXCEPTION,
    SAMPLE,
    CALL_SITE,
    RAISE_SITE,
)


@dataclass(slots=True)
class LocalBinding:
    """One captured local variable.

    ``value`` is a *string* produced by :func:`framecraft.core.values.safe_repr`
    and ``structured`` is the JSON-shaped description produced by
    :func:`framecraft.core.values.capture_value`.  We ship both because humans
    read the first and machines need the second.
    """

    name: str
    value: str
    structured: Any = None
    #: ``"local"`` | ``"free"`` | ``"global"`` | ``"builtin"``.  A name found by
    #: textual search in ``f_locals`` but absent from ``co_varnames`` is *not* a
    #: local -- it is dynamically injected (e.g. by ``exec`` or a REPL), and
    #: flagging that distinction is how you debug "why does my tracer see a
    #: variable the disassembly says cannot exist?".
    scope: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "scope": self.scope,
            "structured": self.structured,
        }


@dataclass(slots=True)
class StackFrame:
    """A *point-in-time copy* of one frame.

    Never store a live :class:`types.FrameType` in a trace pipeline.  Frames
    die the instant the function returns, and reading ``frame.f_locals``
    afterwards raises or -- worse -- shows you recycled memory.  The tracers in
    this library snapshot frames at event time for exactly this reason.
    """

    code_id: int
    code_hash: str
    name: str
    qualname: str
    filename: str
    lineno: int
    offset: int
    #: ``0`` for the topmost frame.  Emitted as ``depth``.
    depth: int = 0
    is_generator: bool = False
    is_coroutine: bool = False
    locals: list[LocalBinding] = field(default_factory=list)

    @property
    def short_filename(self) -> str:
        return self.filename.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def location(self) -> str:
        """``file.py:12`` -- the canonical short form used in text output."""
        return f"{self.short_filename}:{self.lineno}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_id": self.code_id,
            "code_hash": self.code_hash,
            "name": self.name,
            "qualname": self.qualname,
            "filename": self.filename,
            "lineno": self.lineno,
            "offset": self.offset,
            "depth": self.depth,
            "is_generator": self.is_generator,
            "is_coroutine": self.is_coroutine,
            "locals": [b.to_dict() for b in self.locals],
        }

    @classmethod
    def from_frame(cls, frame: Any, *, depth: int = 0) -> StackFrame:
        """Snapshot ``frame`` without touching its namespace.

        ``include_locals=False`` is the default because ``f_locals`` is
        expensive (on <3.13 it materialises a whole new dict on every access)
        and re-enters user code for exotic mappings.
        """
        code = frame.f_code
        return cls(
            code_id=code_id(code),
            code_hash=code_hash(code),
            name=code.co_name,
            qualname=qualified_name(code),
            filename=code.co_filename,
            lineno=frame.f_lineno,
            offset=frame.f_lasti,
            depth=depth,
            is_generator=bool(code.co_flags & 0x20),
            is_coroutine=bool(code.co_flags & 0x80),
        )


@dataclass(slots=True)
class TraceEvent:
    """A single observation emitted by a tracer.

    The field set is a superset of every API's payload.  Fields that do not
    apply to a given ``kind`` stay ``None``; the sink decides what to print.
    That design is what lets the JSONL format stay stable even as tracers gain
    new capabilities.

    Fields
    ------
    seq:
        Monotonic per-``Tracer`` counter.  Gap-free by construction *except*
        where the underlying API itself drops events (documented per tracer).
        Use it to prove you did not lose events.
    wall / cpu / perf:
        Three clocks, because they answer different questions:

        * ``wall`` -- ``time.perf_counter``: elapsed real time.  Always
          monotonic; what a user experiences.
        * ``cpu`` -- ``time.process_time``: CPU burned by *this process*.
          Under the GIL this tracks wall time for Python bytecode; in a
          free-threaded 3.13+ build it does not.
        * ``perf`` -- ``time.perf_counter_ns`` truncated to ints for
          lossless integer arithmetic in sinks.  ``perf_ns - event.perf_ns``
          is the exact interval between two events, with no float drift.

        Give a tracer all three and you can distinguish "slow because it waited"
        from "slow because it burned CPU" -- the single most useful split in
        performance work.
    """

    seq: int
    kind: str
    wall: float
    cpu: float
    perf_ns: int

    # --- location -----------------------------------------------------------
    code_id: int | None = None
    code_hash: str | None = None
    name: str = ""
    qualname: str = ""
    filename: str = ""
    lineno: int | None = None
    offset: int | None = None

    # --- linkage ------------------------------------------------------------
    thread_id: int = 0
    thread_name: str = ""
    task: str | None = None  # asyncio current task name, when available
    depth: int = 0
    call_id: int | None = None  # pairs a call with its return
    parent_call_id: int | None = None

    # --- payload ------------------------------------------------------------
    arg_repr: str | None = None
    retval_repr: str | None = None
    exception_repr: str | None = None
    exception_type: str | None = None
    opcode: str | None = None
    arg: Any = None
    argval: Any = None
    c_name: str | None = None
    sample_count: int = 1
    locals: list[LocalBinding] = field(default_factory=list)
    stack: list[StackFrame] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def short_filename(self) -> str:
        return self.filename.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def location(self) -> str:
        return f"{self.short_filename}:{self.lineno}"

    def to_dict(self, *, include_locals: bool = False) -> dict[str, Any]:
        """Flatten to a plain dict suitable for JSON.

        ``include_locals`` defaults to ``False`` because local capture is
        frequently 95% of the size of a trace file.  Sinks that want it flip
        the flag; sinks that want small files do not.
        """
        d: dict[str, Any] = {
            "seq": self.seq,
            "kind": self.kind,
            "wall": round(self.wall, 9),
            "cpu": round(self.cpu, 9),
            "perf_ns": self.perf_ns,
            "code_id": self.code_id,
            "code_hash": self.code_hash,
            "name": self.name,
            "qualname": self.qualname,
            "filename": self.filename,
            "lineno": self.lineno,
            "offset": self.offset,
            "thread": self.thread_id,
            "thread_name": self.thread_name,
            "task": self.task,
            "depth": self.depth,
            "call_id": self.call_id,
            "parent_call_id": self.parent_call_id,
            "arg_repr": self.arg_repr,
            "retval_repr": self.retval_repr,
            "exception_repr": self.exception_repr,
            "exception_type": self.exception_type,
            "opcode": self.opcode,
            "argval": repr(self.argval) if self.argval is not None else None,
            "c_name": self.c_name,
            "sample_count": self.sample_count,
        }
        if self.extra:
            d["extra"] = self.extra
        if self.stack:
            d["stack"] = [f.to_dict() for f in self.stack]
        if include_locals and self.locals:
            d["locals"] = [b.to_dict() for b in self.locals]
        return d

    def summary(self) -> str:
        """One human-readable line.  The text sink's default rendering."""
        bits = [f"#{self.seq:<6}", f"{self.kind:<11}", f"{self.qualname or self.name:<28}"]
        if self.lineno:
            bits.append(f"{self.short_filename}:{self.lineno}")
        if self.opcode:
            bits.append(self.opcode)
        if self.arg_repr:
            bits.append(f"args={self.arg_repr}")
        if self.retval_repr:
            bits.append(f"-> {self.retval_repr}")
        if self.exception_repr:
            bits.append(f"!! {self.exception_repr}")
        if self.c_name:
            bits.append(f"c={self.c_name}")
        if self.task:
            bits.append(f"task={self.task}")
        return " ".join(bits)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> TraceEvent:
        """Rebuild an event from :meth:`to_dict` output (used by the diff tool)."""
        ev = cls(
            seq=d["seq"],
            kind=d["kind"],
            wall=d["wall"],
            cpu=d["cpu"],
            perf_ns=d["perf_ns"],
        )
        for key in (
            "code_id", "code_hash", "name", "qualname", "filename", "lineno",
            "offset", "thread_id", "thread_name", "task", "depth", "call_id",
            "parent_call_id", "arg_repr", "retval_repr", "exception_repr",
            "exception_type", "opcode", "c_name", "sample_count",
        ):
            if key in d:
                setattr(ev, key, d[key])
        if "stack" in d:
            ev.stack = [
                StackFrame(
                    code_id=f["code_id"], code_hash=f["code_hash"], name=f["name"],
                    qualname=f["qualname"], filename=f["filename"], lineno=f["lineno"],
                    offset=f["offset"], depth=f.get("depth", 0),
                )
                for f in d["stack"]
            ]
        return ev
