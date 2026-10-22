"""Call graph as Graphviz DOT.

``graphviz`` is the right tool for one specific question that a flame graph
cannot answer: **"what is the shape of this codebase's call structure?"**
Recursion shows as a loop.  Coupling shows as a dense blob.  An accidental
import-time call shows as an edge into a module body.  Fan-out shows as
broom-shaped.

Why DOT is worth hand-writing rather than shelling out to ``pydot``
--------------------------------------------------------------------

Because the interesting decisions are all about *suppressing* noise, and they are
policy, not formatting:

*   self-loops are collapsed into a per-node "recur xN" label instead of drawn
    as circular edges, which turn every graph into a hairball;
*   nodes are labelled with the file and line so you can click through;
*   edge width is call count, which makes hot couplings obvious;
*   ``min_calls`` prunes the long tail -- a real call graph of a real program has
    thousands of nodes and you are looking for the thirty that matter.

Everything is rendered to a string; ``render`` shells out to ``dot`` only if you
ask it to.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from framecraft.core.events import TraceEvent
from framecraft.graph.callgraph import CallGraph, build_call_graph

__all__ = ["DOTSink", "callgraph_dot", "render_dot"]


def callgraph_dot(
    graph: CallGraph,
    *,
    name: str = "callgraph",
    min_calls: int = 1,
    show_cycles: bool = True,
    max_nodes: int = 200,
    rankdir: str = "LR",
    fontname: str = "Helvetica",
) -> str:
    """Render a :class:`CallGraph` to DOT source.

    Parameters
    ----------
    min_calls:
        Drop edges seen fewer than this many times.  Start at 2 or 3 for
        anything real.
    max_nodes:
        Keep the ``max_nodes`` most-called nodes.  Graphviz's layout is O(n^2)
        in practice past a few hundred nodes, so an unpruned graph of a real
        program takes minutes and produces an unreadable image.
    show_cycles:
        Annotate recursive nodes and colour their edges.  A cycle is the single
        most structurally interesting fact in a call graph.
    """
    ranked = sorted(graph.nodes.values(), key=lambda n: (-n.ncalls, n.qualname))
    keep = {n.code_hash for n in ranked[:max_nodes]}
    recursive: set[str] = set()
    cycles: list[list[str]] = []
    if show_cycles:
        cycles = graph.find_cycles()
        for c in cycles:
            recursive.update(c)

    graph_id = _dot_ident(name)
    label = _dot_escape(name)
    kept_nodes = [n for n in ranked if n.code_hash in keep]
    lines: list[str] = [
        f"digraph {graph_id} {{",
        f"  rankdir={rankdir};",
        f'  graph [fontname="{fontname}", labelloc="t", label="{label}", '
        f'nodesep=0.25, ranksep=0.6, bgcolor="white"];',
        f'  node [fontname="{fontname}", shape=box, style="rounded,filled", fillcolor="#f7f7f7"];',
        f'  edge [fontname="{fontname}", color="#666"];',
    ]

    for n in kept_nodes:
        attrs = [
            f'label="{_dot_escape(n.qualname)}\\n{_dot_escape(_short(n.filename))}:{n.firstlineno}'
            f'\\ncalls={n.ncalls:,}"',
            f'tooltip="{_dot_escape(f"{n.qualname}  ncalls={n.ncalls}")}"',
        ]
        if n.code_hash in recursive:
            attrs += ['color="#c0392b"', "penwidth=2", 'fillcolor="#fdecea"']
        if n.exceptions:
            attrs.append('shape="octagon"')
            attrs.append(f'tooltip="{_dot_escape(n.qualname + f" raised {n.exceptions} exception(s)")}"')
        lines.append(f'  "{n.code_hash}" [{", ".join(attrs)}];')

    edge_count = 0
    for (a, b), count in sorted(graph.edges.items(), key=lambda kv: -kv[1]):
        if a not in keep or b not in keep or count < min_calls:
            continue
        attrs = [f'label="{count:,}"', f'penwidth={min(4.0, 0.6 + count / 12):.2f}']
        if a == b:
            attrs += ['style=dashed', 'color="#c0392b"']
        lines.append(f'  "{a}" -> "{b}" [{", ".join(attrs)}];')
        edge_count += 1

    if cycles:
        lines.append(f"  // cycles: {len(cycles)} (recursive nodes highlighted red)")
    lines.append(f"  // nodes={len(kept_nodes)} edges={edge_count} total_nodes={len(graph.nodes)}")
    lines.append("}")
    return "\n".join(lines)


def _dot_ident(s: str) -> str:
    """Turn an arbitrary string into a valid DOT graph identifier.

    DOT identifiers must match ``[A-Za-z_][A-Za-z0-9_]*``.  A Windows path used
    verbatim produces ``digraph C:\\Users\\...\\cg.dot {`` which Graphviz
    rejects with a parse error -- and the resulting message ("syntax error near
    'dot'") points at the *filename*, which is a genuinely confusing thing to
    debug.
    """
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in s)
    if not out or not (out[0].isalpha() or out[0] == "_"):
        out = "g_" + out
    return out[-64:]  # keep it under Graphviz's internal name length budget


def _dot_escape(s: str) -> str:
    """Escape a string for a DOT double-quoted label.

    Note this is **not** :func:`html.escape`.  DOT has its own escape syntax
    (``\\``, ``\\"``, ``\\n``); HTML entities pass through Graphviz literally, so
    HTML-escaping makes ``<string>`` render as ``&lt;string&gt;``.  Angle
    brackets are legal in DOT labels and only need the quote and backslash
    handled.
    """
    out = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    out = "".join(ch if ch >= " " else " " for ch in out)
    return out


def _short(path: str) -> str:
    return (path or "?").replace("\\", "/").rsplit("/", 1)[-1]


def render_dot(dot_source: str, out_base: str, *, engine: str = "dot") -> str | None:
    """Shell out to Graphviz.  Returns the output path, or ``None`` if absent.

    Returns ``None`` rather than raising when Graphviz is not installed: a
    missing optional tool should degrade the report, not break the run.  The DOT
    source is still returned to the caller for other consumers.
    """
    exe = shutil.which(engine)
    if not exe:
        return None
    fmt = out_base.rsplit(".", 1)[-1] if "." in out_base else "svg"
    stem = out_base[: -(len(fmt) + 1)] if out_base.endswith("." + fmt) else out_base
    proc = subprocess.run(
        [exe, f"-T{fmt}", "-o", out_base],
        input=dot_source.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return stem + f".{fmt}"


class DOTSink:
    """Accumulate events, emit a call-graph DOT file on ``close()``.

    Unlike the other sinks this one is *not* really streaming: a call graph
    needs the whole run before it can be drawn.  It still implements ``write``
    so it plugs into ``Tracer.add_sink``, and it caps its own memory by
    forgetting events past ``max_events`` and saying so in the output.
    """

    def __init__(
        self,
        path: str = "callgraph.dot",
        *,
        min_calls: int = 1,
        max_nodes: int = 200,
        max_events: int = 2_000_000,
        render: bool = False,
        **dot_kwargs: Any,
    ) -> None:
        self.path = path
        self.min_calls = min_calls
        self.max_nodes = max_nodes
        self.max_events = max_events
        self.render = render
        self.dot_kwargs = dot_kwargs
        self._events: list[TraceEvent] = []
        self.truncated = False

    def write(self, event: TraceEvent) -> None:
        if len(self._events) >= self.max_events:
            if not self.truncated:
                self._truncated_from = len(self._events)
            # Halve the buffer: keeps a bounded tail without unbounded growth.
            del self._events[: len(self._events) // 2]
            self.truncated = True
        self._events.append(event)

    def close(self) -> str:
        graph = build_call_graph(self._events)
        src = callgraph_dot(
            graph, name=self.path, min_calls=self.min_calls, max_nodes=self.max_nodes, **self.dot_kwargs
        )
        if self.truncated:
            src = src[:-1] + f"  // WARNING: trace truncated at {self.max_events} events\n}}"
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(src)
        if self.render:
            render_dot(src, self.path.rsplit(".", 1)[0])
        return src

    def __repr__(self) -> str:
        return f"<DOTSink events={len(self._events)} path={self.path!r}>"
