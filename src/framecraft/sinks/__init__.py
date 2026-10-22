"""Output sinks: the only place in framecraft allowed to decide presentation.

* :mod:`~framecraft.sinks.base` -- the one-method protocol, plus ``NullSink``,
  ``CountingSink`` and ``TeeSink``.
* :mod:`~framecraft.sinks.text` -- :class:`TextSink` (human) and
  :class:`CSVSink` (spreadsheet).
* :mod:`~framecraft.sinks.machine` -- :class:`JSONLSink` (the interchange
  format) and :class:`SQLiteSink` (queryable).
* :mod:`~framecraft.sinks.flame` -- flame graph layout and SVG rendering.
* :mod:`~framecraft.sinks.dot` -- call graph as Graphviz DOT.
* :mod:`~framecraft.sinks.report` -- self-contained HTML report.

A sink is registered with :meth:`framecraft.trace.Tracer.add_sink` and must have
a ``write(event)`` method.  That is the whole contract.
"""

from framecraft.sinks.base import CountingSink, NullSink, Sink, TeeSink, open_sink
from framecraft.sinks.dot import DOTSink, callgraph_dot, render_dot
from framecraft.sinks.flame import SVGFlameSink, flame_svg, layout, stacks_to_tree
from framecraft.sinks.machine import JSONLSink, SQLiteSink, query_sqlite
from framecraft.sinks.report import HTMLReport, render_report
from framecraft.sinks.text import CSVSink, TextSink, colour_for_kind

__all__ = [
    "Sink",
    "NullSink",
    "CountingSink",
    "TeeSink",
    "open_sink",
    "TextSink",
    "CSVSink",
    "colour_for_kind",
    "JSONLSink",
    "SQLiteSink",
    "query_sqlite",
    "SVGFlameSink",
    "flame_svg",
    "layout",
    "stacks_to_tree",
    "DOTSink",
    "callgraph_dot",
    "render_dot",
    "HTMLReport",
    "render_report",
]
