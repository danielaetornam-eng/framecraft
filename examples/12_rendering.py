#!/usr/bin/env python3
"""Example 12 -- Rendering: flame graphs, call graphs, and the HTML report.

Run me::

    python examples/12_rendering.py
    python examples/12_rendering.py --outdir out

Three output formats, three questions.

``flame.svg``
    "Where did the time go?"  Width is the share of observations under that
    stack prefix.  Built from :class:`~framecraft.graph.StackTree`.

``callgraph.dot``
    "What is the shape of the code?"  Nodes are functions, edges are call sites.
    Recursion shows as a cycle, coupling as a dense blob, fan-out as a broom.
    Render with ``dot -Tsvg -O out/callgraph.dot``.

``report.html``
    "What happened?"  Everything above plus counts, hot lines, exceptions and a
    filterable event log, in one file with no external assets.

The flame graph algorithm, from scratch
---------------------------------------

A flame graph is a tidy tree drawn sideways:

*   each node is a stack prefix with a **weight** (observations of that exact
    prefix);
*   a node's width is proportional to its **subtree total**, and it occupies the
    same horizontal interval as all its descendants -- so widths are cumulative;
*   rows are depth, with depth 0 at the **bottom**, which is what makes the graph
    read like a call stack;
*   children are laid out left-to-right inside the parent's interval, **sorted by
    weight**.  Unsorted layout produces a striped mess in which each colour band
    is interleaved; sorting is most of what makes a flame graph readable.

:func:`framecraft.sinks.flame.layout` is that algorithm as a pure function from a
tree to a list of rectangles, which is why ``tests/test_flame.py`` can assert
exact pixel positions instead of eyeballing an image.
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile

from framecraft import CALL, LINE, RETURN, FunctionTracer, TracerConfig
from framecraft.graph import build_call_graph, build_stack_tree
from framecraft.sinks import HTMLReport, SVGFlameSink, callgraph_dot, flame_svg, layout
from framecraft.sinks.base import NullSink

# ----------------------------------------------------------------------


def parse(text: str) -> list[int]:
    return [int(p) for p in text.split(",")]


def transform(rows: list[int]) -> list[int]:
    out = []
    for r in rows:
        if r % 2:
            out.append(r * 2)
        else:
            out.append(r // 2)
    return out


def aggregate(rows: list[int]) -> int:
    return sum(transform(rows))


def recurse(n: int) -> int:
    return 1 if n <= 1 else recurse(n - 1) + recurse(n - 2)


def workload() -> int:
    rows = parse("1,2,3,4,5,6,7,8,9,10")
    a = aggregate(rows)
    b = recurse(6)
    return a + b


# ----------------------------------------------------------------------


def demo_layout(outdir: str) -> None:
    print("=" * 78)
    print("1. THE LAYOUT ALGORITHM, AS DATA")
    print("=" * 78)
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN))) as t:
        for _ in range(3):
            workload()
    tree = build_stack_tree(t.events, min_weight=2)
    rects, px_per_unit = layout(tree, width=1200)
    print(f"  {tree.total} observations, {len(rects)} rectangles, "
          f"{px_per_unit:.4f} px per observation")
    print()
    print(f"  {'depth':>5} {'x':>8} {'w':>8} {'pct':>7}  label")
    for r in sorted(rects, key=lambda r: (r["depth"], r["x"]))[:18]:
        print(f"  {r['depth']:>5} {r['x']:>8.1f} {r['w']:>8.1f} {r['pct']:>6.2f}%  "
              f"{r['label'][:60]}")
    print()
    print("  Three invariants worth asserting in a test:")
    print("   * siblings tile their parent exactly: sum(child.w) == parent.w")
    print("   * a node never overlaps its sibling")
    print("   * depth 0 is the bottom row (largest y in the SVG)")
    print()
    print("  The strict tiling check lives in tests/test_analysis.py::TestFlameLayout,")
    print("  which asserts the same invariants with parent links available.")
    print()
    path = os.path.join(outdir, "flame.svg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(flame_svg(tree, title="framecraft example", unit="line events"))
    print(f"  wrote {path} ({os.path.getsize(path):,} bytes)")


def demo_streaming_flame(outdir: str) -> None:
    print()
    print("=" * 78)
    print("2. THE SAME GRAPH, STREAMED")
    print("=" * 78)
    path = os.path.join(outdir, "streamed.svg")
    sink = SVGFlameSink(path)
    # buffer=False plus a streaming sink: nothing is retained.
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN), buffer=False)) as t:
        t.add_sink(sink)
        for _ in range(3):
            workload()
    sink.close()
    print(f"  events emitted      {sink.count}")
    print(f"  events retained     {len(t.events)}   (buffer=False)")
    print(f"  wrote {path} ({os.path.getsize(path):,} bytes)")
    print()
    print("  SVGFlameSink maintains the live stack prefix incrementally, so it")
    print("  never needs the event list. That is what makes it usable for a trace")
    print("  of a program that produces a hundred million line events: the only")
    print("  thing that grows is the tree, and the tree is bounded by the number")
    print("  of distinct stack prefixes, not by the number of observations.")
    print()
    print("  Add SVGFlameSink to a long-running service and you get a live flame")
    print("  graph of production, for the cost of one integer increment per line.")


def demo_callgraph(outdir: str) -> None:
    print()
    print("=" * 78)
    print("3. CALL GRAPH AS DOT")
    print("=" * 78)
    with FunctionTracer(TracerConfig(events=(CALL, RETURN))) as t:
        workload()
    g = build_call_graph(t.events)
    src = callgraph_dot(g, name="framecraft_example", min_calls=1)
    path = os.path.join(outdir, "callgraph.dot")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print(src)
    print(f"  wrote {path}")
    print()
    print("  Render with:  dot -Tsvg -O " + path)
    from framecraft.sinks import render_dot

    out = render_dot(src, os.path.join(outdir, "callgraph"))
    if out:
        print(f"  rendered {out} (graphviz is installed)")
    else:
        print("  graphviz 'dot' is not on PATH, so only DOT source was written.")
        print("  That is the intended degradation: a missing optional tool must not")
        print("  break the report.")
    print()
    print("  Suppression is where the value is. A real call graph has thousands of")
    print("  nodes and you are looking for the thirty that matter, so:")
    print("   * min_calls drops the long tail")
    print("   * max_nodes keeps the most-called (Graphviz layout is ~O(n^2))")
    print("   * self-loops become dashed red, not circular arcs that turn the")
    print("     whole image into a hairball")
    print("   * cycle members get an octagon when they also raise")


def demo_html(outdir: str) -> None:
    print()
    print("=" * 78)
    print("4. THE HTML REPORT")
    print("=" * 78)
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN), capture_locals=True)) as t:
        workload()
    report = HTMLReport(t.events, dropped=t.dropped, errors=t.error_count,
                        title="framecraft example")
    path = report.write(os.path.join(outdir, "report.html"))
    html = report.render()
    print(f"  wrote {path} ({len(html):,} characters, self-contained)")
    print()
    print("  Structure:")
    for marker, label in (
        ("<div class=\"grid\">", "summary cards"),
        ("<section><h2>Flame graph", "inline flame graph SVG"),
        ("<h2>Hot lines", "hot lines table"),
        ("<h2>Exclusive time", "exclusive-time table"),
        ("<h2>Busiest functions", "busiest functions table"),
        ("<h2>Exceptions", "exceptions table"),
        ("<h2>Event log", "filterable event log"),
    ):
        print(f"    {'yes' if marker in html else 'MISSING':>7}  {label}")
    print()
    print("  Constraints this report keeps on purpose:")
    print("   * one file, no CDN, no build step, opens offline -- because trace")
    print("     reports end up attached to bug reports on machines with no network")
    print("   * no framework; the filter is ~10 lines of vanilla JS")
    print("   * captured locals are OFF by default. A variable-capturing tracer is")
    print("     a secret-exfiltration tool, and this report is about to be")
    print("     attached to an issue.")
    print("   * `dropped` and `errors` are in the header, because a trace that lost")
    print("     events and does not say so is worse than no trace.")


def demo_sink_composition() -> None:
    print()
    print("=" * 78)
    print("5. ONE TRACER, MANY SINKS")
    print("=" * 78)
    from framecraft.sinks import CountingSink

    counting = CountingSink()
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN), buffer=False)) as t:
        t.add_sink(counting)
        t.add_sink(NullSink())
        workload()
    print(f"  CountingSink: {counting}")
    print()
    print("  Sinks are called in registration order after the buffer. The whole")
    print("  interface is one method, `write(event)`, which is why a sink can be a")
    print("  list append, an f.write, a socket send, or nothing at all.")
    print()
    print("  `CountingSink` is the first thing to install when a tracer appears")
    print("  to do nothing: if its counts are zero the problem is filtering or")
    print("  hook installation, not your renderer.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outdir", default=None, help="where to write output (default: a temp dir)")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir")
    ns = ap.parse_args()
    outdir = ns.outdir or tempfile.mkdtemp(prefix="framecraft-12-")
    os.makedirs(outdir, exist_ok=True)
    print(f"output directory: {outdir}\n")
    demo_layout(outdir)
    demo_streaming_flame(outdir)
    demo_callgraph(outdir)
    demo_html(outdir)
    demo_sink_composition()
    if not ns.keep and not ns.outdir:
        shutil.rmtree(outdir, ignore_errors=True)
    else:
        print(f"\noutputs left in {outdir}")
    print("=" * 78)
    print("Next: 13 -- capturing variables safely, and redacting them")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
