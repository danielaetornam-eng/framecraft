"""Framecraft's stable, documented API.

Two layers live in ``framecraft.core``:

``events``
    The vocabulary.  If two different tracers produce the same
    :class:`~framecraft.core.events.TraceEvent`, every sink and every analysis
    in the library works on both.  Import the event-kind constants from here
    rather than using raw strings.

``frames``
    Read-only reflection over live frames, with no hooks installed.  Everything
    a tracer needs to *understand* a frame, separated from everything it needs
    to *observe* one.

Both re-export their symbols from the package root, so
``from framecraft import walk, LINE, CapturePolicy`` is the intended style.
"""

from framecraft.core import events, frames
from framecraft.core.events import (
    C_CALL,
    C_EXCEPTION,
    C_RETURN,
    CALL,
    CALL_SITE,
    EXCEPTION,
    KINDS,
    LINE,
    OPCODE,
    RAISE_SITE,
    RETURN,
    SAMPLE,
    YIELD,
    EventKind,
    LocalBinding,
    StackFrame,
    TraceEvent,
)
from framecraft.core.frames import (
    caller_frame,
    capture_locals,
    clocks,
    current_frame,
    describe,
    frame_depth,
    is_suspended_frame,
    render_stack,
    resolve_name,
    scope_of,
    stack_snapshot,
    suspended_frames,
    walk,
)

__all__ = [
    "events",
    "frames",
    "TraceEvent",
    "StackFrame",
    "LocalBinding",
    "EventKind",
    "KINDS",
    "CALL",
    "RETURN",
    "LINE",
    "OPCODE",
    "EXCEPTION",
    "YIELD",
    "C_CALL",
    "C_RETURN",
    "C_EXCEPTION",
    "SAMPLE",
    "CALL_SITE",
    "RAISE_SITE",
    "current_frame",
    "caller_frame",
    "walk",
    "stack_snapshot",
    "render_stack",
    "describe",
    "frame_depth",
    "capture_locals",
    "resolve_name",
    "scope_of",
    "suspended_frames",
    "is_suspended_frame",
    "clocks",
]
