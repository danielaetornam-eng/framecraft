"""Self-contained HTML trace report.

One file, no assets, no JavaScript framework, opens offline.  That constraint is
deliberate: trace reports get attached to bug reports and end up on machines
with no network, and a report that needs a CDN is a report nobody opens.

What goes in
------------

*   A **summary panel** -- event counts by kind, span, distinct functions, and
    the ``dropped``/``errors`` counters.  Always at the top, because if the
    trace is incomplete you need to know before you trust the rest.
*   A **flame graph** rendered inline as SVG by :mod:`framecraft.sinks.flame`.
*   **Hot lines** and **exclusive times** tables.
*   **Exceptions**, if any.
*   An **event log** with a filter box -- plain ``<input oninput=...>`` and
    ~20 lines of vanilla JS, no framework.

What deliberately does not go in
--------------------------------

Captured locals, by default.  They are the biggest and most sensitive part of a
trace, they are already in your JSONL file if you asked for them, and baking
them into an HTML artifact you are about to attach to a public issue is a bad
default.  Pass ``include_locals=True`` when you know the report is private.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Any

from framecraft.analysis.stats import (
    exception_summary,
    exclusive_times,
    hot_functions,
    hot_lines,
    summarise_events,
)
from framecraft.core.events import TraceEvent
from framecraft.graph.callgraph import build_stack_tree
from framecraft.sinks.flame import flame_svg

__all__ = ["HTMLReport", "render_report"]

_CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#252a34;--fg:#dfe3ea;--muted:#8b93a3;
--accent:#7aa2f7;--warn:#e0af68;--bad:#f7768e;--good:#9ece6a}
@media (prefers-color-scheme: light){:root{--bg:#f7f8fa;--panel:#fff;--line:#e3e6ec;
--fg:#1a1d23;--muted:#5c6370;--accent:#2f5fd0;--warn:#a06000;--bad:#c0392b;--good:#2e7d32}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{padding:18px 22px;border-bottom:1px solid var(--line);background:var(--panel);
position:sticky;top:0;z-index:5}
h1{margin:0;font-size:17px;letter-spacing:.2px}
h2{margin:0 0 10px;font-size:14px;color:var(--accent);text-transform:uppercase;
letter-spacing:.8px}
.sub{color:var(--muted);font-size:12px;margin-top:4px}
.wrap{padding:20px 22px;max-width:1500px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
margin-bottom:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.card .v{font-size:20px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.card.warn .v{color:var(--warn)} .card.bad .v{color:var(--bad)} .card.good .v{color:var(--good)}
section{margin-bottom:26px}
table{border-collapse:collapse;width:100%;font-size:12.5px;
font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--muted);font-weight:600;padding:6px 10px;
border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
td{padding:4px 10px;border-bottom:1px solid color-mix(in srgb,var(--line) 55%,transparent)}
tr:hover td{background:color-mix(in srgb,var(--accent) 8%,transparent)}
td.num,th.num{text-align:right}
.bar{height:6px;background:var(--accent);border-radius:3px;display:inline-block;
vertical-align:middle}
pre{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;
overflow:auto;max-height:60vh;font-size:12px}
input{background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:6px 10px;width:280px;font:inherit;font-size:13px}
.flame{overflow-x:auto;background:var(--panel);border:1px solid var(--line);
border-radius:8px;padding:8px}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}
.note{color:var(--muted);font-size:12px;margin:6px 0 0}
.badge-ok{color:var(--good)} .badge-bad{color:var(--bad)}
"""


