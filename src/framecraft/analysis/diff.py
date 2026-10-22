"""Deterministic trace diffing: did my change actually change anything?

This is the one analysis tool that needs the code *hashes* from
:mod:`framecraft._internal.codeinfo`, and the reason is worth stating.

The problem
-----------

You have a trace from before a change and a trace from after.  You want to know
"did this make things faster".  Naively you compare event counts per
``file:line``.  That fails constantly:

*   you added a helper function, so *every* frame's ``file:line`` shifted and
    the diff is all noise;
*   the change moved work from ``a()`` to ``b()`` and you cannot tell whether
    that was a net win;
*   one run had a warm cache and the other did not, and the counts are not
    comparable at all.

What makes it tractable is that :func:`framecraft._internal.codeinfo.code_hash`
is a function of ``(co_name, co_filename, co_firstlineno, co_consts,
co_names)``.  So "the same function" survives a refactor that moves it up a few
lines, and "a different function" does not collide.  Not perfect -- it is a
heuristic, and :func:`diff_traces` reports its own confidence rather than
pretending otherwise -- but it turns an unusable diff into a usable one.

What is and is not comparable
-----------------------------

Counts are comparable *only* if the program did the same amount of work.  So
:func:`diff_traces` normalises by the total number of ``call`` events and
reports the raw counts too.  If the totals differ by more than a few percent,
the diff says the runs are not comparable instead of pretending.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from framecraft.core.events import CALL, LINE, TraceEvent

__all__ = ["TraceProfile", "profile_trace", "diff_traces", "DiffRow", "render_diff"]


@dataclass
class TraceProfile:
    """A comparable fingerprint of one run."""

    label: str
    call_counts: dict[str, int] = field(default_factory=dict)
    line_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    locations: dict[str, str] = field(default_factory=dict)
    total_calls: int = 0
    total_lines: int = 0
    duration_s: float = 0.0

    def norm(self) -> float:
        """Calls per unit work; the divisor for comparable percentages."""
        return float(self.total_calls or 1)


def profile_trace(events: Iterable[TraceEvent], *, label: str = "run") -> TraceProfile:
    """Build a :class:`TraceProfile` from an event stream."""
    p = TraceProfile(label=label)
    first = None
    last = None
    for ev in events:
        if first is None:
            first = ev.perf_ns
        last = ev.perf_ns
        if ev.kind == CALL and ev.code_hash:
            p.call_counts[ev.code_hash] = p.call_counts.get(ev.code_hash, 0) + 1
            p.total_calls += 1
            p.names.setdefault(ev.code_hash, ev.qualname or ev.name)
            p.locations.setdefault(ev.code_hash, f"{ev.short_filename}:{ev.lineno}")
        elif ev.kind == LINE and ev.filename:
            key = (ev.filename.replace("\\", "/"), str(ev.lineno))
            p.line_counts[key] = p.line_counts.get(key, 0) + 1
            p.total_lines += 1
    if first is not None and last is not None:
        p.duration_s = (last - first) / 1e9
    return p


@dataclass
class DiffRow:
    """One function's before/after comparison."""

    code_hash: str
    name: str
    location: str
    before: int
    after: int
    delta: int
    pct_before: float
    pct_after: float
    pct_delta: float

    @property
    def direction(self) -> str:
        if self.pct_delta > 1.0:
            return "slower"
        if self.pct_delta < -1.0:
            return "faster"
        return "same"


