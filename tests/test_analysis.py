"""Graph structures, sinks and analysis.

These are pure-data tests: no hooks, no timing, exact assertions.  That is
deliberate.  The interesting failures in a flame graph are *arithmetic* bugs --
an off-by-one width, a sibling placed at the wrong x -- and you cannot catch
those with a screenshot.
"""

from __future__ import annotations

import io
import itertools
import json
import sqlite3

import pytest

from framecraft import CALL, LINE, RETURN, FunctionTracer, TracerConfig
from framecraft.analysis import (
    confidence_note,
    diff_traces,
    exception_summary,
    exclusive_times,
    hot_lines,
    render_diff,
    render_table,
    summarise_events,
)
from framecraft.graph import CallGraph, build_call_graph, build_stack_tree, build_timeline
from framecraft.graph.callgraph import StackTree
from framecraft.sinks import (
    CountingSink,
    JSONLSink,
    SQLiteSink,
    TextSink,
    callgraph_dot,
    flame_svg,
    layout,
)
from framecraft.sinks.base import NullSink, TeeSink, open_sink

# ----------------------------------------------------------------------
# Workload
# ----------------------------------------------------------------------


def leaf(n):
    return n * 2


def middle(n):
    return leaf(n) + leaf(n + 1)


def top():
    return middle(2)


def recursive(n):
    return 1 if n <= 1 else recursive(n - 1)


def diamond(x):
    return check_a(x) and check_b(x)


def check_a(x):
    return normalise(x) is not None


def check_b(x):
    return len(normalise(x)) <= 10


def normalise(x):
    return [v for v in x if isinstance(v, int)]


def boom():
    raise ValueError("nope")


# ----------------------------------------------------------------------


@pytest.fixture
def trace():
    with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN))) as t:
        for _ in range(3):
            top()
    return t.events


class TestCallGraph:
    def test_nodes_and_edges(self, trace):
        g = build_call_graph(trace)
        assert "leaf" in {n.qualname for n in g.nodes.values()}
        assert g.edges, "two call kinds in the workload must produce edges"

    def test_call_counts(self, trace):
        g = build_call_graph(trace)
        leaf_node = next(n for n in g.nodes.values() if n.qualname == "leaf")
        assert leaf_node.ncalls == 6  # twice per middle, three tops

    def test_self_time_is_non_negative(self, trace):
        g = build_call_graph(trace)
        for n in g.nodes.values():
            assert n.self_ns >= 0
            assert n.total_ns >= n.self_ns

    def test_recursion_is_a_cycle(self):
        with FunctionTracer(TracerConfig(events=(CALL, RETURN),
                                        include=lambda c: c.co_name == "recursive")) as t:
            recursive(5)
        g = build_call_graph(t.events)
        cycles = g.find_cycles()
        assert cycles, "self-recursion should be detected as a cycle"
        node = next(n for n in g.nodes.values() if n.qualname == "recursive")
        assert node.code_hash in cycles[0], "cycles are reported as code hashes"

    def test_find_cycles_survives_deep_recursion(self):
        """Iterative SCC is not an optimisation; it is a correctness requirement."""
        g = CallGraph()
        h = "0"
        g.add_call(h, "f", "x.py", 1, start_ns=0, call_id=1)
        prev = None
        for i in range(2000):
            h = str(i + 1)
            g.add_call(h, f"f{i}", "x.py", i + 1, parent_hash=prev, call_id=i + 2)
            prev = h
        cycles = g.find_cycles()  # a recursive DFS would blow up here
        assert isinstance(cycles, list)

    def test_no_cycles_in_a_dag(self):
        g = CallGraph()
        g.add_call("a", "a", "x.py", 1)
        g.add_call("b", "b", "x.py", 2, parent_hash="a")
        g.add_call("c", "c", "x.py", 3, parent_hash="b")
        assert g.find_cycles() == []

    def test_roots_and_leaves(self):
        g = CallGraph()
        g.add_call("a", "a", "x.py", 1)
        g.add_call("b", "b", "x.py", 2, parent_hash="a")
        assert [n.qualname for n in g.roots()] == ["a"]
        assert [n.qualname for n in g.leaves()] == ["b"]

    def test_children_may_be_registered_before_their_parent(self):
        """Edges must not be silently dropped for an unregistered parent."""
        g = CallGraph()
        for i in range(3):
            g.add_call(f"c{i}", f"c{i}", "x.py", i, parent_hash="a")
        g.add_call("a", "a", "x.py", 0)
        assert len(g.edges) == 3

    def test_fan_in_ranks_by_number_of_callers(self):
        """A shared callee has high fan-in; a root has none.

        Easy to get backwards: `fan_in` counts *callers*, so the function at the
        top of a call tree is at the *bottom* of this ranking.
        """
        g = CallGraph()
        g.add_call("root", "root", "x.py", 1)
        for i in range(3):
            g.add_call(f"p{i}", f"p{i}", "x.py", i, parent_hash="root")
            g.add_call("shared", "shared", "x.py", 100, parent_hash=f"p{i}")
        g.finish()
        ranked = [n.qualname for n in g.fan_in()]
        assert ranked[0] == "shared", ranked
        assert ranked[-1] == "root", ranked

    def test_stub_is_upgraded_when_the_parent_is_registered_later(self):
        g = CallGraph()
        g.add_call("child", "child", "x.py", 1, parent_hash="a")
        g.add_call("a", "real_name", "x.py", 0)
        node = g.nodes["a"]
        assert node.qualname == "real_name", (
            "a stub must be replaced by the real identity, not left as <a>"
        )

    def test_serialisable(self, trace):
        d = build_call_graph(trace).to_dict()
        json.dumps(d)


