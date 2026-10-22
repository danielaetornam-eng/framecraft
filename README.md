# framecraft

**Execution tracing for Python, built on the frame APIs themselves.**

Most Python profilers call one hook and print a table. This one teaches you
every hook Python has, what each can and cannot see, and gives you the pieces to
build whichever tracer your problem actually needs.

```python
from framecraft import MonitorTracer, TracerConfig
from framecraft.analysis import exclusive_times, hot_lines, render_table

with MonitorTracer(TracerConfig(events=("call", "line", "return"),
                                capture_locals=True)) as t:
    work()

print(render_table(hot_lines(t.events, limit=10), ["location", "count", "pct"]))
print(render_table(exclusive_times(t.events), ["name", "calls", "total_ms", "self_ms"]))
```

```
$ fc-trace demo
$ fc-trace run app.py -o trace.jsonl && fc-trace report trace.jsonl --html report.html
```

Zero runtime dependencies. Python 3.11+.

---

## Why this exists

Tracing is the only window into what a Python program is actually doing, and the
API is full of documented sharp edges that produce **plausible but wrong**
output rather than errors. Some of the ones this library found by writing it:

| | |
|---|---|
| `f_trace_opcodes = True` silently yields **zero** opcode events on CPython 3.14 unless you assign `frame.f_trace` first | [G8](docs/03-gotchas.md#g8-ftrace_opcodes-is-silently-dropped-on-cpython-314) |
| An exception in a trace hook **crashes your program and unsets the hook** | [G2](docs/03-gotchas.md#g2-an-exception-in-a-hook-kills-the-traced-program-and-the-tracer) |
| Assigning `frame.f_trace` cross-thread **succeeds and reports nothing** | [G5](docs/03-gotchas.md#g5-f_trace-assignment-needs-a-global-hook-in-that-thread) |
| `sys.monitoring.DISABLE` is a bare `object()` — `set_events` rejects it | [G12](docs/03-gotchas.md#g12-sysmonitoringdisable-is-not-a-valid-argument-to-set_events) |
| On 3.14 the C-call events are gone, `EXCEPTION` is gone, and `C_RETURN` cannot be enabled alone | [G10](docs/03-gotchas.md#g10-sysmonitoring-api-changes-measured) |
| `co_varnames` is **empty** for module frames, so capturing locals finds nothing | [G6](docs/03-gotchas.md#g6-co_nlocals-is-empty-for-module-frames) |
| One resumed generator fires `call` again — call counters over-count | [G18](docs/03-gotchas.md#g18-generators-produce-many-call-events) |
| One failure fires `exception` once per frame — exception counters over-count | [G19](docs/03-gotchas.md#g19-one-failure-produces-many-exception-events) |
| Frames are not GC-tracked, so `gc.get_objects()` cannot find a suspended coroutine | [G16](docs/03-gotchas.md#g16-frames-are-not-gc-tracked-so-gcget_objects-finds-nothing) |

Twenty in total, each with a reproducer you can paste into a REPL, in
[docs/03-gotchas.md](docs/03-gotchas.md).

---

## The four engines

| | `FunctionTracer` | `ProfileTracer` | `MonitorTracer` | `SamplingTracer` |
|---|---|---|---|---|
| hook | `sys.settrace` | `sys.setprofile` | `sys.monitoring` | none |
| needs | 3.x | 3.x | **3.12+** | 3.x |
| line numbers | yes | **no** | yes | no |
| Python exceptions | yes | no | yes (+`PY_UNWIND`) | no |
| C functions | no | **yes** | 3.12/3.13 only | no |
| call site + args | no | no | **yes** | no |
| per-function opt-in | no | no | **yes, in C** | n/a |
| cost | ~400x / line | ~30x / call | ~500x / line | **~1x** |

All four emit the *same* `TraceEvent`, so every sink and every analysis works
unchanged on any of them.

**Choose by asking two questions.** Need wall-clock attribution → sample
(an instrumenting tracer is a distortion, not a measurement). Otherwise →
instrument, with `MonitorTracer` on 3.12+.

The pairing is the recommended answer for most problems:

```python
from framecraft import SamplingTracer, MonitorTracer
from framecraft.trace import compose

with compose(SamplingTracer(interval=0.001),                  # where is the time?
             MonitorTracer(events=("call", "line", "return"))) as (s, t):
    work()
print(s.report(10))          # sample first, then filter the tracer to those 10
```

---

## What you get

**Tracers** — four engines behind one vocabulary, with `capture_locals`,
`capture_stack`, per-code filters, threading and asyncio support.

**Graphs** — `CallGraph` (coupling, recursion, iterative Tarjan so deep recursion
does not crash the analyser), `StackTree` (flame graphs), `Timeline`
(per-thread Gantt, with the uninstrumented time named rather than hidden).

**Sinks** — streaming JSONL, indexed SQLite, CSV, aligned text, inline-SVG flame
graphs, Graphviz DOT, and a self-contained HTML report with no external assets.

**Analysis** — hot lines, exclusive/inclusive time, exception summaries,
recursion reports, and a trace differ that **refuses to answer** when two runs
did different amounts of work.

**Safety** — a hook can never raise, never retains a frame, never exceeds its
memory budget, never outlives its `with` block, and never traces itself.

---

## Documentation

Start with **[docs/00-the-map.md](docs/00-the-map.md)**.

| | |
|---|---|
| [00 The map](docs/00-the-map.md) | which hook for which question |
| [01 Frame anatomy](docs/01-frame-anatomy.md) | code objects, namespaces, the instruction pointer, suspension |
| [02 The hooks](docs/02-the-hooks.md) | all four engines, with their limits |
| [03 Gotchas](docs/03-gotchas.md) | 20 traps, each with a reproducer |
| [04 Building a tracer](docs/04-building-a-tracer.md) | hook design, scoping, error isolation |
| [05 Capturing state](docs/05-capturing-state.md) | `safe_repr`, redaction, budgets |
| [06 Performance](docs/06-performance.md) | measured overhead, where it goes |
| [07 Graphs and reports](docs/07-graphs-and-reports.md) | flame graphs, call graphs, timelines |
| [08 Threads and asyncio](docs/08-threads-and-asyncio.md) | per-thread hooks, task identity |
| [09 Versions](docs/09-versions.md) | every behaviour that changed, 3.10 → 3.14 |
| [10 API reference](docs/10-api-reference.md) | every public name |
| [11 Cookbook](docs/11-cookbook.md) | task-shaped recipes |

---

## Examples

Sixteen runnable files, each a lesson that prints what it observed and explains
it. Read them in order.

```bash
python examples/01_frame_anatomy.py            # the data structures, no hooks
python examples/02_raw_settrace.py             # the two-level protocol, by hand
python examples/03_line_tracer.py              # FunctionTracer, filtering, capture cost
python examples/04_opcode_tracer.py            # instruction-level, and the 3.14 trap
python examples/05_profile_tracer.py           # seeing into C, and overhead measured
python examples/06_pep669_monitor.py           # PEP 669, and the 3.14 API churn
python examples/07_sampling_profiler.py        # honest timing, honest statistics
python examples/08_call_graph.py               # coupling, recursion, diamonds
python examples/09_exceptions_and_unwinding.py # state at the moment of failure
python examples/10_generators_and_coroutines.py# frames that are on no stack
python examples/11_threads_and_asyncio.py      # per-thread hooks, task identity
python examples/12_rendering.py                # flame graph layout, from scratch
python examples/13_variable_capture.py         # seven ways a repr can hurt you
python examples/14_safeguards.py               # budgets, backpressure, self-exclusion
python examples/15_execution_recorder.py       # a recorder you could actually ship
python examples/16_determinism.py              # allocation attribution, trace diffing
```

---

## CLI

```
fc-trace run <script> [args...]    execute under a tracer; options may appear anywhere
fc-trace report <trace>            summarise; --html for the full report
fc-trace graph <trace>             flame and/or call graph
fc-trace diff <a> <b>              compare two traces (exit 1 if incomparable)
fc-trace query <db> <SQL>          query a .sqlite trace
fc-trace inspect <file.py[:N|::fn]> open the hood on a code object
fc-trace demo                      zero-setup demonstration
```

A trace is written even when the script raises — the crash is usually the thing
you are debugging.

---

## Installation

```bash
pip install -e .            # library + fc-trace
pip install -e ".[dev]"     # + pytest, ruff, mypy
```

No runtime dependencies, on purpose: a tracer that pulls in a dependency tree
cannot be dropped into a broken environment to debug it. Graphviz is optional and
only needed to render DOT.

```bash
python -m pytest              # 302 tests
python bench/overhead.py      # measured overhead
python -m ruff check .
```

---

## License

MIT.