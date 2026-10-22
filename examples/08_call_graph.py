#!/usr/bin/env python3
"""Example 08 -- Call graphs: coupling, recursion, and what a graph cannot say.

Run me::

    python examples/08_call_graph.py
    python examples/08_call_graph.py --dot callgraph.dot

Two structures, two questions
-----------------------------

:class:`~framecraft.graph.CallGraph`
    Nodes are *functions*, edges are *call sites*.  Answers **coupling**
    questions: which function has 40 callees, which pair of modules is
    entangled, where is the recursion.

:class:`~framecraft.graph.StackTree`
    Nodes are *stack prefixes*.  Answers **cost** questions.  This is what a
    flame graph renders.

Why you cannot use one for both
-------------------------------

Consider::

    a -> b -> d
    a -> c -> d

The call graph has one node ``d`` with two callers.  A flame graph needs ``d`` to
appear **twice**, once under each path, because exclusive time depends on the
path taken.  Collapsing them would tell you ``d`` costs 10% of runtime -- a
number that is entirely a function of the call mix, and would be 1% if ``d``
were only ever called from ``b``.

Cycles
------

Recursion shows up as a cycle.  :meth:`CallGraph.find_cycles` uses an
**iterative** Tarjan SCC pass, because the obvious recursive DFS overflows on
precisely the deep-recursion workloads you most want to profile.  Every "my
profiler crashed on the buggy input" report is that stack overflow.
"""

from __future__ import annotations

import argparse

from framecraft import CALL, LINE, RETURN, FunctionTracer, TracerConfig
from framecraft.analysis import recursion_report, render_table
from framecraft.graph import build_call_graph, build_stack_tree
from framecraft.sinks import callgraph_dot

# ----------------------------------------------------------------------
# A workload with interesting structure: recursion, fan-out, and diamonds
# ----------------------------------------------------------------------


def fib(n: int) -> int:
    return n if n < 2 else fib(n - 1) + fib(n - 2)


def factorial(n: int) -> int:
    if n <= 1:
        return 1
    return n * factorial(n - 1)


def validate(row: list) -> bool:
    """A diamond: A calls B and C, both of which call D."""
    return check_types(row) and check_length(row)


def check_types(row: list) -> bool:
    return normalise(row) is not None


def check_length(row: list) -> bool:
    return len(normalise(row)) <= 10


def normalise(row: list) -> list | None:
    return [x for x in row if isinstance(x, int)]


def fanout(n: int) -> int:
    """One caller, many callees: high fan-out is a design smell."""
    total = 0
    total += fib(4)
    total += factorial(4)
    total += 1 if validate([1, 2, 3]) else 0
    return total


def workload() -> int:
    return fanout(3) + (1 if validate([1, 2]) else 0)


# ----------------------------------------------------------------------


def demo_graph() -> None:
    print("=" * 78)
    print("1. BUILDING A CALL GRAPH FROM AN EVENT STREAM")
    print("=" * 78)
    cfg = TracerConfig(events=(CALL, RETURN), include=lambda c: c.co_filename == __file__)
    with FunctionTracer(cfg) as t:
        workload()
    g = build_call_graph(t.events)
    print(f"  {len(g)} nodes, {len(g.edges)} edges, from {len(t.events)} events")
    print()
    rows = sorted(g.nodes.values(), key=lambda n: (-n.ncalls, n.qualname))
    print(render_table(
        [{"function": n.qualname, "calls": n.ncalls, "callees": len(n.callees),
          "callers": len(n.callers), "total_ms": round(n.total_ns / 1e6, 4),
          "self_ms": round(n.self_ns / 1e6, 4)}
         for n in rows],
        ["function", "calls", "callees", "callers", "total_ms", "self_ms"],
    ))
    print()
    print("  `callees` and `callers` are the coupling metrics. A node with 20")
    print("  callees is doing too much; a pair of nodes that call each other")
    print("  across a module boundary is a layering violation; a `callers` count")
    print("  of 1 on something named `util` means it is not a utility.")