class TestStackTree:
    def test_line_events_become_distinct_nodes(self, trace):
        """Keying by (code_hash, line) is what keeps subtrees non-zero."""
        tree = build_stack_tree(trace)
        labels = [n.label for n, _d in tree.walk()]
        assert len(labels) == len(set(labels)), "labels must be unique per node"
        assert any(":" in label for label in labels)

    def test_total_matches_observations(self, trace):
        tree = build_stack_tree(trace)
        assert tree.total == sum(1 for e in trace if e.kind == LINE)

    def test_collapse_prunes(self, trace):
        tree = build_stack_tree(trace)
        pruned = tree.collapse(tree.total // 2)
        assert pruned.total == tree.total
        assert sum(1 for _n, _d in pruned.walk()) < sum(1 for _n, _d in tree.walk())

    def test_add_stack_orders_outermost_first(self):
        tree = StackTree("all")
        tree.add_stack(["inner", "outer"])
        names = [n.label for n, _d in tree.walk()]
        assert names == ["all", "outer", "inner"]

    def test_hottest_paths_are_sorted(self):
        tree = StackTree("all")
        tree.add_path([("a", "a")], weight=1)
        tree.add_path([("b", "b")], weight=10)
        paths = tree.hottest_paths(5)
        assert paths[0][1][-1] == "b"


class TestFlameLayout:
    """The layout is a pure function, so assert exact geometry."""

    def build(self):
        tree = StackTree("all")
        tree.add_path([("a", "a"), ("b", "b"), ("c", "c")], weight=6)
        tree.add_path([("a", "a"), ("b", "b"), ("d", "d")], weight=2)
        tree.add_path([("a", "a"), ("e", "e")], weight=2)
        return tree

    def test_siblings_tile_their_parent(self):
        rects, _ppu = layout(self.build(), width=1000)
        by_label = {r["label"]: r for r in rects}
        b_children = [r for r in rects if r["depth"] == 3]
        assert sum(r["w"] for r in b_children) == pytest.approx(by_label["b"]["w"])

    def test_siblings_do_not_overlap(self):
        rects, _ppu = layout(self.build(), width=1000)
        by_depth: dict[int, list] = {}
        for r in rects:
            by_depth.setdefault(r["depth"], []).append(r)
        for depth, group in by_depth.items():
            group.sort(key=lambda r: r["x"])
            for a, b in itertools.pairwise(group):
                assert a["x"] + a["w"] <= b["x"] + 0.01, (
                    f"depth {depth}: {a['label']} overlaps {b['label']}"
                )

    def test_children_are_inside_the_parent(self):
        rects, _ppu = layout(self.build(), width=1000)
        a = next(r for r in rects if r["label"] == "a")
        for r in rects:
            if r["depth"] > a["depth"]:
                assert r["x"] >= a["x"] - 0.01
                assert r["x"] + r["w"] <= a["x"] + a["w"] + 0.01

    def test_percentages_sum_to_100(self):
        rects, _ppu = layout(self.build(), width=1000)
        roots = [r for r in rects if r["depth"] == 1]
        assert sum(r["pct"] for r in roots) == pytest.approx(100.0)

    def test_deeper_is_lower_in_the_svg(self):
        """Depth 0 at the bottom is what makes a flame graph read like a stack."""
        rects, _ppu = layout(self.build(), width=1000)
        a = next(r for r in rects if r["label"] == "a")
        c = next(r for r in rects if r["label"] == "c")
        assert c["y"] < a["y"]

    def test_heavier_first(self):
        rects, _ppu = layout(self.build(), width=1000)
        depth2 = sorted((r for r in rects if r["depth"] == 2), key=lambda r: r["x"])
        assert depth2[0]["label"] == "b", "the heavier child must come first"

    def test_min_width_prunes_children(self):
        tree = StackTree("all")
        tree.add_path([("big", "big")], weight=1_000_000)
        tree.add_path([("big", "big"), ("tiny", "tiny")], weight=1)
        rects, _ppu = layout(tree, width=100, min_width_px=1.0)
        assert not [r for r in rects if r["label"] == "tiny"]

    def test_empty_tree(self):
        rects, ppu = layout(StackTree("all"))
        assert rects == [] and ppu == 0.0


class TestSVG:
    def test_is_well_formed_enough(self):
        tree = StackTree("all")
        tree.add_path([("a", "a")], weight=1)
        svg = flame_svg(tree, title="t & t", unit="events")
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")
        assert 'xmlns="http://www.w3.org/2000/svg"' in svg
        assert "t &amp; t" in svg, "the title must be XML-escaped"

    def test_labels_are_escaped(self):
        tree = StackTree("all")
        tree.add_path([("<script>alert(1)</script>", "x")], weight=1)
        svg = flame_svg(tree, width=2000)
        assert "<script>alert(1)</script>" not in svg
        assert "&lt;script&gt;" in svg

    def test_empty_tree_still_produces_svg(self):
        assert flame_svg(StackTree("all")).startswith("<svg")


class TestDot:
    def test_valid_identifier(self):
        g = CallGraph()
        g.add_call("a", "a", "x.py", 1)
        src = callgraph_dot(g, name=r"C:\Users\me\graph.dot")
        ident = src.split()[1]
        assert ident.replace("_", "").replace("CUsersRob...", "").isalnum() or "_" in ident
        assert "." not in ident and ":" not in ident and "\\" not in ident

    def test_labels_are_not_html_escaped(self):
        g = CallGraph()
        g.add_call("a", "f", "<string>", 1)
        src = callgraph_dot(g, name="g")
        assert "&lt;" not in src
        assert "<string>" in src

    def test_self_loop_is_marked(self):
        g = CallGraph()
        g.add_call("a", "rec", "x.py", 1)
        g.add_call("a", "rec", "x.py", 1, parent_hash="a", call_id=2)
        g.finish()
        src = callgraph_dot(g, name="g")
        assert 'style=dashed' in src

    def test_min_calls_prunes(self):
        g = CallGraph()
        g.add_call("a", "a", "x.py", 1, call_id=1)
        g.add_call("b", "b", "x.py", 2, parent_hash="a", call_id=2)
        g.add_call("b", "b", "x.py", 2, parent_hash="a", call_id=3)   # seen twice
        g.add_call("c", "c", "x.py", 3, parent_hash="a", call_id=4)   # seen once
        g.finish()
        assert g.edges[("a", "b")] == 2
        assert g.edges[("a", "c")] == 1
        assert callgraph_dot(g, name="g", min_calls=1).count("->") == 2
        assert callgraph_dot(g, name="g", min_calls=2).count("->") == 1

    def test_empty_graph_is_still_valid(self):
        src = callgraph_dot(CallGraph(), name="g")
        assert src.startswith("digraph g {")
        assert src.rstrip().endswith("}")


class TestTimeline:
    def test_spans_are_matched(self, trace):
        tl = build_timeline(trace)
        assert len(tl) > 0
        assert all(s.closed for s in tl.spans), "every span must have been closed"
        assert all(s.duration_ns > 0 for s in tl.spans)

    def test_children_tile_the_parent(self, trace):
        tl = build_timeline(trace)
        for s in tl.spans:
            assert s.child_ns() <= s.duration_ns

    def test_uninstrumented_is_non_negative(self, trace):
        tl = build_timeline(trace)
        for s in tl.spans:
            assert s.uninstrumented_ns() >= 0

    def test_overlaps_of_a_single_thread_is_empty(self, trace):
        assert build_timeline(trace).overlaps() == []

    def test_overlaps_detects_concurrency(self):
        import threading

        from framecraft import TracerConfig as TC

        with FunctionTracer(TC(events=(CALL, RETURN), include_threads=True)) as t:
            def work():
                import time as _t

                _t.sleep(0.02)
                return 1

            threads = [threading.Thread(target=work) for _ in range(3)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        tl = build_timeline(t.events)
        assert tl.overlaps(), "three concurrent sleeps must produce overlap windows"

    def test_empty_timeline_has_no_overlaps(self):
        assert build_timeline([]).overlaps() == []


class TestSinks:
    def test_jsonl_round_trip(self, trace, tmp_path):
        path = tmp_path / "t.jsonl"
        sink = JSONLSink(path=str(path))
        for ev in trace:
            sink.write(ev)
        sink.close()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == len(trace)
        for line in lines:
            json.loads(line)  # one JSON object per line

    def test_jsonl_round_trips_through_the_loader(self, trace, tmp_path):
        from framecraft.analysis import load_jsonl

        path = tmp_path / "t.jsonl"
        sink = JSONLSink(path=str(path))
        for ev in trace:
            sink.write(ev)
        sink.close()
        back = load_jsonl(str(path))
        assert len(back) == len(trace)
        assert back[0].qualname == trace[0].qualname
        assert back[0].lineno == trace[0].lineno

    def test_jsonl_appends(self, trace, tmp_path):
        path = tmp_path / "t.jsonl"
        for _ in range(2):
            sink = JSONLSink(path=str(path))
            for ev in trace[:5]:
                sink.write(ev)
            sink.close()
        assert len(path.read_text(encoding="utf-8").strip().split("\n")) == 10

    def test_sqlite_schema_and_counts(self, trace, tmp_path):
        path = tmp_path / "t.sqlite"
        sink = SQLiteSink(str(path), reset=True)
        for ev in trace:
            sink.write(ev)
        sink.close()
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute("SELECT kind, count(*) FROM events GROUP BY kind").fetchall()
            assert sum(n for _k, n in rows) == len(trace)
            assert conn.execute("SELECT count(*) FROM codes").fetchone()[0] > 0
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            assert meta["schema_version"] == "1"
            assert "python_version" in meta
        finally:
            conn.close()

    def test_sqlite_interns_codes(self, trace, tmp_path):
        """Interning codes is the reason the schema is not one wide table."""
        path = tmp_path / "t.sqlite"
        sink = SQLiteSink(str(path), reset=True)
        for ev in trace:
            sink.write(ev)
        sink.close()
        conn = sqlite3.connect(str(path))
        try:
            n_events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
            n_codes = conn.execute("SELECT count(*) FROM codes").fetchone()[0]
            assert n_codes < n_events, "every event should reference a shared code row"
        finally:
            conn.close()

    def test_text_sink(self, trace):
        buf = io.StringIO()
        sink = TextSink(buf, colour=False, kinds=(CALL,))
        for ev in trace:
            sink.write(ev)
        out = buf.getvalue()
        assert out.count("call") == len([e for e in trace if e.kind == CALL])
        assert "\033[" not in out, "colour=False must not emit ANSI codes"

    def test_text_sink_filters_kinds(self, trace):
        buf = io.StringIO()
        sink = TextSink(buf, colour=False, kinds=(RETURN,))
        for ev in trace:
            sink.write(ev)
        assert "line" not in buf.getvalue()

    def test_counting_sink(self, trace):
        sink = CountingSink()
        for ev in trace:
            sink.write(ev)
        assert sink.total == len(trace)

    def test_tee_sink_isolates_failures(self, trace):
        good = CountingSink()
        bad = type("Bad", (), {"write": lambda self, e: (_ for _ in ()).throw(RuntimeError())})()
        tee = TeeSink(good, bad, NullSink())
        raised = 0
        for ev in trace:
            try:
                tee.write(ev)
            except RuntimeError:
                raised += 1
        assert good.total == len(trace), "one failing child must not starve the others"
        assert raised == len(trace), "but the failure must not be swallowed either"

    def test_open_sink_accepts_a_path(self, tmp_path):
        p = tmp_path / "x.txt"
        sink = open_sink(str(p))
        sink.write("hello")
        sink.close()
        assert p.read_text(encoding="utf-8") == "hello"

    def test_open_sink_none_is_null(self):
        assert isinstance(open_sink(None), NullSink)

    def test_sink_failure_is_counted_not_fatal(self, trace):

        bad = type("Bad", (), {"write": lambda self, e: (_ for _ in ()).throw(RuntimeError())})()
        with FunctionTracer(TracerConfig(events=(CALL,), include=lambda c: c.co_name == "top")) as t:
            t.add_sink(bad)
            top()
        assert t.error_count > 0


class TestAnalysis:
    def test_summary_counts(self, trace):
        s = summarise_events(trace)
        assert s["total_events"] == len(trace)
        assert s["events_by_kind"][CALL] > 0
        assert s["coverage_ratio"] == 1.0

    def test_summary_reports_incompleteness(self, trace):
        s = summarise_events(trace, dropped=50, errors=2)
        assert s["coverage_ratio"] < 1.0
        assert s["dropped"] == 50 and s["errors"] == 2

    def test_hot_lines_are_ranked(self, trace):
        rows = hot_lines(trace, limit=5)
        assert rows
        counts = [r["count"] for r in rows]
        assert counts == sorted(counts, reverse=True)
        assert 0 < rows[0]["pct"] <= 100

    def test_exclusive_times_sum(self, trace):
        rows = exclusive_times(trace)
        assert rows
        assert all(r["total_ns"] >= r["self_ns"] for r in rows)

    def test_exception_summary_deduplicates(self):
        with FunctionTracer(TracerConfig(events=("exception",))) as t:
            for _ in range(5):
                try:
                    boom()
                except ValueError:
                    pass
        rows = exception_summary(t.events)
        assert rows
        assert rows[0]["type"] == "ValueError"
        assert rows[0]["count"] >= 1

    def test_render_table_aligns(self):
        text = render_table([{"a": "x", "n": 1}, {"a": "yyyy", "n": 200000}], ["a", "n"])
        lines = text.split("\n")
        assert lines[0].startswith("+")
        assert len({len(line) for line in lines if line.startswith("+")}) == 1

    def test_render_table_empty(self):
        assert render_table([], ["a"]) == "(no rows)"

    def test_confidence_note_is_honest_about_noise(self):
        assert confidence_note(0) == "noise"
        assert confidence_note(5) == "noise"
        assert confidence_note(15) == "low"
        assert confidence_note(100) == "ok"
        assert confidence_note(5000) == "high"


class TestDiff:
    def test_identical_runs_are_equal(self, trace):
        d = diff_traces(trace, trace, min_pct_delta=0.0)
        assert d["comparable"]
        assert d["summary"]["net_pct_delta"] == pytest.approx(0.0, abs=0.01)

    def test_incomparable_runs_are_refused(self, trace):
        with FunctionTracer(TracerConfig(events=(CALL, RETURN),
                                        include=lambda c: c.co_name == "top")) as t:
            top()
        small = t.events
        with FunctionTracer(TracerConfig(events=(CALL, RETURN),
                                        include=lambda c: c.co_name == "top")) as t2:
            for _ in range(20):
                top()
        big = t2.events
        d = diff_traces(small, big, label_before="1 call", label_after="20 calls")
        assert not d["comparable"]
        assert "different amounts of work" in d["reason"]

    def test_new_and_removed_functions(self):
        def before_run():
            return leaf(1)

        def after_run():
            return middle(1)

        with FunctionTracer(TracerConfig(events=(CALL,))) as a:
            before_run()
        with FunctionTracer(TracerConfig(events=(CALL,))) as b:
            after_run()
        d = diff_traces(a.events, b.events)
        assert any("after_run" in n for n in d["new_functions"])
        assert any("before_run" in n for n in d["removed_functions"])

    def test_render_diff_includes_the_incomparable_warning(self, trace):
        short = trace[: len(trace) // 4]
        d = diff_traces(short, trace, label_before="a", label_after="b")
        text = render_diff(d)
        assert "NOT COMPARABLE" in text or d["comparable"]
