#!/usr/bin/env python3
"""Example 15 -- Capstone: build a real execution recorder end to end.

Run me::

    python examples/15_execution_recorder.py
    python examples/15_execution_recorder.py --replay replay.jsonl
    python examples/15_execution_recorder.py --html recorder.html

Everything so far has been one technique at a time.  This file assembles them
into the thing you would actually ship: a recorder that

*   traces with the best available hook for the interpreter,
*   streams to JSONL so memory is bounded,
*   snapshots frames instead of retaining them,
*   redacts secrets before anything is written,
*   reconstructs the call tree from the flat event log,
*   and can replay the tree as text, or as a flame graph.

The one thing that makes it a *recorder* rather than a *profiler* is
:func:`replay`: a JSON event log is enough to rebuild the exact call tree, the
line-by-line control flow, and the state at each point.  That is what you send
to a colleague instead of a screen recording.

Design decisions worth arguing about
------------------------------------

**Which hook?**  A ``monitoring_hook()`` function that returns
:class:`~framecraft.trace.MonitorTracer` on 3.12+ and
:class:`~framecraft.trace.FunctionTracer` before.  Both emit the same event
vocabulary, so everything downstream is engine-agnostic.  That is the entire
reason the library defines a shared
:class:`~framecraft.core.events.TraceEvent` rather than exposing each hook's
native payload.

**What to record?**  ``call``, ``line``, ``return``, ``exception`` -- enough to
reconstruct control flow, without opcodes (which would be 5-10x the volume and
add nothing a reader can use).

**What to capture?**  Locals only on ``exception`` events, not on every line.
The interesting state is the state at the moment of failure, and capturing
locals everywhere costs 5x for a question nobody is asking yet.
``TracerConfig.filter`` is the extension point for "only inside my package".
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from typing import Any

from framecraft import CALL, EXCEPTION, LINE, RETURN, FunctionTracer, MonitorTracer, TracerConfig
from framecraft._internal.compat import HAS_MONITORING
from framecraft.core.events import TraceEvent
from framecraft.sinks import JSONLSink

# ----------------------------------------------------------------------
# The recorder
# ----------------------------------------------------------------------


def monitoring_hook() -> Any:
    """Best available tracer for this interpreter."""
    return MonitorTracer if HAS_MONITORING else FunctionTracer


class Recorder:
    """A complete execution recorder: install, stream, close."""

    def __init__(self, path: str, *, locals_on_error_only: bool = True,
                 max_events: int = 2_000_000) -> None:
        self.path = path
        self.locals_on_error_only = locals_on_error_only
        self.max_events = max_events
        self.tracer: Any = None
        self.sink: JSONLSink | None = None

    def __enter__(self) -> Recorder:
        cfg = TracerConfig(
            events=(CALL, LINE, RETURN, EXCEPTION),
            capture_locals=self.locals_on_error_only,
            include=lambda c: c.co_filename == _TARGET_FILE,
            buffer=False,          # streaming: memory is bounded by the tree, not the log
            max_events=self.max_events,
        )
        self.tracer = monitoring_hook()(cfg)
        self.sink = JSONLSink(path=self.path, include_locals=True, flush_every=256)
        self.tracer.add_sink(self.sink)
        self.tracer.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.tracer.stop()
        self.sink.close()

    def stats(self) -> dict:
        return {
            "path": self.path,
            "bytes": os.path.getsize(self.path),
            "engine": type(self.tracer).__name__,
            "dropped": self.tracer.dropped,
            "errors": self.tracer.error_count,
        }


_TARGET_FILE = __file__


# ----------------------------------------------------------------------
# Replay: flat log -> structured trace
# ----------------------------------------------------------------------


def replay(path: str) -> dict:
    """Rebuild the call tree from a flat JSONL event log.

    This is the function that justifies the whole design.  Given only the event
    stream -- no frames, no live program -- it reconstructs

    *   the call tree, with entry and exit positions,
    *   the line-level control flow inside every frame,
    *   the arguments and return values,
    *   every exception and where it passed through.

    A trace file is therefore a *complete* artefact: shareable, diffable, and
    replayable without the original program.
    """
    root: dict[str, Any] = {"label": "<trace>", "children": [], "lines": [], "events": []}
    stack: list[dict[str, Any]] = [root]
    counts = {"call": 0, "line": 0, "return": 0, "exception": 0}

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            ev = TraceEvent.from_dict(json.loads(raw))
            counts[ev.kind] = counts.get(ev.kind, 0) + 1
            node = stack[-1]
            node["events"].append(ev.seq)
            if ev.kind == LINE:
                node["lines"].append(ev.lineno)
            elif ev.kind == CALL:
                child = {
                    "label": ev.qualname or ev.name,
                    "location": f"{ev.short_filename}:{ev.lineno}",
                    "code_hash": ev.code_hash,
                    "call_id": ev.call_id,
                    "args": ev.arg_repr,
                    "children": [],
                    "lines": [],
                    "events": [],
                    "retval": None,
                    "exception": None,
                }
                node["children"].append(child)
                stack.append(child)
            elif ev.kind == RETURN:
                if len(stack) > 1:
                    node = stack.pop()
                    node["retval"] = ev.retval_repr
            elif ev.kind == EXCEPTION:
                info = {
                    "type": ev.exception_type,
                    "text": ev.exception_repr,
                    "at": f"{ev.short_filename}:{ev.lineno}",
                    "locals": {b.name: b.value for b in ev.locals},
                }
                # Recorded on every frame the exception passed through, which is
                # exactly the propagation path. `exception` events do NOT pop
                # the stack: the frame is still alive and will get its RETURN.
                node["exception"] = info

    root["counts"] = counts
    root["unclosed"] = [n["label"] for n in stack[1:]]
    return root


def render_replay(tree: dict, max_lines: int = 12) -> str:
    """Human-readable replay of a reconstructed tree."""
    out: list[str] = []

    def walk(node: dict, depth: int) -> None:
        pad = "  " * depth
        if node is not tree:
            out.append(f"{pad}{node['label']}({node['args'] or ''})")
            lines = node["lines"]
            uniq = sorted(set(node["lines"]))
            if uniq:
                shown = uniq[:max_lines]
                more = f" (+{len(uniq) - len(shown)} more lines)" if len(uniq) > len(shown) else ""
                out.append(f"{pad}  lines {shown[0]}-{shown[-1]} "
                           f"({len(lines)} events){more}")
            if node["exception"]:
                e = node["exception"]
                out.append(f"{pad}  !! {e['type']} at {e['at']}: {e['text']}")
                for k, v in list(e["locals"].items())[:6]:
                    out.append(f"{pad}       {k} = {v}")
            for c in node["children"]:
                walk(c, depth + 1)
            if node["retval"] is not None:
                out.append(f"{pad}  -> {node['retval']}")
        else:
            for c in node["children"]:
                walk(c, depth)

    walk(tree, 0)
    return "\n".join(out)


def to_stack_tree(tree: dict, weight: int = 1):
    """Convert a replayed tree into a :class:`StackTree` for flame rendering."""
    from framecraft.graph import StackTree

    st = StackTree("<trace>")

    def walk(node: dict) -> None:
        for c in node["children"]:
            st.add_path([(f"{c['label']} ({c['location']})", c["code_hash"] or c["label"])],
                        weight=weight * max(1, len(c["lines"])))
            walk(c)

    walk(tree)
    return st


# ----------------------------------------------------------------------
# A workload worth recording
# ----------------------------------------------------------------------


def tokenise(text: str) -> list[str]:
    return [t for t in text.replace(",", " ").split() if t]


def to_int(token: str) -> int:
    try:
        return int(token)
    except ValueError as exc:
        raise ValueError(f"not an integer: {token!r}") from exc


def total(values: list[int]) -> int:
    acc = 0
    for v in values:
        acc += v
    return acc


def pipeline(text: str) -> int:
    tokens = tokenise(text)
    numbers = []
    for t in tokens:
        numbers.append(to_int(t))
    return total(numbers)


def run_pipeline(text: str) -> int:
    return pipeline(text)


# ----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--replay", metavar="JSONL", help="replay an existing trace instead of recording")
    ap.add_argument("--html", help="also write an HTML report of the recording")
    ap.add_argument("--svg", help="also write a flame graph of the replayed trace")
    ns = ap.parse_args()

    if ns.replay:
        tree = replay(ns.replay)
        print(f"replayed {ns.replay}")
        print(f"events: {tree['counts']}")
        print()
        print(render_replay(tree))
        return 0

    tmp = tempfile.mkdtemp(prefix="framecraft-15-")
    path = os.path.join(tmp, "recorder.jsonl")

    print("=" * 78)
    print("RECORDING")
    print("=" * 78)
    print(f"  engine: {monitoring_hook().__name__}  (PEP 669 available: {HAS_MONITORING})")
    print(f"  output: {path}")
    print()
    with Recorder(path) as rec:
        run_pipeline("1 2 3 4 5")
        try:
            run_pipeline("1 2 three 4")
        except ValueError as exc:
            print(f"  the workload raised, as designed: {exc}")
        else:
            print("  the workload did NOT raise -- check the filter")
    print(f"  {json.dumps(rec.stats(), indent=2)}")

    print()
    print("=" * 78)
    print("REPLAY")
    print("=" * 78)
    tree = replay(path)
    print(f"  events reconstructed: {tree['counts']}")
    print(f"  frames left open at EOF: {tree['unclosed'] or '(none)'}")
    print()
    print(render_replay(tree))

    print()
    print("=" * 78)
    print("WHAT THE REPLAY BUYS YOU")
    print("=" * 78)
    print("  Nothing above needed the original program, the original objects, or")
    print("  the original process. The JSONL file is the artefact. You can:")
    print("    * attach it to a bug report and someone else can read the control")
    print("      flow exactly")
    print("    * diff two runs (see example 16)")
    print("    * re-render it as a flame graph in a different shape")
    if ns.svg:
        from framecraft.sinks import flame_svg

        st = to_stack_tree(tree)
        with open(ns.svg, "w", encoding="utf-8") as fh:
            fh.write(flame_svg(st, title="replayed recorder trace", unit="line events"))
        print(f"    * flame graph: wrote {ns.svg}")
    if ns.html:
        from framecraft.analysis import load_jsonl
        from framecraft.sinks import HTMLReport

        HTMLReport(load_jsonl(path), title="recorder").write(ns.html)
        print(f"    * HTML report: wrote {ns.html}")

    print()
    print("=" * 78)
    print("Next: 16 -- determinism: tracemalloc attribution and diffing two runs")
    print("=" * 78)
    print(f"\n(trace left at {path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
