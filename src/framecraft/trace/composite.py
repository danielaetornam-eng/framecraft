"""Running several tracers at once, safely.

``sys.settrace`` and ``sys.monitoring`` each have a *single* global slot (per
thread) for ``settrace``, but ``sys.monitoring`` has five tool ids.  So the
composition rules are:

======================  ==========  =====================================
Tracer                  Slots used  Composable?
======================  ==========  =====================================
:class:`FunctionTracer` ``settrace``  one only (unless you chain manually)
:class:`ProfileTracer`  ``setprofile`` one only (same slot for all of them)
:class:`MonitorTracer`  one tool id  up to 3 more (``2..5``)
:class:`SamplingTracer` none          unlimited
======================  ==========  =====================================

Why this matters
----------------

The obvious way to "profile with lines *and* see C calls" is
``sys.settrace`` plus ``sys.setprofile``.  That works -- they are separate
slots -- but you must remember that ``cProfile`` is itself just
``sys.setprofile``, so if anything else in your process (a test runner, a
coverage plugin, ``ddtrace``) has installed a profile hook, your
:class:`ProfileTracer` silently replaced it.

:func:`compose` therefore takes *roles*, not classes, and refuses
unconstructible combinations with a message that names the conflict.  Failing
loudly at setup is always better than a profile that is subtly someone else's.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from framecraft.trace.base import Tracer, TracerConfig
from framecraft.trace.function_tracer import FunctionTracer
from framecraft.trace.monitor_tracer import MonitorTracer
from framecraft.trace.profile_tracer import ProfileTracer
from framecraft.trace.sampling import SamplingTracer

__all__ = ["compose", "trace_function", "profile_function", "trace_monitor"]


def compose(*tracers: Tracer) -> Any:
    """Install several tracers as one nested context manager.

    Order matters: tracers are *entered* left to right and *exited* right to
    left.  Put the sampler first so that its baseline measurement includes the
    cost of the instrumenting tracers rather than hiding it::

        with compose(SamplingTracer(interval=0.001),
                     MonitorTracer(events=("call", "return"))) as (s, m):
            work()

    Returns a ``CompositeTracer``.  Index it to get the tracers back.
    """
    return CompositeTracer(tracers)


class CompositeTracer:
    """Owns several tracers and presents them as one context manager."""

    def __init__(self, tracers: Sequence[Tracer]) -> None:
        self.tracers = list(tracers)
        self._validate()

    def _validate(self) -> None:
        settrace_users = [t for t in self.tracers if isinstance(t, FunctionTracer)]
        setprofile_users = [t for t in self.tracers if isinstance(t, ProfileTracer)]
        if len(settrace_users) > 1:
            raise RuntimeError(
                "two FunctionTracers cannot coexist: sys.settrace has one global "
                "slot per thread. Merge their configs into one tracer instead, or "
                "use MonitorTracer, which gets its own PEP 669 tool id."
            )
        if len(setprofile_users) > 1:
            raise RuntimeError(
                "two ProfileTracers cannot coexist: sys.setprofile has one global "
                "slot per thread and cProfile already lives there."
            )

    def __enter__(self) -> CompositeTracer:
        entered: list[Tracer] = []
        try:
            for t in self.tracers:
                t.start()
                entered.append(t)
        except BaseException:
            for t in reversed(entered):
                t.stop()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        for t in reversed(self.tracers):
            t.stop()

    def __getitem__(self, i: int) -> Tracer:
        return self.tracers[i]

    def __iter__(self) -> Iterator[Tracer]:
        return iter(self.tracers)

    def __len__(self) -> int:
        return len(self.tracers)

    def __repr__(self) -> str:
        return f"<CompositeTracer {self.tracers!r}>"


# ----------------------------------------------------------------------
# One-liner decorators.  Decorators beat context managers for the common
# "trace this one function" case because they cannot be forgotten and they
# cannot leak the hook.
# ----------------------------------------------------------------------


def trace_function(
    *kinds: str,
    capture_locals: bool = True,
    scope: str = "file",
    backend: str = "auto",
    **cfg: Any,
):
    """Decorate a function so its execution is traced while it runs.

    ::

        @trace_function("call", "line", "return", capture_locals=True)
        def algorithm(x, y):
            ...

    Parameters
    ----------
    scope:
        ``"file"`` (default) traces the decorated function *and everything it
        calls that lives in the same file* -- which is what you almost always
        want when debugging one algorithm.  ``"self"`` traces only the
        decorated function's own lines, which is what you want when measuring
        its internal control flow without the noise of its callees.
    backend:
        ``"auto"`` picks ``MonitorTracer`` on 3.12+ and ``FunctionTracer``
        before.  Pass ``"settrace"`` or ``"monitoring"`` to pin it; pinning
        ``"monitoring"`` on < 3.12 raises immediately rather than silently
        producing a different trace than you asked for.

    Two implementation notes that matter:

    *   The hook is installed *inside* the wrapper, so the wrapper's own frame
        and the rest of the process are excluded by the ``include`` predicate.
        Without that filter you would trace yourself -- and since the tracer's
        own frames are the innermost frames, they would dominate the output.
    *   ``functools.wraps`` preserves ``__name__``/``__doc__`` and sets
        ``__wrapped__``, which keeps tracebacks readable and lets
        ``inspect.stack()`` find the real function.

    The finished tracer is left on ``fn.last_tracer`` so you can inspect it::

        @trace_function("call", "return")
        def f(): ...
        f()
        print(f.last_tracer.stats())
    """
    import functools

    from framecraft._internal.compat import HAS_MONITORING

    kinds = kinds or ("call", "return", "line")
    if backend == "monitoring" and not HAS_MONITORING:
        raise RuntimeError("backend='monitoring' requires Python 3.12+")
    cls = MonitorTracer if (backend == "monitoring" or (backend == "auto" and HAS_MONITORING)) else FunctionTracer

    def decorate(fn):
        target_code = fn.__code__
        target_file = fn.__code__.co_filename

        if scope == "self":
            def include(c: Any) -> bool:
                return c is target_code
        elif scope == "file":
            def include(c: Any) -> bool:
                return c.co_filename == target_file and c.co_name not in ("<module>", "wrapper")
        else:
            raise ValueError(f"scope must be 'self' or 'file', got {scope!r}")

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = cls(
                TracerConfig(
                    events=tuple(kinds),
                    include=include,
                    capture_locals=capture_locals,
                    **cfg,
                )
            )
            with tracer:
                result = fn(*args, **kwargs)
            wrapper.last_tracer = tracer  # type: ignore[attr-defined]
            return result

        wrapper.last_tracer = None  # type: ignore[attr-defined]
        return wrapper

    return decorate


def profile_function(*kinds: str, **cfg: Any):
    """Like :func:`trace_function` but on the ``setprofile`` hook.

    Use this when you care about *timing* rather than control flow: the profile
    hook has far lower overhead, and every ``return`` event carries
    ``extra['duration_ns']``::

        @profile_function()
        def slow(): ...
        slow()
        slow.last_tracer.events[-1].extra["duration_ns"]
    """
    import functools

    kinds = kinds or ("call", "return")

    def decorate(fn):
        target_file = fn.__code__.co_filename

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = ProfileTracer(
                TracerConfig(
                    events=tuple(kinds),
                    include=lambda c: c.co_filename == target_file and c.co_name != "wrapper",
                ),
                **cfg,
            )
            with tracer:
                result = fn(*args, **kwargs)
            wrapper.last_tracer = tracer  # type: ignore[attr-defined]
            return result

        wrapper.last_tracer = None  # type: ignore[attr-defined]
        return wrapper

    return decorate


def trace_monitor(*kinds: str, **cfg: Any):
    """Like :func:`trace_function` but always on PEP 669.

    Provided so that a codebase can be explicit about which hook it depends on.
    Raises on < 3.12.
    """
    import functools

    kinds = kinds or ("call", "line", "return")

    def decorate(fn):
        target_file = fn.__code__.co_filename

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = MonitorTracer(
                TracerConfig(
                    events=tuple(kinds),
                    include=lambda c: c.co_filename == target_file and c.co_name != "wrapper",
                ),
                **cfg,
            )
            with tracer:
                result = fn(*args, **kwargs)
            wrapper.last_tracer = tracer  # type: ignore[attr-defined]
            return result

        wrapper.last_tracer = None  # type: ignore[attr-defined]
        return wrapper

    return decorate


# Re-export so `from framecraft.trace import ...` covers the whole vocabulary.
__all__ += ["FunctionTracer", "ProfileTracer", "MonitorTracer", "SamplingTracer", "Tracer", "TracerConfig"]