def diff_traces(
    before: Sequence[TraceEvent],
    after: Sequence[TraceEvent],
    *,
    label_before: str = "before",
    label_after: str = "after",
    min_pct_delta: float = 1.0,
    limit: int = 40,
    comparable_tolerance: float = 0.05,
) -> dict[str, Any]:
    """Compare two runs function-by-function.

    Returns a dict with ``comparable`` (bool), ``reason``, ``rows`` and
    ``summary``.  When the two runs did materially different amounts of work we
    say so and set ``comparable=False`` rather than emitting a diff that looks
    authoritative and is not.
    """
    a = profile_trace(before, label=label_before)
    b = profile_trace(after, label=label_after)

    comparable = True
    reason = "same total call volume"
    if a.total_calls and b.total_calls:
        ratio = abs(a.total_calls - b.total_calls) / max(a.total_calls, b.total_calls)
        if ratio > comparable_tolerance:
            comparable = False
            reason = (
                f"total calls differ by {ratio * 100:.1f}% "
                f"({a.total_calls} vs {b.total_calls}); the runs did different "
                f"amounts of work so per-function percentages are not comparable"
            )

    rows: list[DiffRow] = []
    for h in set(a.call_counts) | set(b.call_counts):
        ca = a.call_counts.get(h, 0)
        cb = b.call_counts.get(h, 0)
        pa = 100.0 * ca / a.norm()
        pb = 100.0 * cb / b.norm()
        rows.append(
            DiffRow(
                code_hash=h,
                name=b.names.get(h) or a.names.get(h, h),
                location=b.locations.get(h) or a.locations.get(h, ""),
                before=ca,
                after=cb,
                delta=cb - ca,
                pct_before=round(pa, 4),
                pct_after=round(pb, 4),
                pct_delta=round(pb - pa, 4),
            )
        )

    rows.sort(key=lambda r: (-abs(r.pct_delta), r.name))
    interesting = [r for r in rows if abs(r.pct_delta) >= min_pct_delta] or rows
    faster = [r for r in rows if r.pct_delta < 0]
    slower = [r for r in rows if r.pct_delta > 0]

    return {
        "comparable": comparable,
        "reason": reason,
        "before": {
            "label": a.label,
            "total_calls": a.total_calls,
            "total_lines": a.total_lines,
            "duration_s": round(a.duration_s, 6),
            "distinct_functions": len(a.call_counts),
        },
        "after": {
            "label": b.label,
            "total_calls": b.total_calls,
            "total_lines": b.total_lines,
            "duration_s": round(b.duration_s, 6),
            "distinct_functions": len(b.call_counts),
        },
        "new_functions": sorted(b.names[h] for h in set(b.call_counts) - set(a.call_counts)),
        "removed_functions": sorted(a.names[h] for h in set(a.call_counts) - set(b.call_counts)),
        "rows": [r.__dict__ for r in interesting[:limit]],
        "summary": {
            "faster": len(faster),
            "slower": len(slower),
            "unchanged": len(rows) - len(faster) - len(slower),
            "net_pct_delta": round(sum(r.pct_delta for r in rows), 4),
            "faster_ns_share": round(
                100.0 * sum(abs(r.pct_delta) for r in faster) / max(1e-9, sum(abs(r.pct_delta) for r in rows)), 2
            ),
        },
    }


def _direction(pct_delta: float) -> str:
    """The +/-/= label, using the same thresholds as DiffRow.direction."""
    if pct_delta > 1.0:
        return "slower"
    if pct_delta < -1.0:
        return "faster"
    return "same"


def render_diff(diff: dict[str, Any]) -> str:
    """Render :func:`diff_traces` output as text, honestly."""
    from framecraft.analysis.stats import render_table

    lines: list[str] = []
    lines.append(f"TRACE DIFF  {diff['before']['label']}  ->  {diff['after']['label']}")
    lines.append("=" * 78)
    lines.append(
        f"  before: {diff['before']['total_calls']:,} calls, "
        f"{diff['before']['total_lines']:,} lines, "
        f"{diff['before']['duration_s']:.4f}s, "
        f"{diff['before']['distinct_functions']:,} functions"
    )
    lines.append(
        f"  after : {diff['after']['total_calls']:,} calls, "
        f"{diff['after']['total_lines']:,} lines, "
        f"{diff['after']['duration_s']:.4f}s, "
        f"{diff['after']['distinct_functions']:,} functions"
    )
    if not diff["comparable"]:
        lines.append("")
        lines.append(f"  !! NOT COMPARABLE: {diff['reason']}")
    s = diff["summary"]
    lines.append(
        f"  {s['faster']} faster / {s['slower']} slower / {s['unchanged']} unchanged; "
        f"net {s['net_pct_delta']:+.2f} percentage points of call volume"
    )
    if diff["new_functions"]:
        lines.append(f"  + new: {', '.join(diff['new_functions'][:8])}")
    if diff["removed_functions"]:
        lines.append(f"  - gone: {', '.join(diff['removed_functions'][:8])}")
    lines.append("")
    # NOTE: `direction` is a @property on DiffRow, so `r.__dict__` (which is
    # what diff_traces puts in "rows") does not contain it.  Read it through the
    # dataclass rather than the dict, or compute it here.
    rows = [
        {
            "name": r["name"],
            "location": r["location"],
            "before": r["before"],
            "after": r["after"],
            "delta": r["delta"],
            "pct": f"{r['pct_delta']:+.2f}%",
            "dir": _direction(r["pct_delta"]),
        }
        for r in diff["rows"]
    ]
    if rows:
        lines.append(
            render_table(
                rows,
                ["name", "location", "before", "after", "delta", "pct", "dir"],
                headers=["function", "location", "before", "after", "delta", "% calls", ""],
            )
        )
    return "\n".join(lines)


def load_jsonl(path: str) -> list[TraceEvent]:
    """Read a JSONL file written by :class:`framecraft.sinks.JSONLSink`."""
    out: list[TraceEvent] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(TraceEvent.from_dict(json.loads(line)))
    return out