def _rows_table(rows: Sequence[dict], cols: Sequence[tuple[str, str, bool]], *, max_pct: str | None = None) -> str:
    """``cols`` is ``[(key, header, is_numeric)]``."""
    if not rows:
        return '<p class="note">(none)</p>'
    mx = max((r.get(max_pct, 0) or 0 for r in rows), default=1) or 1
    head = "".join(
        f'<th class="num">{html.escape(h)}</th>' if num else f"<th>{html.escape(h)}</th>"
        for _k, h, num in cols
    )
    body = []
    for r in rows:
        tds = []
        for k, _h, num in cols:
            v = r.get(k)
            txt = "-" if v is None else html.escape(str(v))
            if k == max_pct:
                w = 100.0 * (v or 0) / mx
                txt = f'{txt} <span class="bar" style="width:{max(2, w * 0.9):.0f}px"></span>'
            tds.append(f'<td class="num">{txt}</td>' if num else f"<td>{txt}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_report(
    events: Sequence[TraceEvent],
    *,
    title: str = "framecraft trace report",
    dropped: int = 0,
    errors: int = 0,
    include_locals: bool = False,
    max_log_rows: int = 4000,
    min_flame_weight: int = 1,
    flame_only: bool = False,
) -> str:
    """Render a complete HTML report as a single string."""
    summary = summarise_events(events, dropped=dropped, errors=errors)
    summary_html = _summary_cards(summary)

    if not flame_only:
        tree = build_stack_tree(events, min_weight=min_flame_weight)
        svg = flame_svg(tree, title="execution flame graph", unit="line events")
    else:
        svg = ""

    hot_lines_html = _rows_table(
        hot_lines(events, limit=40),
        [("location", "location", False), ("count", "count", True), ("pct", "% of lines", True)],
        max_pct="count",
    )
    exclusive_html = _rows_table(
        exclusive_times(events, limit=40),
        [
            ("name", "function", False),
            ("calls", "calls", True),
            ("total_ms", "total ms", True),
            ("self_ms", "self ms", True),
            ("pct_total", "% incl", True),
            ("pct_self", "% self", True),
        ],
        max_pct="total_ns",
    )
    hotfn_html = _rows_table(
        hot_functions(events, limit=30),
        [
            ("name", "function", False),
            ("location", "location", False),
            ("count", "events", True),
            ("pct", "% events", True),
        ],
        max_pct="count",
    )
    exc = exception_summary(events)
    exc_html = (
        _rows_table(
            exc,
            [
                ("type", "exception", False),
                ("raised_in", "raised in", False),
                ("location", "location", False),
                ("count", "count", True),
                ("example", "example", False),
            ],
        )
        if exc
        else '<p class="note">No exception events were recorded.</p>'
    )

    log_rows = []
    for ev in list(events)[:max_log_rows]:
        locs = ""
        if include_locals and ev.locals:
            locs = "  " + " ".join(f"{b.name}={b.value}" for b in ev.locals)
        log_rows.append(
            f'<tr data-kind="{html.escape(ev.kind)}"><td class="num">{ev.seq}</td>'
            f"<td><span class=\"tag\">{html.escape(ev.kind)}</span></td>"
            f"<td class=\"num\">{ev.depth}</td>"
            f"<td>{html.escape(ev.qualname or ev.name)}</td>"
            f"<td>{html.escape(ev.location)}</td>"
            f"<td>{html.escape((ev.retval_repr or ev.exception_repr or ev.opcode or ev.c_name or '')[:120])}"
            f"{html.escape(locs[:400])}</td></tr>"
        )

    from framecraft import __version__

    warnings = []
    if dropped:
        warnings.append(
            f'<span class="badge-bad">{dropped:,} events were dropped</span> '
            f"({summary['coverage_ratio'] * 100:.3f}% coverage) -- percentages below are "
            f"lower bounds, not totals."
        )
    if errors:
        warnings.append(
            f'<span class="badge-bad">{errors:,} tracer errors</span> -- set '
            f"FRAMECRAFT_DEBUG=1 to see them."
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <div class="sub">framecraft {html.escape(__version__)} &middot;
  {summary['total_events']:,} events &middot; {' &middot; '.join(warnings) if warnings else 'complete trace'}
  </div>
</header>
<div class="wrap">
  <div class="grid">{summary_html}</div>
  <section><h2>Flame graph</h2><div class="flame">{svg}</div>
    <p class="note">Width is the share of line events that occurred inside that
    stack prefix. Hover a rectangle for exact numbers.</p></section>
  <section><h2>Hot lines</h2>{hot_lines_html}</section>
  <section><h2>Exclusive time</h2>{exclusive_html}
    <p class="note">Wall clock from <code>call</code> to <code>return</code>,
    excluding time attributed to callees. Includes tracer overhead.</p></section>
  <section><h2>Busiest functions</h2>{hotfn_html}</section>
  <section><h2>Exceptions</h2>{exc_html}</section>
  <section><h2>Event log</h2>
    <input id="q" placeholder="filter by function, kind, file, line&#8230;"
      oninput="(function(){{var q=this.value.toLowerCase();var r=document.querySelectorAll('#log tbody tr');
      for(var i=0;i<r.length;i++){{r[i].style.display=r[i].textContent.toLowerCase().indexOf(q)>=0?'':'none'}}}})()">
    <p class="note">Showing first {len(log_rows):,} of {summary['total_events']:,} events.</p>
    <pre><table id="log"><thead><tr><th class="num">seq</th><th>event</th>
    <th class="num">d</th><th>function</th><th>location</th><th>detail</th></tr></thead>
    <tbody>{''.join(log_rows)}</tbody></table></pre></section>
</div>
</body></html>"""


def _summary_cards(s: dict) -> str:
    def card(k: str, v: Any, cls: str = "") -> str:
        return f'<div class="card {cls}"><div class="k">{html.escape(k)}</div><div class="v">{v}</div></div>'

    cards = [
        card("events", f"{s['total_events']:,}"),
        card("span", f"{s['span_s']:.4f}s"),
        card("threads", s["threads"]),
        card("functions", f"{s['distinct_code_objects']:,}"),
        card("files", s["distinct_files"]),
        card("events/s", f"{s['events_per_s']:,.0f}" if s["events_per_s"] else "-"),
    ]
    for kind, n in sorted(s["events_by_kind"].items()):
        cards.append(card(kind, f"{n:,}"))
    cov = s["coverage_ratio"]
    cards.append(
        card(
            "coverage",
            f"{cov * 100:.3f}%",
            "good" if cov > 0.999 else ("warn" if cov > 0.9 else "bad"),
        )
    )
    if s["dropped"]:
        cards.append(card("dropped", f"{s['dropped']:,}", "bad"))
    if s["errors"]:
        cards.append(card("errors", f"{s['errors']:,}", "bad"))
    return "".join(cards)


class HTMLReport:
    """Build an HTML report from a buffered tracer.

    ::

        t = FunctionTracer(events=("call", "line", "return"), buffer=True)
        with t:
            work()
        HTMLReport(t.events, dropped=t.dropped, errors=t.error_count).write("report.html")

    Needs the buffer: unlike :class:`~framecraft.sinks.SVGFlameSink` this is a
    post-hoc renderer, because it aggregates.  For streaming output use the JSONL
    sink and run ``fc-trace report`` over the file.
    """

    def __init__(self, events: Sequence[TraceEvent], **kwargs: Any) -> None:
        self.events = events
        self.kwargs = kwargs

    def render(self) -> str:
        return render_report(self.events, **self.kwargs)

    def write(self, path: str) -> str:
        html_text = self.render()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html_text)
        return path

    def __repr__(self) -> str:
        return f"<HTMLReport events={len(self.events)}>"
