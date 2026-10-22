"""The sink protocol, and the rules that make a sink safe to run inside a hook.

A **sink** is any object with ``write(event)``.  That is the entire interface.
Keeping it one method wide is deliberate:

*   ``Tracer.add_sink`` calls it from inside the trace hook, so it must be fast
    and must not raise.  (A sink that raises is caught and counted by
    :meth:`Tracer._guard`, so it cannot kill the program -- but it *will*
    silently lose every event it failed on, so write sinks defensively.)
*   Because the interface is one method, a sink can be a list append, an f.write,
    a socket send, or a no-op.  ``examples/19_streaming.py`` uses all four.
*   A sink is the only place allowed to decide *presentation*.  Tracers capture,
    sinks render.  If rendering decisions leaked into tracers you could not
    switch formats without rewriting the engine.

Buffered versus unbuffered
--------------------------

Every sink here is **streaming**: it writes as events arrive.  The tracer's own
buffer is opt-in (``TracerConfig.buffer``).  For a long-running program with
millions of line events, buffering is not a convenience, it is a memory leak.
The trade is that a crashed program loses whatever the OS has not flushed --
which is why :class:`~framecraft.sinks.JSONLSink` and
:class:`~framecraft.sinks.SQLiteSink` commit in batches rather than per event.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from framecraft.core.events import TraceEvent

__all__ = ["Sink", "NullSink", "CountingSink", "TeeSink", "open_sink"]


@runtime_checkable
class Sink(Protocol):
    """Anything that can consume a :class:`~framecraft.core.events.TraceEvent`."""

    def write(self, event: TraceEvent) -> None:  # pragma: no cover - protocol
        ...


class NullSink:
    """Discards everything.  Useful for benchmarking tracer overhead alone."""

    __slots__ = ()

    def write(self, event: TraceEvent) -> None:
        return None

    def __repr__(self) -> str:
        return "<NullSink>"


class CountingSink:
    """Counts events by kind.  The cheapest possible sanity check.

    When a tracer appears to do nothing, install this first: if the counts are
    zero the problem is filtering or hook installation, not the sink.
    """

    __slots__ = ("counts", "total")

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.total = 0

    def write(self, event: TraceEvent) -> None:
        self.total += 1
        self.counts[event.kind] = self.counts.get(event.kind, 0) + 1

    def __repr__(self) -> str:
        return f"<CountingSink total={self.total} {self.counts}>"


class TeeSink:
    """Fan-out to several sinks.

    ``Tracer.add_sink`` already accepts many sinks, so ``TeeSink`` is only
    useful when you need one object with a ``write`` method -- for instance to
    pass to a third-party API.  Its error policy is *all-or-nothing per event*:
    one failing child does not stop the others, and the failure is re-raised at
    the end so it cannot be lost.
    """

    __slots__ = ("children",)

    def __init__(self, *children: Any) -> None:
        self.children = [c for c in children if c is not None]

    def write(self, event: TraceEvent) -> None:
        first_exc: BaseException | None = None
        for c in self.children:
            try:
                c.write(event)
            except BaseException as exc:
                if first_exc is None:
                    first_exc = exc
        if first_exc is not None:
            raise first_exc

    def __repr__(self) -> str:
        return f"<TeeSink {self.children!r}>"


def open_sink(spec: Any) -> Any:
    """Coerce a path / stream / sink into something with ``.write``.

    Accepts, in order of preference:

    *   an object with a ``write`` method (a sink, a file, ``sys.stdout``),
    *   ``None`` -- returns a :class:`NullSink`,
    *   a ``str`` / ``os.PathLike`` -- opened in append mode.
    """
    if spec is None:
        return NullSink()
    if hasattr(spec, "write"):
        return spec
    return open(spec, "a", encoding="utf-8")
