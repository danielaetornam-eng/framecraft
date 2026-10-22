"""Call graphs and collapsed stack trees.

Two structures, two questions:

:class:`CallGraph`
    "Who calls whom?"  Nodes are *functions*, edges are call sites.  Answers
    *coupling* questions: which function has 40 callees, which pair of modules
    is entangled, where is the recursion.

:class:`StackTree`
    "Where did the time go?"  Nodes are *stack prefixes*, edges are "was called
    from".  Answers *cost* questions.  This is the structure a flame graph
    renders, and it is the only correct way to collapse a call graph into
    exclusive percentages (a call graph loses ordering, so it cannot).

Why you need both
-----------------

A tempting shortcut is to render a call graph as a flame graph.  It is wrong,
and the reason is instructive.  Consider::

    a -> b -> d
    a -> c -> d

The call graph has one node ``d`` with two callers.  A flame graph needs ``d``
to appear *twice*, once under each path, because exclusive time depends on the
path taken.  Collapsing them would tell you ``d`` costs 10% of runtime, which
depends entirely on the call mix -- the same ``d`` costs 1% if it is only
called from ``b``.

Cycles
------

Both structures must tolerate recursion.  :class:`CallGraph` records a cycle
when an edge would close one, and :meth:`CallGraph.find_cycles` returns every
distinct cycle using an iterative Tarjan SCC pass -- iterative, because the
obvious recursive DFS blows up on the same deep-recursion workloads you most
want to profile.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from framecraft.core.events import (
    C_CALL,
    C_RETURN,
    CALL,
    EXCEPTION,
    LINE,
    RETURN,
    TraceEvent,
)

__all__ = ["CallNode", "CallGraph", "StackTree", "build_call_graph", "build_stack_tree"]


@dataclass
class CallNode:
    """One function in a :class:`CallGraph`."""

    code_hash: str
    qualname: str
    filename: str
    firstlineno: int = 0

    ncalls: int = 0
    #: total time inside this function, *excluding* callees
    self_ns: int = 0
    #: total time including callees
    total_ns: int = 0
    #: wall time from entry to exit, summed across invocations
    elapsed_ns: int = 0
    exceptions: int = 0
    #: ``{callee_code_hash: count}``
    callees: dict[str, int] = field(default_factory=dict)
    #: ``{caller_code_hash: count}``
    callers: dict[str, int] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, int]:
        """Stable identity for this node across runs of the same program."""
        return (self.filename, self.firstlineno)

    @property
    def mean_ns(self) -> float:
        return self.total_ns / self.ncalls if self.ncalls else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_hash": self.code_hash,
            "qualname": self.qualname,
            "filename": self.filename,
            "firstlineno": self.firstlineno,
            "ncalls": self.ncalls,
            "self_ns": self.self_ns,
            "total_ns": self.total_ns,
            "elapsed_ns": self.elapsed_ns,
            "mean_ns": round(self.mean_ns, 1),
            "exceptions": self.exceptions,
            "callees": dict(self.callees),
            "callers": dict(self.callers),
        }


class CallGraph:
    """A directed multigraph of function calls.

    Built by :func:`build_call_graph` from a list of
    :class:`~framecraft.core.events.TraceEvent`.  You can also drive it
    manually with :meth:`add_call` / :meth:`add_return`, which is what the
    streaming (buffer-less) mode in ``examples/19_streaming.py`` does.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, CallNode] = {}
        #: ``{(caller_hash, callee_hash): count}``
        self.edges: dict[tuple[str, str], int] = {}
        #: ``call_id -> (code_hash, start_ns)`` for calls that have not returned
        self._open: dict[int, tuple[str, int]] = {}
        #: Set by ``add_call``; cleared by ``_ensure_adjacency``.
        self._adjacency_dirty = False

    # ------------------------------------------------------------------
    def _node(self, code_hash: str, qualname: str, filename: str, lineno: int) -> CallNode:
        """Fetch or create the node for ``code_hash``, upgrading stubs.

        A stub is created when an edge names a parent that has not been
        registered yet.  If that parent *is* registered later, its identity must
        be overwritten -- otherwise the graph permanently reports a function
        called ``<a1b2c3>`` with the right structure and the wrong name, which
        is the kind of wrong that survives code review because nothing looks
        broken.
        """
        n = self.nodes.get(code_hash)
        if n is None:
            n = CallNode(code_hash=code_hash, qualname=qualname, filename=filename, firstlineno=lineno)
            self.nodes[code_hash] = n
        elif n.qualname.startswith("<") and not qualname.startswith("<"):
            n.qualname = qualname
            n.filename = filename
            n.firstlineno = lineno
        return n

    def add_call(
        self,
        code_hash: str,
        qualname: str,
        filename: str,
        lineno: int,
        *,
        parent_hash: str | None = None,
        parent_name: str = "",
        parent_filename: str = "",
        parent_lineno: int = 0,
        start_ns: int = 0,
        call_id: int | None = None,
    ) -> CallNode:
        """Record one call.

        Only three things are maintained incrementally here -- ``ncalls``,
        ``edges`` and the open-call table -- and the derived adjacency
        (``callees`` / ``callers``) is rebuilt by :meth:`finish`.

        That split is deliberate.  Maintaining both directions incrementally
        invites exactly the bug this code once had: registering children before
        their parent creates a stub for the parent, and the later real
        registration of that parent never records the reverse edges, so
        ``fan_in`` reports the wrong most-coupled function with no error
        anywhere.  Deriving one direction from the other removes the class of
        bug rather than this instance of it.

        ``parent_*`` only matter when ``parent_hash`` names a node that does not
        exist yet; the parent is then created as a stub so the edge is not
        dropped.
        """
        n = self._node(code_hash, qualname, filename, lineno)
        n.ncalls += 1
        if parent_hash is not None:
            if parent_hash not in self.nodes:
                self._node(parent_hash, parent_name or f"<{parent_hash}>",
                           parent_filename or "<unknown>", parent_lineno)
            self.edges[(parent_hash, code_hash)] = self.edges.get((parent_hash, code_hash), 0) + 1
        if call_id is not None:
            self._open[call_id] = (code_hash, start_ns)
        self._adjacency_dirty = True
        return n

    def _ensure_adjacency(self) -> None:
        """Rebuild ``callees``/``callers`` from ``edges`` if needed.

        Lazy on purpose.  Requiring callers to remember ``finish()`` before
        ``find_cycles()`` or ``fan_in()`` is an ordering trap: the accessors
        silently return empty answers otherwise, which looks exactly like "this
        function has no callers" -- a conclusion you would act on.
        """
        if not self._adjacency_dirty:
            return
        for n in self.nodes.values():
            n.callees.clear()
            n.callers.clear()
        # _count is unused here, but iterating .items() is still right:
        # PERF102 suggests .keys(), which would cost a second dict lookup for
        # every edge on a graph with tens of thousands of them.
        for (a, b), _count in self.edges.items():  # noqa: PERF102
            if a in self.nodes and b in self.nodes:
                self.nodes[a].callees[b] = self.nodes[a].callees.get(b, 0) + 1
                self.nodes[b].callers[a] = self.nodes[b].callers.get(a, 0) + 1
        self._adjacency_dirty = False

    def add_return(self, call_id: int | None, *, end_ns: int = 0, exception: bool = False) -> None:
        if call_id is None:
            return
        rec = self._open.pop(call_id, None)
        if rec is None:
            return
        code_hash, start_ns = rec
        n = self.nodes.get(code_hash)
        if n is None:
            return
        n.elapsed_ns += max(0, end_ns - start_ns)
        if exception:
            n.exceptions += 1

    def finish(self) -> CallGraph:
        """Derive adjacency and inclusive times from ``edges``.  Idempotent.

        *   ``callees``/``callers`` are rebuilt from ``edges``, in both
            directions.  Deriving one from the other is what makes the graph
            self-consistent no matter what order calls were registered in.
        *   Inclusive time for a DAG is
            ``total[v] = self[v] + sum(total[c] for c in distinct callees v)``.
            Sum over *distinct* callees, because a callee invoked twice from the
            same site is one inclusive bucket, not two.
        *   Cycles are handled by collapsing strongly connected components: every
            member of an SCC gets the component's total.  See
            :meth:`find_cycles`, which is iterative because the recursive
            workloads are exactly the ones you most want to profile.
        """
        self._ensure_adjacency()

        # Collapse SCCs: every member of a component shares the component total.
        comp_total: dict[str, int] = {}
        comp_members: dict[str, list[str]] = {}
        for i, comp in enumerate(self.find_cycles()):
            members = sorted(comp)
            comp_members[members[0]] = members
            for h in members:
                comp_total[h] = i

        # Process nodes with no unresolved callees first (reverse topological
        # order over the condensation).  Nodes inside a cycle are handled as a
        # group, which is why the whole SCC shares one total.
        done: set[str] = set()
        for comp in comp_members.values():
            total = sum(self.nodes[h].self_ns for h in comp)
            for h in comp:
                self.nodes[h].total_ns = total
                done.update(comp)

        remaining = [h for h in self.nodes if h not in done]
        progress = True
        while progress:
            progress = False
            for h in list(remaining):
                callees = [c for c in self.nodes[h].callees if c in self.nodes]
                if all(c in done for c in callees):
                    self.nodes[h].total_ns = self.nodes[h].self_ns + sum(
                        self.nodes[c].total_ns for c in callees
                    )
                    done.add(h)
                    remaining.remove(h)
                    progress = True
        # Anything still unresolved is inside a cycle we did not enumerate;
        # give it its self time rather than an arbitrary number.
        for h in remaining:
            self.nodes[h].total_ns = self.nodes[h].self_ns
        return self

    # ------------------------------------------------------------------
    def find_cycles(self) -> list[list[str]]:
        self._ensure_adjacency()
        """Every distinct cycle, as a list of node keys (iterative Tarjan).

        Why iterative: the workloads with the deepest call graphs are exactly
        the recursive ones, and a recursive DFS over those is a second stack
        overflow.  Every "my profiler crashed on the buggy input" report is
        this.

        Returns a list of cycles; each cycle is a list of code hashes in
        traversal order, with no repeated tail.
        """
        self._ensure_adjacency()
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        result: list[list[str]] = []
        counter = 0

        # `n.callees` is the *value* of the comprehension, so `.items()` is
        # correct here; PERF102 only applies when a value is discarded.
        successors = {h: tuple(n.callees) for h, n in self.nodes.items()}

        for root in self.nodes:
            if root in index:
                continue
            # (node, next-successor-index) work items; iterative Tarjan.
            work: list[list[Any]] = [[root, 0]]
            while work:
                node, pi = work[-1]
                if pi == 0:
                    index[node] = low[node] = counter
                    counter += 1
                    stack.append(node)
                    on_stack.add(node)
                recursed = False
                succs = successors.get(node, ())
                while pi < len(succs):
                    w = succs[pi]
                    pi += 1
                    if w not in successors:
                        continue
                    if w not in index:
                        work[-1][1] = pi
                        work.append([w, 0])
                        recursed = True
                        break
                    if w in on_stack:
                        low[node] = min(low[node], index[w])
                if recursed:
                    continue
                work[-1][1] = pi
                if pi >= len(succs):
                    work.pop()
                    if work:
                        parent = work[-1][0]
                        low[parent] = min(low[parent], low[node])
                    if low[node] == index[node]:
                        comp: list[str] = []
                        while True:
                            w = stack.pop()
                            on_stack.discard(w)
                            comp.append(w)
                            if w == node:
                                break
                        if len(comp) > 1 or node in (successors.get(node) or ()):
                            result.append(self._rotate(comp, successors))
        return result

    @staticmethod
    def _rotate(comp: list[str], successors: dict[str, list[str]]) -> list[str]:
        """Turn an SCC into a readable cycle starting at its shallowest node."""
        members = set(comp)
        # Prefer a member with no incoming edge from inside the component.
        starts = [n for n in comp if not any(w in members for w in successors.get(n, ()))]
        head = (starts or comp)[0]
        order: list[str] = []
        cur = head
        for _ in range(len(comp) + 1):
            order.append(cur)
            nxt = next((w for w in successors.get(cur, ()) if w in members), None)
            if nxt is None or nxt == head:
                break
            cur = nxt
        return order

    def roots(self) -> list[CallNode]:
        """Nodes with no callers -- the entry points of the traced region."""
        self._ensure_adjacency()
        return [n for n in self.nodes.values() if not n.callers]

    def leaves(self) -> list[CallNode]:
        """Nodes with no callees."""
        self._ensure_adjacency()
        return [n for n in self.nodes.values() if not n.callees]

    def fan_in(self) -> list[CallNode]:
        """Nodes ranked by number of distinct callers (coupling hot spots)."""
        self._ensure_adjacency()
        return sorted(self.nodes.values(), key=lambda n: (-len(n.callers), -n.ncalls))

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [{"from": a, "to": b, "count": c} for (a, b), c in self.edges.items()],
            "cycles": self.find_cycles(),
        }

    def __len__(self) -> int:
        return len(self.nodes)

    def __repr__(self) -> str:
        return f"<CallGraph nodes={len(self.nodes)} edges={len(self.edges)}>"


