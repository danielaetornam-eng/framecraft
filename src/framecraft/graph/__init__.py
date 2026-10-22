"""Graph structures built from trace events.

* :mod:`~framecraft.graph.callgraph` -- :class:`CallGraph` (who calls whom) and
  :class:`StackTree` (where did the samples go).
* :mod:`~framecraft.graph.timeline` -- :class:`Timeline` for viewing events as a
  Gantt chart, which is the right visualisation for concurrency and for
  "what was the program doing while it was blocked".
"""

from framecraft.graph.callgraph import (
    CallGraph,
    CallNode,
    StackTree,
    build_call_graph,
    build_stack_tree,
)
from framecraft.graph.timeline import Span, Timeline, build_timeline

__all__ = [
    "CallGraph",
    "CallNode",
    "StackTree",
    "build_call_graph",
    "build_stack_tree",
    "Timeline",
    "Span",
    "build_timeline",
]
