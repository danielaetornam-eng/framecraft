"""Statistics over trace events: hot spots, costs, and where time went.

Everything here is deterministic aggregation over an event buffer.  The design
rule throughout: **every percentage is printed next to its raw count**, because
a percentage without a count is unfalsifiable and is how profilers mislead
people.  A function credited with "12.4%" from 3 samples out of 24 is noise, and
the report must say so.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from framecraft.core.events import (
    CALL,
    EXCEPTION,
    LINE,
    RETURN,
    TraceEvent,
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
]

#: Below this many observations, treat a ranking as unreliable and say so.
_NOISE_FLOOR = 30


def confidence_note(count: int) -> str:
    """A short, honest verdict on whether ``count`` observations mean anything.

    For a *deterministic* trace (line/call events) the counts are exact, so we
    do not apply the statistical floor -- only :class:`~framecraft.trace.sampling.
    SamplingTracer` results do.  This function is for sampled data; callers
    with exact counts should skip it.
    """
    if count >= 1000:
        return "high"
    if count >= _NOISE_FLOOR:
        return "ok"
    if count >= 10:
        return "low"
    return "noise"


def summarise_events(
    events: Sequence[TraceEvent],
    *,
    dropped: int = 0,
    errors: int = 0,
    duration_ns: int | None = None,
) -> dict[str, Any]:
    """One dict describing a whole run.

    The fields a trace report must always carry:

    ``events_by_kind``
        Proves you looked at what you think you looked at.
    ``dropped`` / ``errors``
        A trace that silently lost events is worse than no trace, because it
        looks authoritative.  These are surfaced in every rendering.
    ``coverage_ratio``
        ``kept / (kept + dropped)``.  Report it; do not hide it.
    """
    by_kind: Counter = Counter()
    threads: Counter = Counter()
    files: Counter = Counter()
    funcs: set = set()
    first_ns = None
    last_ns = None

    for ev in events:
        by_kind[ev.kind] += 1
        threads[ev.thread_id] += 1
        if ev.filename:
            files[ev.short_filename] += 1
        if ev.code_hash:
            funcs.add(ev.code_hash)
        if first_ns is None:
            first_ns = ev.perf_ns
        last_ns = ev.perf_ns

    total = len(events)
    span_ns = duration_ns
    if span_ns is None and first_ns is not None and last_ns is not None:
        span_ns = max(0, last_ns - first_ns)

    return {
        "total_events": total,
        "events_by_kind": dict(sorted(by_kind.items())),
        "threads": len(threads),
        "distinct_files": len(files),
        "distinct_code_objects": len(funcs),
        "dropped": dropped,
        "errors": errors,
        "coverage_ratio": round(total / (total + dropped), 6) if (total + dropped) else 1.0,
        "span_ns": span_ns,
        "span_s": round(span_ns / 1e9, 6) if span_ns else 0.0,
        "events_per_s": round(total / (span_ns / 1e9), 1) if span_ns else None,
    }


def hot_lines(events: Iterable[TraceEvent], *, limit: int = 25, root: str | None = None) -> list[dict]:
    """Most-executed source lines.

    Counts ``line`` events only.  On a line tracer this is exact.  On a
    sampling tracer there are no ``line`` events at all -- use
    :meth:`framecraft.trace.SamplingTracer.line_tally` instead.
    """
    counts: Counter = Counter()
    for ev in events:
        if ev.kind != LINE or ev.lineno is None:
            continue
        if root and not ev.filename.replace("\\", "/").startswith(root):
            continue
        counts[(ev.filename, ev.lineno)] += 1
    total = sum(counts.values()) or 1
    out = []
    for (filename, lineno), n in counts.most_common(limit):
        out.append(
            {
                "file": filename,
                "line": lineno,
                "location": f"{filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{lineno}",
                "count": n,
                "pct": round(100.0 * n / total, 3),
            }
        )
    return out


def hot_functions(events: Iterable[TraceEvent], *, limit: int = 25, root: str | None = None) -> list[dict]:
    """Functions ranked by inclusive event volume.

    "Inclusive" here means *every event that happened while the frame was
    live*, which for a line tracer is dominated by line count and therefore
    correlates with how long the function took.  It is not wall-clock; if you
    want wall-clock, use :func:`exclusive_times` (from ``call``/``return``
    pairs) or a sampling tracer.
    """
    counts: Counter = Counter()
    names: dict = {}
    for ev in events:
        if not ev.code_hash:
            continue
        if root and not ev.filename.replace("\\", "/").startswith(root):
            continue
        counts[ev.code_hash] += 1
        names.setdefault(ev.code_hash, (ev.qualname or ev.name, ev.filename, ev.lineno or 0))
    total = sum(counts.values()) or 1
    out = []
    for h, n in counts.most_common(limit):
        name, filename, lineno = names[h]
        out.append(
            {
                "code_hash": h,
                "name": name,
                "file": filename,
                "line": lineno,
                "count": n,
                "pct": round(100.0 * n / total, 3),
            }
        )
    return out


def exclusive_times(events: Sequence[TraceEvent], *, limit: int = 25) -> list[dict]:
    """Wall-clock time per function, excluding callees.

    Computed by walking the event stream and tracking, per open frame, the time
    already attributed to its children::

        on  call:   push (hash, start, child_ns=0)
        on  exit:   self_ns = (now - start) - child_ns
                    parent.child_ns += (now - start)
                    pop

    This is exact for the tracer that produced the events, with two honest
    caveats reported in the output:

    *   a ``C_CALL``'s duration is charged to the *Python* frame that was
        running, because the hook does not tell us the C frame's own start;
    *   time inside an untraced frame is charged to its nearest traced ancestor.
    """
    live: list[list[Any]] = []  # [hash, start_ns, child_ns]
    self_ns: Counter = Counter()
    total_ns: Counter = Counter()
    calls: Counter = Counter()
    names: dict = {}

    for ev in events:
        if ev.kind == CALL:
            if ev.code_hash:
                names.setdefault(ev.code_hash, (ev.qualname or ev.name, ev.filename, ev.lineno or 0))
                calls[ev.code_hash] += 1
            live.append([ev.code_hash, ev.perf_ns, 0])
        elif ev.kind in (RETURN, EXCEPTION):
            if not live:
                continue
            h, start, child_ns = live.pop()
            dur = max(0, ev.perf_ns - start)
            if h:
                self_ns[h] += max(0, dur - child_ns)
                total_ns[h] += dur
            if live:
                live[-1][2] += dur

    grand = sum(total_ns.values()) or 1
    rows = []
    for h, t_ns in sorted(total_ns.items(), key=lambda kv: -kv[1])[:limit]:
        name, filename, lineno = names.get(h, (h, "", 0))
        rows.append(
            {
                "code_hash": h,
                "name": name,
                "file": filename,
                "line": lineno,
                "calls": calls.get(h, 0),
                "total_ns": t_ns,
                "self_ns": self_ns.get(h, 0),
                "total_ms": round(t_ns / 1e6, 4),
                "self_ms": round(self_ns.get(h, 0) / 1e6, 4),
                "pct_total": round(100.0 * t_ns / grand, 3),
                "pct_self": round(100.0 * self_ns.get(h, 0) / grand, 3),
            }
        )
    return rows


def inclusive_times(events: Sequence[TraceEvent], *, limit: int = 25) -> list[dict]:
    """Wall-clock time per function *including* all callees, recursively."""
    live: list[list[Any]] = []
    self_ns: Counter = Counter()
    names: dict = {}

    for ev in events:
        if ev.kind == CALL:
            if ev.code_hash:
                names.setdefault(ev.code_hash, (ev.qualname or ev.name, ev.filename, ev.lineno or 0))
            live.append([ev.code_hash, ev.perf_ns, 0])
        elif ev.kind in (RETURN, EXCEPTION):
            if not live:
                continue
            h, start, child_ns = live.pop()
            dur = max(0, ev.perf_ns - start)
            if h:
                self_ns[h] += max(0, dur - child_ns)
            if live:
                live[-1][2] += dur

    grand = sum(self_ns.values()) or 1
    rows = []
    for h, s in sorted(self_ns.items(), key=lambda kv: -kv[1])[:limit]:
        name, filename, lineno = names.get(h, (h, "", 0))
        rows.append(
            {
                "code_hash": h,
                "name": name,
                "file": filename,
                "line": lineno,
                "self_ns": s,
                "self_ms": round(s / 1e6, 4),
                "pct": round(100.0 * s / grand, 3),
            }
        )
    return rows


def exception_summary(events: Iterable[TraceEvent], *, limit: int = 20) -> list[dict]:
    """Exceptions by type and by the frame that raised them.

    Deduplicated by ``(type, frame)`` with a count, because a retry loop
    raising the same exception 5000 times is one bug, not 5000.
    """
    counts: Counter = Counter()
    where: dict = {}
    for ev in events:
        if ev.kind != EXCEPTION or not ev.exception_type:
            continue
        key = (ev.exception_type, ev.code_hash or "", ev.lineno or 0)
        counts[key] += 1
        where.setdefault(key, (ev.qualname or ev.name, ev.filename, ev.short_filename, ev.exception_repr))
    out = []
    for key, n in counts.most_common(limit):
        name, filename, short, text = where[key]
        out.append(
            {
                "type": key[0],
                "raised_in": name,
                "file": filename,
                "location": f"{short}:{key[2]}",
                "count": n,
                "example": text,
            }
        )
    return out


def recursion_report(events: Sequence[TraceEvent]) -> dict[str, Any]:
    """Find the deepest repeated frame and report max depth per code object.

    Recursion shows up in a trace as the same ``code_hash`` appearing several
    times in one live stack.  Tracking max-live-count per hash is O(1) per event
    and finds it exactly.
    """
    live_counts: Counter = Counter()
    best: dict = {}
    max_depth = 0
    for ev in events:
        if ev.kind == CALL:
            h = ev.code_hash
            if not h:
                continue
            live_counts[h] += 1
            if live_counts[h] > best.get(h, 0):
                best[h] = live_counts[h]
            max_depth = max(max_depth, sum(live_counts.values()))
        elif ev.kind == RETURN:
            h = ev.code_hash
            if h and live_counts.get(h):
                live_counts[h] -= 1
    recursive = {h: d for h, d in best.items() if d > 1}
    return {
        "max_stack_depth": max_depth,
        "recursive_functions": len(recursive),
        "deepest": sorted(recursive.items(), key=lambda kv: -kv[1])[:10],
    }


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------


def render_table(
    rows: Sequence[dict],
    columns: Sequence[str],
    *,
    headers: Sequence[str] | None = None,
    max_width: int = 200,
) -> str:
    """Render dict rows as an aligned text table.

    Left-aligned for text, right-aligned for numbers.  That detail matters more
    than it sounds: a right-aligned column of integers of differing magnitude
    lines up on the decimal point, and scanning the magnitudes is the whole
    reason you are looking at the table.
    """
    if not rows:
        return "(no rows)"
    headers = list(headers or [c.replace("_", " ") for c in columns])
    cells: list[list[str]] = []
    for r in rows:
        cells.append([_fmt(r.get(c)) for c in columns])
    numeric = [
        all(_is_num(cells[i][j]) for i in range(len(cells)))
        for j in range(len(columns))
    ]
    widths = []
    for j in range(len(columns)):
        w = max(len(headers[j]), *(len(row[j]) for row in cells))
        widths.append(min(w, max_width))

    def line(sep_left: str, sep_mid: str, sep_right: str) -> str:
        return sep_left + sep_mid.join("-" * (w + 2) for w in widths) + sep_right

    out = [line("+", "+", "+")]
    out.append("| " + " | ".join(h.ljust(widths[j]) for j, h in enumerate(headers)) + " |")
    out.append(line("+", "+", "+"))
    for row in cells:
        parts = []
        for j, cell in enumerate(row):
            parts.append(cell.rjust(widths[j]) if numeric[j] else cell.ljust(widths[j]))
        out.append("| " + " | ".join(parts) + " |")
    out.append(line("+", "+", "+"))
    return "\n".join(out)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if v != 0 and (abs(v) < 1e-3 or abs(v) >= 1e6):
            return f"{v:.3e}"
        return f"{v:.3f}".rstrip("0").rstrip(".") or "0"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _is_num(s: str) -> bool:
    t = s.replace(",", "").replace("%", "").replace(".", "").replace("-", "").replace("e-", "")
    return t.isdigit() or (t == "" and s in ("-", ""))