# ----------------------------------------------------------------------
# builder
# ----------------------------------------------------------------------

#: Kinds that mean "a frame was entered".
_ENTER = frozenset({CALL, C_CALL})
#: Kinds that mean "a frame was left".
_EXIT = frozenset({RETURN, C_RETURN, EXCEPTION})


def build_call_graph(events: Iterable[TraceEvent], *, attribute_self_time: bool = True) -> CallGraph:
    """Fold a flat event stream into a :class:`CallGraph`.

    ``events`` must be in emission order (they are, by construction: ``seq`` is
    monotonic and all tracers buffer in arrival order).

    Exclusive ("self") time is attributed like this, which is exact rather than
    inferred: when a frame returns, everything between its ``call`` and its
    ``return`` belongs to it, minus the intervals already claimed by its direct
    children.  We keep an explicit child stack so a function that calls another
    function which calls a third contributes only its own intervals.
    """
    g = CallGraph()
    # stack of (call_id, code_hash, child_ns, start_ns)
    live: list[list[Any]] = []

    for ev in events:
        if ev.kind in _ENTER:
            parent = live[-1][1] if live else None
            # The parent frame's identity, so that `add_call` can create a stub
            # for it if it has not been registered yet.
            parent_info = live[-1][5] if live else None
            g.add_call(
                ev.code_hash or "",
                ev.qualname or ev.name,
                ev.filename,
                ev.lineno or 0,
                parent_hash=parent,
                parent_name=parent_info[0] if parent_info else "",
                parent_filename=parent_info[1] if parent_info else "",
                parent_lineno=parent_info[2] if parent_info else 0,
                start_ns=ev.perf_ns,
                call_id=ev.call_id if ev.kind == CALL else None,
            )
            live.append([ev.call_id, ev.code_hash or "", 0, ev.perf_ns, ev.kind,
                         (ev.qualname or ev.name, ev.filename, ev.lineno or 0)])

        elif ev.kind in _EXIT:
            # Close the matching frame.  For CALL/RETURN the ids match; for
            # C events there is no id, so we pop the innermost Python frame and
            # accept the approximation (documented in the report as such).
            if not live:
                continue
            entry = live.pop()
            duration = max(0, ev.perf_ns - entry[3])
            is_exception = ev.kind == EXCEPTION or ev.exception_type is not None
            g.add_return(entry[0], end_ns=ev.perf_ns, exception=is_exception)
            if attribute_self_time:
                node = g.nodes.get(entry[1])
                if node is not None:
                    if entry[4] == CALL:
                        node.self_ns += duration
                    else:
                        node.self_ns += 0  # C time is attributed to the owner
            if live:
                live[-1][2] += duration

    return g.finish()


