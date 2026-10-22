"""Analysis: turn a trace into an answer.

* :mod:`~framecraft.analysis.stats` -- counts, hot lines, exclusive/inclusive
  times, exception summaries, and the text table renderer.
* :mod:`~framecraft.analysis.diff` -- deterministic comparison of two runs.

Two rules hold everywhere in this package:

1.  Every percentage is printed next to the raw count that produced it.
2.  Every report carries ``dropped`` and ``errors``, because a trace that lost
    events and does not say so is worse than no trace.
"""

from framecraft.analysis.diff import (
    DiffRow,
    TraceProfile,
    diff_traces,
    load_jsonl,
    profile_trace,
    render_diff,
)
from framecraft.analysis.stats import (
    confidence_note,
    exception_summary,
    exclusive_times,
    hot_functions,
    hot_lines,
    inclusive_times,
    recursion_report,
    render_table,
    summarise_events,
)

__all__ = [
    "summarise_events",
    "hot_lines",
    "hot_functions",
    "exclusive_times",
    "inclusive_times",
    "exception_summary",
    "recursion_report",
    "render_table",
    "confidence_note",
    "TraceProfile",
    "profile_trace",
    "diff_traces",
    "render_diff",
    "load_jsonl",
    "DiffRow",
]