def demo_recursion() -> None:
    print()
    print("=" * 78)
    print("2. CYCLES, AND WHY THE DETECTION IS ITERATIVE")
    print("=" * 78)
    cfg = TracerConfig(events=(CALL, RETURN), include=lambda c: c.co_filename == __file__)
    with FunctionTracer(cfg) as t:
        fib(6)
        factorial(4)
        workload()
    g = build_call_graph(t.events)
    cycles = g.find_cycles()
    print(f"  {len(cycles)} strongly connected component(s) of size > 1, or self-loops:")
    for c in cycles:
        names = [g.nodes[h].qualname if h in g.nodes else h for h in c]
        print(f"    {' -> '.join(names)} -> (back to start)")
    print()
    print("  Note the count: `fib(6)` alone is 25 calls, `factorial(4)` is 4, and")
    print("  `workload` calls both. The graph reports *structure*, not counts --")
    print("  `fib` has a self-edge with weight 24 and an incoming edge from")
    print("  `fanout`. Structure is what tells you the recursion is unbounded.")
    print()
    print(render_table([{"function": g.nodes[h].qualname, "max_depth": d}
                        for h, d in recursion_report(
                            _call_events(fib, 8) + _call_events(factorial, 5)
                        )["deepest"]],
                       ["function", "max_depth"]))
    print()
    print("  An iterative Tarjan pass is used because a recursive one would blow")
    print("  up on exactly the inputs you want to profile. Test it:")


def _call_events(fn, n):
    with FunctionTracer(TracerConfig(events=(CALL,), include=lambda c: c is fn.__code__)) as t:
        fn(n)
    return t.events


def demo_recursion_depth_guarded() -> None:
    """Find the actual recursion limit *without* killing the interpreter."""
    print()
    print("=" * 78)
    print("3. RECURSION DEPTH, MEASURED SAFELY")
    print("=" * 78)

    def recurse(n: int) -> int:
        return 1 if n == 0 else 1 + recurse(n - 1)

    depth = 0
    try:
        depth = recurse(10_000)
    except RecursionError as exc:
        depth = exc.__class__.__name__
    print(f"  asked for 10,000 frames, got: {depth}")
    print()
    print("  `sys.setrecursionlimit` counts *Python* frames, not C frames, and")
    print("  the default (1000) is a safety margin against C stack overflow -- not")
    print("  a meaningful budget. Raising it to make recursion 'work' is how you")
    print("  get a segfault instead of a RecursionError.")
    print()
    print("  For a tracer this matters in a specific way: a trace of deep recursion")
    print("  is a trace where your own analyser can overflow. Hence iterative")
    print("  SCC, iterative stack walks, and `sum(1 for _ in walk(frame))` rather")
    print("  than a recursive helper.")


def demo_diamonds() -> None:
    print()
    print("=" * 78)
    print("4. WHY A CALL GRAPH CANNOT BE RENDERED AS A FLAME GRAPH")
    print("=" * 78)
    cfg = TracerConfig(events=(CALL, LINE, RETURN), include=lambda c: c.co_filename == __file__)
    with FunctionTracer(cfg) as t:
        workload()
    g = build_call_graph(t.events)
    tree = build_stack_tree(t.events)
    normalise_calls = sum(n.ncalls for n in g.nodes.values() if n.qualname.endswith("normalise"))
    tree_occurrences = sum(
        1 for node, _d in tree.walk() if node.label.startswith("normalise")
    )
    print(f"  `normalise` in the CALL GRAPH:  1 node, {normalise_calls} calls total")
    print(f"  `normalise` in the STACK TREE:  {tree_occurrences} nodes "
          f"(one per distinct caller path)")
    print()
    print("  The call graph says 'normalise was called 3 times'. The stack tree says")
    print("  'normalise was reached from check_types once and from check_length")
    print("  twice'. Only the second can be turned into a width, because only the")
    print("  second knows which callers contributed.")
    print()
    print("  If you collapse the graph node you get one bar whose width is the sum,")
    print("  which reads as 'normalise is 30% of the program'. Depending on the")
    print("  call mix that number could be anything. Collapsing destroys the")
    print("  information the visualisation depends on.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dot", help="write a Graphviz DOT call graph here")
    ns = ap.parse_args()
    demo_graph()
    demo_recursion()
    demo_recursion_depth_guarded()
    demo_diamonds()
    if ns.dot:
        cfg = TracerConfig(events=(CALL, RETURN), include=lambda c: c.co_filename == __file__)
        with FunctionTracer(cfg) as t:
            workload()
        g = build_call_graph(t.events)
        with open(ns.dot, "w", encoding="utf-8") as fh:
            fh.write(callgraph_dot(g, name="framecraft_example"))
        print(f"\nwrote {ns.dot}")
        print("render it with:  dot -Tsvg -O " + ns.dot)
    print()
    print("=" * 78)
    print("Next: 09 -- exceptions and unwinding: capturing state at failure")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
