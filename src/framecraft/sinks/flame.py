"""Flame graph layout and SVG rendering, implemented from first principles.

If you already have Brendan Gregg's ``flamegraph.pl`` this module still earns
its place, for three reasons:

1.  It consumes a live :class:`~framecraft.core.events.TraceEvent` stream, so you
    never have a trace file at all.
2.  It is testable.  The layout is a pure function from a tree to a list of
    rectangles, which means ``tests/test_flame.py`` can assert exact pixel
    positions.
3.  It documents the algorithm, which is the point of this repository.

The algorithm
-------------

A flame graph is a **tidy tree drawn sideways**.

*   Each node is a stack prefix with a *value* (here: how many times that exact
    prefix was observed).
*   A node's width is proportional to its *subtree total*, and it occupies the
    same horizontal interval as all its descendants.  So widths are cumulative,
    not per-node.
*   Rows are assigned by depth: depth 0 at the bottom, deeper rows above.
*   Children are laid out left to right inside the parent's interval.  Sorting
    children by value descending (or keeping them sorted so similar stacks
    are adjacent) is what makes the graph *readable* -- unsorted layout produces
    a striped mess where each colour band is interleaved.

Implementation::

    x = node.x                      # parent's left edge
    for child in sorted_children:
        w = width_of(child)         # subtree total * px_per_unit
        child.rect = (x, y, x + w, y + row_height)
        x += w
    node.width = x - node.x

Root nodes with identical labels are **merged** before layout.  Merging is not
cosmetic: ``main -> parse -> process`` called from two different entry points
must appear as one ``parse`` node, or half the profile is duplicated and the
percentages are wrong.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from typing import Any

from framecraft.core.events import CALL, LINE, RETURN, TraceEvent
from framecraft.graph.callgraph import StackTree, _Frame_

__all__ = ["SVGFlameSink", "flame_svg", "layout", "Rect"]

Rect = tuple[float, float, float, float]  # x, y, width, height


class SVGFlameSink:
    """Streaming flame graph sink.

    Renders directly from the event stream, maintaining the live stack as it
    goes, so you get a flame graph from a program that produced a hundred
    million line events without ever materialising a tree.

    ::

        t = FunctionTracer(events=("call", "line", "return"))
        t.add_sink(SVGFlameSink("flame.svg"))
        work()
        # flame.svg is written on close()

    The measured unit is **line events**, so the width of a frame is "how much
    of the traced execution happened inside it".  If you want wall-clock instead,
    drive it from a :class:`~framecraft.trace.sampling.SamplingTracer` and call
    :func:`flame_svg` with its stacks -- a flame graph from samples is honest
    about time, whereas this one is honest about instruction counts.
    """

    def __init__(
        self,
        path: str = "flame.svg",
        *,
        min_width_px: float = 0.2,
        title: str = "framecraft flame graph",
        unit: str = "executions",
    ) -> None:
        self.path = path
        self.min_width_px = min_width_px
        self.title = title
        self.unit = unit
        self.tree = StackTree("all")
        self._live: list[tuple[str, str]] = []
        self.count = 0

    # ------------------------------------------------------------------
    def write(self, event: TraceEvent) -> None:
        if event.kind == CALL:
            self._live.append((event.qualname or event.name, event.code_hash or ""))
        elif event.kind == RETURN and self._live:
            self._live.pop()
        elif event.kind == LINE and self._live:
            loc = f"{event.short_filename}:{event.lineno}"
            # Key on (code_hash, line) so each source line is its own node; see
            # the note in graph.callgraph.build_stack_tree.
            self.tree.add_path(
                [(f"{n} ({loc})", (h, event.lineno)) for n, h in self._live], weight=1
            )
            self.count += 1

    def close(self) -> None:
        """Render and write the SVG."""
        svg = flame_svg(self.tree, title=self.title, unit=self.unit, min_width_px=self.min_width_px)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(svg)

    def __repr__(self) -> str:
        return f"<SVGFlameSink paths={self.path} count={self.count}>"


def layout(
    tree: StackTree,
    *,
    width: int = 1200,
    row_height: int = 18,
    min_width_px: float = 0.2,
) -> tuple[list[dict], float]:
    """Turn a :class:`StackTree` into positioned rectangles.

    Returns ``(rects, px_per_unit)``.  ``px_per_unit`` is reported so the caller
    can label the x axis honestly -- a flame graph that does not tell you what a
    pixel is worth is a picture, not a measurement.

    Depth 0 is drawn at the **bottom**, which is the convention that makes the
    graph read like a call stack.

    Why ``node.value`` and not ``sum(child.value)``
    ----------------------------------------------
    :meth:`StackTree.add_path` increments ``value`` on *every* node along the
    path, not only the last one.  So ``node.value`` is already the cumulative
    observation count for that prefix, and ``sum(child.value)`` equals it too.

    Recomputing the subtree from the children is not merely redundant -- it is
    wrong for leaves, whose child sum is zero, and therefore renders every leaf
    as a zero-width bar and every parent's width as the sum of its *siblings'
    worth* of children while the cursor fails to advance.  Both failure modes
    look like "the profile is very flat" rather than like a bug, which is why
    this is worth stating.
    """
    total = tree.root.value
    if total <= 0:
        return [], 0.0
    px_per_unit = (width - 10) / total
    rects: list[dict] = []

    def emit(node: _Frame_, x0: float, depth: int) -> float:
        """Lay out ``node``'s subtree starting at ``x0``; return its own width.

        The return value is the node's width, **not** the sum of how far the
        child cursor advanced.  Those two are equal for a node with children and
        differ by exactly the node's width for a leaf, so returning the advance
        makes every leaf measure zero -- and since siblings advance the cursor by
        this return value, every sibling then lands on the same x and the whole
        graph stacks into one column.

        Returning ``width_px`` is correct because ``add_path`` increments
        ``value`` on every node along the path, so the children of a node tile
        it exactly: ``sum(child.width) == node.width``.
        """
        width_px = node.value * px_per_unit
        if node is not tree.root:
            # Prune BEFORE appending.  Emitting the rectangle and then deciding
            # not to descend leaves a sub-pixel sliver on the chart -- invisible,
            # unclickable, and still present in the data, which is worse than
            # absent.
            if width_px < min_width_px:
                return width_px  # too small to show; skip it and its children
            rects.append(
                {
                    "x": x0,
                    "y": -((depth + 1) * row_height),
                    "w": width_px,
                    "h": row_height - 1,
                    "depth": depth,
                    "label": node.label,
                    "value": node.value,
                    "self_value": node.value,
                    "pct": 100.0 * node.value / total,
                    "code_hash": node.code_hash,
                }
            )
        cursor = x0
        # Sort by weight descending, then by label. Sorting is not cosmetic:
        # unsorted layout interleaves every colour band and the graph becomes
        # unreadable.
        for c in sorted(node.children.values(), key=lambda c: (-c.value, c.label)):
            cursor += emit(c, cursor, depth + 1)
        return width_px

    emit(tree.root, 5.0, 0)
    return rects, px_per_unit


#: A warm, colour-blind-safe ramp.  Hashing the label gives a stable colour per
#: function so the same function is the same colour across renders -- which is
#: what lets your eye learn the palette instead of re-reading the legend.
_PALETTE = (
    "#e8a33d", "#d9743f", "#c25b4a", "#b04a63", "#9a4a80",
    "#7f4b95", "#6355a0", "#4a63a5", "#3a72a8", "#3583a3",
    "#3d9498", "#4da389", "#65ad76", "#85b566", "#a5b95a",
    "#c2b854", "#d9b252", "#e6a24d",
)


def _colour(label: str) -> str:
    h = 0
    for ch in label:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return _PALETTE[h % len(_PALETTE)]


def flame_svg(
    tree: StackTree,
    *,
    width: int = 1200,
    row_height: int = 18,
    title: str = "flame graph",
    unit: str = "executions",
    min_width_px: float = 0.2,
    show_title: bool = True,
) -> str:
    """Render a :class:`StackTree` as a standalone SVG string.

    Pure function: no I/O, no globals.  That is what makes the layout testable
    and what lets you render the same tree to a string for a web UI.
    """
    rects, px_per_unit = layout(
        tree, width=width, row_height=row_height, min_width_px=min_width_px
    )
    if not rects:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="80"></svg>'
        )
    max_depth = max(r["depth"] for r in rects) + 1
    height = max_depth * row_height + (34 if show_title else 6)
    baseline = max_depth * row_height

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Verdana,DejaVu Sans,sans-serif">',
        "<style>",
        ".fr{fill:#f5f5f5;stroke:#333;stroke-width:.4}",
        ".ft{font-size:11px;fill:#111;pointer-events:none}",
        ".h1{font-size:15px;font-weight:bold;fill:#222}",
        ".h2{font-size:11px;fill:#666}",
        ".bg{fill:#fafafa}",
        "</style>",
        f'<rect class="bg" x="0" y="0" width="{width}" height="{height}"/>',
    ]
    if show_title:
        parts.append(
            f'<text class="h1" x="6" y="16">{html.escape(title)} '
            f'&#8212; {tree.total:,} {html.escape(unit)}</text>'
        )
        parts.append(
            f'<text class="h2" x="6" y="30">{px_per_unit:.5f} px per {html.escape(unit)}'
            f' &#183; width = subtree share</text>'
        )

    for r in sorted(rects, key=lambda r: (r["depth"], r["x"])):
        y = baseline + r["y"]
        fill = _colour(r["label"])
        parts.append(
            f'<g><title>{html.escape(self_title(r))}</title>'
            f'<rect class="fr" x="{r["x"]:.2f}" y="{y:.2f}" '
            f'width="{max(0.0, r["w"] - 0.3):.2f}" height="{r["h"]}" fill="{fill}"/>'
        )
        if r["w"] > 34:
            label = r["label"]
            max_chars = max(1, int((r["w"] - 6) / 6.1))
            if len(label) > max_chars:
                label = label[: max_chars - 1] + "…"
            parts.append(
                f'<text class="ft" x="{r["x"] + 3:.2f}" y="{y + r["h"] - 4:.2f}">{html.escape(label)}</text>'
            )
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def self_title(r: dict) -> str:
    """The ``<title>`` tooltip text for one rectangle."""
    pct = r["pct"]
    if pct >= 10:
        pct_s = f"{pct:.0f}%"
    elif pct >= 0.1:
        pct_s = f"{pct:.1f}%"
    else:
        pct_s = f"{pct:.3f}%"
    return f"{r['label']}  {r['value']:,} ({pct_s})"


def stacks_to_tree(stacks: Iterable[Sequence[Any]]) -> StackTree:
    """Build a :class:`StackTree` from an iterable of innermost-first stacks."""
    t = StackTree("all")
    for s in stacks:
        t.add_stack(s)
    return t
