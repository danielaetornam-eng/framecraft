# 07 — Graphs and reports

Four structures, four questions. Confusing them is the reason profiles are
misread.

| structure | question it answers |
|---|---|
| `CallGraph` | who calls whom? (coupling, recursion) |
| `StackTree` | where did the time go? (cost) |
| `Timeline` | what was each thread doing, when? (concurrency) |
| `TraceEvent` stream | what exactly happened? (the record) |

## CallGraph — structure

Nodes are functions, edges are call sites.

```python
g = build_call_graph(events)
g.nodes          # code_hash -> CallNode
g.edges          # (caller_hash, callee_hash) -> count
g.roots()        # no callers: the entry points
g.leaves()       # no callees
g.fan_in()       # ranked by number of distinct callers
g.find_cycles()  # strongly connected components
```

### Why the derived fields are rebuilt, not maintained

`callees` and `callers` are **derived from `edges`**, lazily, on first access:

```python
def _ensure_adjacency(self):
    if not self._adjacency_dirty:
        return
    for n in self.nodes.values():
        n.callees.clear(); n.callers.clear()
    for (a, b), _count in self.edges.items():
        self.nodes[a].callees[b] = ... ; self.nodes[b].callers[a] = ...
    self._adjacency_dirty = False
```

This exists because an earlier version maintained both directions
incrementally, and registering children before their parent produced a graph
where `fan_in()` named the wrong function with no error anywhere. Deriving one
direction from the other removes the *class* of bug rather than the instance.

For the same reason, the accessors call `_ensure_adjacency()` themselves. Making
callers remember `finish()` before `find_cycles()` is an ordering trap whose
failure mode is "this function has no callers" — a conclusion you would act on.

### Cycles, iteratively

```python
def find_cycles(self) -> list[list[str]]:
```

An **iterative** Tarjan SCC pass. The obvious recursive DFS overflows on
precisely the deep-recursion inputs you most want to profile, and "my profiler
crashed on the buggy input" is always this. Pinned by
`test_find_cycles_survives_deep_recursion`, which builds a 2000-node chain.

### Inclusive time

```
total[v] = self[v] + Σ total[c] for distinct callees c
```

Sum over *distinct* callees: a callee invoked twice from the same site is one
inclusive bucket, not two. Cycles are handled by collapsing each SCC, so every
member of a component shares the component's total.

`fan_in` counts **callers**, so a root has zero and sorts *last*. That is
backwards from intuition and worth stating.

## StackTree — cost

Nodes are **stack prefixes** with a weight (how many times that exact prefix was
observed). This is the structure a flame graph renders.

```python
tree = build_stack_tree(events, min_weight=2)
tree.total, tree.walk(), tree.hottest_paths(10), tree.collapse(min)
```

### Why a call graph cannot be rendered as a flame graph

```python
a -> b -> d
a -> c -> d
```

The call graph has **one** node `d` with two callers. A flame graph needs `d` to
appear **twice**, once under each path, because exclusive time depends on the path
taken. Collapsing them gives you one bar whose width is the sum — reading as
"`d` is 30% of the program", a number that is entirely a function of the call
mix, and could be 1% if `d` were only ever called from `b`.

Demonstrated in `examples/08` section 4 with real numbers.

### Keying by `(code_hash, line)`

```python
tree.add_path([(f"{name} ({file}:{line})", (code_hash, lineno)) for name, code_hash in live])
```

Keying on `code_hash` alone collapses every line of a function into one node, so
each frame becomes a degenerate two-node chain and **every subtree total is
zero** — which renders as a flame graph of invisible bars. Keying by line keeps
the per-line detail that makes a flame graph worth reading.

## FlameGraph layout

A flame graph is a tidy tree drawn sideways. The algorithm, in full:

```
width(node) = node.value * px_per_unit
x(children) = x(parent), advanced by each child's width, in order of weight
y(node)     = -depth * row_height        # depth 0 at the BOTTOM
```

Three details that each fixed a real bug:

**`node.value` is already the cumulative count.** `add_path` increments `value`
on *every* node along the path, not only the last. Recomputing the subtree from
the children is not redundant, it is **wrong for leaves** (whose child sum is
zero) and it makes every sibling land on the same x.

**The function returns its own width, not how far the cursor advanced.** Those
are equal for a node with children and differ by exactly the node's width for a
leaf — so returning the advance makes every leaf measure zero and every sibling
stack into one column.

**Prune before appending.** Emitting a rectangle and then declining to descend
leaves a sub-pixel sliver: invisible, unclickable, and still in the data, which
is worse than absent.

Sorting children by weight is not cosmetic. Unsorted layout interleaves every
colour band and the graph becomes unreadable; sorting is most of what makes a
flame graph legible.

`layout()` is a pure function from a tree to a list of rects, so the tests assert
exact geometry:

```python
def test_siblings_tile_their_parent(): ...
def test_siblings_do_not_overlap(): ...
def test_children_are_inside_the_parent(): ...
def test_deeper_is_lower_in_the_svg(): ...     # depth 0 at the bottom
def test_heavier_first(): ...
```

## Timeline — concurrency

A flame graph aggregates, so it cannot answer "what was thread B doing while
thread A was blocked on that lock?" For that you want time on the x-axis and
threads as rows.

```python
tl = build_timeline(events)
tl.threads()          # [tid, ...]
tl.overlaps()         # (start, end, tid_a, tid_b) sweep-line, O(n log n)
span.uninstrumented_ns()
```

### The honest limit

**A tracer cannot see blocking.** It fires on bytecode. A thread in
`lock.acquire()` or `socket.recv()` executes no bytecode, so it produces no
events. The gap *is* the interesting part.

So `Span.uninstrumented_ns()` names it rather than silently attributing it to
whatever frame happens to be innermost:

```
thread     depth   start ms    dur ms   uninstr ms  function
w0             1       3.19     1.717        1.717  worker
MainThread     0       0.00    12.028        1.369  threaded_workload
```

`worker`'s uninstrumented time is almost entirely its `time.sleep(0.001)` plus
the work done under the GIL with no bytecode of *its* running. A profiler that
spread that time across the enclosing frames would be lying.

## The call graph as DOT

```python
callgraph_dot(graph, name="g", min_calls=2, max_nodes=200, show_cycles=True)
render_dot(src, "out/callgraph")      # shells out to graphviz, or returns None
```

Everything about it is about **suppressing noise**, which is policy:

- self-loops become dashed red, not circular arcs that turn the image into a
  hairball;
- cycle members are outlined and, if they raise, drawn as octagons;
- edge width is call count;
- `min_calls` prunes the long tail;
- `max_nodes` keeps the most-called, because Graphviz layout is ~O(n²) in
  practice past a few hundred nodes.

Two escaping details worth knowing, both of which produced visibly broken output:

**Graph identifiers cannot contain `:` or `\`.** A Windows path used verbatim
gives `digraph C:\Users\me\graph.dot {`, which Graphviz rejects — and the error
points at the *filename*, which is a genuinely confusing thing to debug.
`_dot_ident()` sanitises.

**HTML escaping is wrong for DOT.** `html.escape` turns `<string>` into
`&lt;string&gt;`, which Graphviz renders *literally*. DOT has its own escapes
(`\\`, `\"`, `\n`); angle brackets are legal as-is.

`render_dot` returns `None` when Graphviz is absent. A missing optional tool
must degrade the report, not break the run.

## Streaming flame graphs

`SVGFlameSink` maintains the live stack prefix incrementally, so it needs no
event buffer at all. That is what makes it usable for a program producing a
hundred million line events: the only thing that grows is the tree, and the tree
is bounded by the number of *distinct stack prefixes*.

```python
t = FunctionTracer(TracerConfig(events=("call", "line", "return"), buffer=False))
t.add_sink(SVGFlameSink("flame.svg"))
with t:
    work()
# flame.svg is written on close()
```

Add that to a long-running service and you get a live flame graph of production
for the cost of one integer increment per line.

## The HTML report

One file, no external assets, no framework, opens offline. That constraint is
deliberate: trace reports end up attached to bug reports and opened on machines
with no network.

Sections: summary cards · inline flame graph · hot lines · exclusive time ·
busiest functions · exceptions · a filterable event log.

```python
HTMLReport(events, dropped=tracer.dropped, errors=tracer.error_count,
           include_locals=False, title="my report").write("report.html")
```

### What it deliberately leaves out

**Captured locals, by default.** A variable-capturing tracer is a
secret-exfiltration tool, and this report is about to be attached to an issue.
`include_locals=True` when you know the report is private.

**A CDN and a JS framework.** The log filter is ten lines of vanilla
`<input oninput=...>`. A report that needs the network is a report nobody opens.

### What it always shows

```
!! 500 events were dropped (99.983% coverage) -- percentages below are lower bounds
```

A trace that lost events and did not say so looks exactly as authoritative as one
that did not.

### What it hardens

- the SVG `xmlns` is a namespace URI, not a fetch; nothing is loaded (pinned by
  a test that greps for `src=`/`href=` pointing off-document);
- every interpolated value is `html.escape`d, including code hashes and
  exception text, so a malicious `__repr__` cannot inject script (pinned);
- the row cap is respected, so a million-event trace does not produce a 400 MB
  file.

## Which unit?

The flame graph's x-axis means different things depending on the source, and the
graph says so in its own subtitle:

| built from | width means |
|---|---|
| `line` events | share of line events under this prefix — **instruction count, not time** |
| sample stacks | share of timer ticks — **honest wall clock** |

A line-event flame graph is a control-flow picture, not a time picture. If you
want time, use the sampler (`examples/07`), and if you want to compare against a
replayed trace (`examples/15`) you are looking at instruction counts in both, so
the comparison is at least consistent.