"""Text and tabular sinks: what you look at when you are debugging.

:class:`TextSink` is streaming and human-oriented: one line per event, colour
optional, aligned.  :class:`CSVSink` and :class:`JSONLSink` are the machine
formats; see :mod:`framecraft.sinks.machine`.

Design note on ANSI colour: it is on by default only when the destination is a
TTY, because colour in a redirected log file is the single most common reason a
tool's output "looks broken" in CI.  Detection is ``stream.isatty()`` and it is
checked once, at construction, not per line.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any, TextIO

from framecraft.core.events import (
    C_CALL,
    C_EXCEPTION,
    C_RETURN,
    CALL,
    EXCEPTION,
    LINE,
    OPCODE,
    RETURN,
    SAMPLE,
    YIELD,
    TraceEvent,
)

__all__ = ["TextSink", "CSVSink", "colour_for_kind"]

#: ANSI codes per event kind.  Chosen so the kinds you scan for are visually
#: distinct without being a rainbow: calls green, returns blue, lines grey,
#: exceptions red, C events magenta, samples yellow.
_COLOURS = {
    CALL: "\033[32m",
    RETURN: "\033[34m",
    LINE: "\033[90m",
    OPCODE: "\033[36m",
    EXCEPTION: "\033[31;1m",
    YIELD: "\033[35m",
    C_CALL: "\033[35m",
    C_RETURN: "\033[35m",
    C_EXCEPTION: "\033[31;1m",
    SAMPLE: "\033[33m",
}
_RESET = "\033[0m"


def colour_for_kind(kind: str, *, enabled: bool = True) -> str:
    return (_COLOURS.get(kind, "") + kind + (_RESET if enabled else "")) if enabled else kind


class TextSink:
    """One line per event, aligned and optionally coloured.

    Parameters
    ----------
    stream:
        Any text stream.  Defaults to ``sys.stdout``.
    kinds:
        Restrict output to these kinds.  ``None`` means all.
    colour:
        ``"auto"`` (default), ``True``, or ``False``.  ``"auto"`` enables
        colour only for a TTY.
    show_locals:
        Append each captured local binding.  Off by default because it turns a
        readable line into a wall of text.
    width:
        Target width; used to truncate the location column.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        kinds: Sequence[str] | None = None,
        colour: Any = "auto",
        show_locals: bool = False,
        show_stack: bool = False,
        width: int = 200,
        header: bool = True,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        if colour == "auto":
            try:
                colour = bool(self.stream.isatty())
            except Exception:
                colour = False
        self.colour = bool(colour)
        self.kinds = set(kinds) if kinds else None
        self.show_locals = show_locals
        self.show_stack = show_stack
        self.width = width
        self.count = 0
        self._wrote_header = False
        self._header = header

    def write(self, event: TraceEvent) -> None:
        if self.kinds and event.kind not in self.kinds:
            return
        if self._header and not self._wrote_header:
            self._wrote_header = True
            self.stream.write(
                f"{'seq':>6}  {'event':<11} {'depth':>5}  {'function':<30} {'location':<28} detail\n"
            )
            self.stream.write("-" * min(self.width, 120) + "\n")
        parts = [
            f"{event.seq:>6}",
            f"{colour_for_kind(event.kind, enabled=self.colour):<20}",
            f"{event.depth:>5}",
            f"{(event.qualname or event.name or '?')[:30]:<30}",
            f"{event.location[:28]:<28}",
        ]
        detail: list[str] = []
        if event.opcode:
            detail.append(event.opcode)
        if event.c_name:
            detail.append(f"c={event.c_name}")
        if event.arg_repr:
            detail.append(f"args=({event.arg_repr})")
        if event.retval_repr:
            detail.append(f"-> {event.retval_repr}")
        if event.exception_repr:
            detail.append(f"!! {event.exception_type}: {event.exception_repr}")
        if event.task:
            detail.append(f"task={event.task}")
        parts.append(" ".join(detail))
        self.stream.write("  ".join(parts).rstrip() + "\n")
        if self.show_locals and event.locals:
            for b in event.locals:
                self.stream.write(f"{'':>6}      {b.scope[:5]:<5} {b.name} = {b.value}\n")
        if self.show_stack and event.stack:
            for f in reversed(event.stack):
                self.stream.write(f"{'':>6}      {'|' if f.depth else '`--'} {f.location} in {f.qualname}\n")
        self.count += 1

    def close(self) -> None:
        self.stream.flush()

    def __repr__(self) -> str:
        return f"<TextSink rows={self.count} colour={self.colour}>"


class CSVSink:
    """Flat CSV, one row per event.

    Fixed column set, chosen so a spreadsheet can answer the questions people
    actually ask: which function, which line, which thread, how deep, what came
    back, what blew up.  Variable data (locals, stacks) goes in the JSONL sink
    instead -- a CSV with a column containing ``{"a": 1, "b": 2}`` is a CSV no
    one can use.
    """

    COLUMNS = (
        "seq",
        "kind",
        "perf_ns",
        "wall",
        "cpu",
        "thread",
        "thread_name",
        "task",
        "depth",
        "call_id",
        "parent_call_id",
        "code_id",
        "code_hash",
        "name",
        "qualname",
        "filename",
        "line",
        "offset",
        "opcode",
        "c_name",
        "arg",
        "retval",
        "exception_type",
        "exception",
    )

    def __init__(self, stream: TextIO | None = None, *, path: str | None = None) -> None:
        import csv

        self._own = stream is None
        self.stream = stream if stream is not None else open(path or "-", "w", newline="", encoding="utf-8")
        self._w = csv.writer(self.stream)
        self._w.writerow(self.COLUMNS)
        self.count = 0

    def write(self, event: TraceEvent) -> None:
        self._w.writerow(
            [
                event.seq,
                event.kind,
                event.perf_ns,
                f"{event.wall:.9f}",
                f"{event.cpu:.9f}",
                event.thread_id,
                event.thread_name,
                event.task or "",
                event.depth,
                event.call_id if event.call_id is not None else "",
                event.parent_call_id if event.parent_call_id is not None else "",
                event.code_id if event.code_id is not None else "",
                event.code_hash or "",
                event.name,
                event.qualname,
                event.filename,
                event.lineno if event.lineno is not None else "",
                event.offset if event.offset is not None else "",
                event.opcode or "",
                event.c_name or "",
                event.arg_repr or "",
                event.retval_repr or "",
                event.exception_type or "",
                event.exception_repr or "",
            ]
        )
        self.count += 1

    def close(self) -> None:
        self.stream.flush()
        if self._own:
            self.stream.close()

    def __repr__(self) -> str:
        return f"<CSVSink rows={self.count}>"
