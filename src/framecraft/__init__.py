"""framecraft: execution tracing built on Python's frame APIs.

    from framecraft import FunctionTracer, MonitorTracer, TextSink

    with FunctionTracer(events=("call", "line", "return")) as t:
        work()

    for ev in t.events:
        print(ev.summary())

Read the docs in ``docs/`` in order; ``examples/`` is a runnable curriculum.
"""

from framecraft._internal.compat import HAS_MONITORING, PY_VERSION
from framecraft._internal.values import CapturePolicy, Redactor, capture_value, safe_repr
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
from framecraft.trace.base import HookLease, Tracer, TracerConfig
from framecraft.trace.composite import (
    compose,
    profile_function,
    trace_function,
    trace_monitor,
)
from framecraft.trace.function_tracer import FunctionTracer, LineTracer
from framecraft.trace.monitor_tracer import MonitorTracer
from framecraft.trace.profile_tracer import ProfileTracer
from framecraft.trace.sampling import SamplingTracer, stack_key

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "HAS_MONITORING",
    "PY_VERSION",
    # events
    "TraceEvent",
    "StackFrame",
    "LocalBinding",
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
    # tracers
    "Tracer",
    "TracerConfig",
    "FunctionTracer",
    "LineTracer",
    "MonitorTracer",
    "ProfileTracer",
    "SamplingTracer",
    "HookLease",
    "compose",
    "trace_function",
    "trace_monitor",
    "profile_function",
    # frame introspection
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
    "stack_key",
    # values
    "safe_repr",
    "capture_value",
    "CapturePolicy",
    "Redactor",
]