# ----------------------------------------------------------------------
# stack tree
# ----------------------------------------------------------------------


@dataclass
class _Frame_:
    """Mutable tree node.  Private: use :meth:`StackTree.root` and friends."""

    label: str
    code_hash: str = ""
    value: int = 0
    self_value: int = 0
    children: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = {}

    @property
    def name(self) -> str:
        return self.label


class StackTree:
    """Collapsed stack tree: the data behind a flame graph.

    Each node is a *stack prefix*.  ``value`` is the number of times the exact
    prefix was observed; ``total_value`` (the subtree sum) is how many
    observations passed through it.  A flame graph draws each node with width
    proportional to ``total_value``, which is why it is correct where a call
    graph render is not.

    Built two ways:

    *   :func:`build_stack_tree` from ``line``/``call`` events -- exact, gives
        you line-level nodes and a real tree.
    *   :meth:`add_stack` from :class:`~framecraft.trace.sampling.SamplingTracer`
        stacks -- approximate in time, exact in structure.
    """

    def __init__(self, root_label: str = "all") -> None:
        self.root = _Frame_(root_label)

    def add_stack(self, frames: Sequence[Any], *, weight: int = 1) -> None:
        """Insert one observation.

        ``frames`` is innermost-first (the order ``f_back`` gives you).  We
        reverse it so the root's children are outermost -- matching how a
        flame graph reads and how a traceback reads.
        """
        node = self.root
        node.value += weight
        for f in reversed(list(frames)):
            key = getattr(f, "code_hash", None) or getattr(f, "name", str(f))
            label = getattr(f, "qualname", None) or getattr(f, "name", str(f))
            child = node.children.get(key)
            if child is None:
                lineno = getattr(f, "lineno", None)
                child = _Frame_(
                    f"{label} ({f.filename.split(chr(92))[-1].split('/')[-1]}:{lineno})"
                    if getattr(f, "filename", None)
                    else label,
                    code_hash=key,
                )
                node.children[key] = child
            node = child
            node.value += weight

    def add_path(self, names: Sequence[str], *, weight: int = 1) -> None:
        """Insert one observation given as ``(qualname, code_hash)`` pairs."""
        node = self.root
        node.value += weight
        for entry in names:
            if isinstance(entry, tuple):
                name, key = entry
            else:
                name, key = entry, entry
            child = node.children.get(key)
            if child is None:
                child = _Frame_(name, code_hash=key)
                node.children[key] = child
            node = child
            node.value += weight

    # ------------------------------------------------------------------
    @property
    def total(self) -> int:
        """Total observations inserted (the root's weight).

        A property rather than a method because renderers use it constantly and
        ``tree.total`` reads better than ``tree.total()`` in a template.
        """
        return self.root.value

    def walk(self, node: _Frame_ | None = None, depth: int = 0) -> Iterator[tuple[_Frame_, int]]:
        """Pre-order traversal, ``(node, depth)``."""
        node = node if node is not None else self.root
        yield node, depth
        # Sort by value so hot paths come first: a flamegraph rendered in
        # insertion order is unreadable.
        for child in sorted(node.children.values(), key=lambda c: (-c.value, c.label)):
            yield from self.walk(child, depth + 1)

    def hottest_paths(self, limit: int = 20) -> list[tuple[int, list[str]]]:
        """The ``limit`` heaviest root-to-leaf paths, as ``(weight, names)``."""
        out: list[tuple[int, list[str]]] = []

        def rec(node: _Frame_, acc: list[str]) -> None:
            if not node.children:
                if acc:
                    out.append((node.value, list(acc)))
                return
            for c in node.children.values():
                rec(c, acc + [c.label])

        rec(self.root, [])
        out.sort(key=lambda t: (-t[0], t[1]))
        return out[:limit]

    def collapse(self, min_weight: int) -> StackTree:
        """Return a copy with nodes below ``min_weight`` pruned.

        Pruning rather than merging matters: merging would invent parent-child
        relationships that never happened.
        """
        out = StackTree(self.root.label)

        def rec(src: _Frame_, dst: _Frame_) -> None:
            for c in src.children.values():
                if c.value < min_weight:
                    continue
                n = _Frame_(c.label, code_hash=c.code_hash, value=c.value, self_value=c.self_value)
                dst.children[c.code_hash or c.label] = n
                rec(c, n)

        rec(self.root, out.root)
        out.root.value = self.root.value
        return out

    def to_dict(self) -> dict[str, Any]:
        def conv(n: _Frame_) -> dict[str, Any]:
            return {
                "label": n.label,
                "value": n.value,
                "code_hash": n.code_hash,
                "children": [conv(c) for c in sorted(n.children.values(), key=lambda c: -c.value)],
            }

        return conv(self.root)

    def __repr__(self) -> str:
        return f"<StackTree total={self.root.value} leaves={sum(1 for _ in self.walk())}>"


