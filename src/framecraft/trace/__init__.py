"""Tracers: the things that install hooks.

Reading order if you are learning the material:

1.  :mod:`framecraft.trace.base` -- the two-level hook contract, the scoped
    installation dance, and why hooks must never raise.
2.  :mod:`framecraft.trace.function_tracer` -- ``sys.settrace``, the engine you
    will use on every Python version.
3.  :mod:`framecraft.trace.monitor_tracer` -- PEP 669, the engine you *should*
    use on 3.12+.
4.  :mod:`framecraft.trace.profile_tracer` -- ``sys.setprofile``, the only way
    to see into C.
5.  :mod:`framecraft.trace.sampling` -- no hook at all.
6.  :mod:`framecraft.trace.composite` -- how to run more than one.

The event-kind vocabulary (``"call"``, ``"line"``, ``"return"``,
``"exception"``, ``"yield"``, ``"c_call"``, ``"c_return"``, ``"c_exception"``)
is defined once in :mod:`framecraft.core.events` and re-exported here.
"""

from framecraft._internal.compat import HAS_MONITORING
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
)
from framecraft.trace.base import HookLease, Tracer, TracerConfig
from framecraft.trace.composite import (
    CompositeTracer,
    compose,
    profile_function,
    trace_function,
    trace_monitor,
)
from framecraft.trace.function_tracer import (
    FunctionTracer,
    LineTracer,
    install_thread,
    uninstall_thread,
)
from framecraft.trace.monitor_tracer import EVENT_MAP, MonitorTracer
from framecraft.trace.profile_tracer import ProfileTracer
from framecraft.trace.sampling import SamplingTracer, StackKey, stack_key

__all__ = [
    "Tracer",
    "TracerConfig",
    "HookLease",
    "FunctionTracer",
    "LineTracer",
    "MonitorTracer",
    "ProfileTracer",
    "SamplingTracer",
    "StackKey",
    "stack_key",
    "install_thread",
    "uninstall_thread",
    "compose",
    "CompositeTracer",
    "trace_function",
    "trace_monitor",
    "profile_function",
    "EVENT_MAP",
    "HAS_MONITORING",
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
]