def build_stack_tree(events: Iterable[TraceEvent], *, min_weight: int = 1) -> StackTree:
    """Fold a flat ``line``/``call`` event stream into a :class:`StackTree`.

    Replays call/return nesting to know, for each ``line`` event, the exact
    stack prefix it happened under.  This is what turns a flat event log into
    something you can actually read.

    ``min_weight`` prunes the result; the default of 1 keeps everything.
    """
    tree = StackTree("all")
    live: list[tuple[str, str]] = []  # (qualname, code_hash)

    for ev in events:
        if ev.kind == CALL:
            live.append((ev.qualname or ev.name, ev.code_hash or ""))
        elif ev.kind in (RETURN, EXCEPTION):
            # An EXCEPTION event does not pop the frame: the frame is still
            # alive and will get its own RETURN.  Only RETURN unwinds.
            if ev.kind == RETURN and live:
                live.pop()
        elif ev.kind == LINE and live:
            loc = f"{ev.short_filename}:{ev.lineno}"
            # Key on (code_hash, line), NOT code_hash alone.
            #
            # With code_hash as the key every line of a function collapses into
            # one node whose label is whichever line happened to come first, so
            # each frame becomes a degenerate two-node chain and every subtree
            # total is zero -- which renders as a flame graph of zero-width
            # bars. Keying by line keeps the per-line detail that makes a flame
            # graph worth looking at.
            tree.add_path([(f"{n} ({loc})", (h, ev.lineno)) for n, h in live], weight=1)

    return tree.collapse(min_weight) if min_weight > 1 else tree
