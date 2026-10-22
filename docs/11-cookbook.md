# 11 — Cookbook

Task-shaped recipes. Each one states the question, the approach, and what you
will and will not learn.

---

## "Where is my program spending its time?"

**Sample.** Anything else is a distortion.

```python
from framecraft import SamplingTracer

with SamplingTracer(interval=0.001) as s:
    work()

print(s.stats())                      # note effective_hz and the sample count
print(s.report(20))                   # ranked by SELF samples, with confidence
```

Read the `confidence` column before the percentage column. If the top entry has
fewer than 30 samples, run longer: `stats()["min_seconds_for_1000_samples"]`
tells you how long.

Then:

```python
from framecraft.sinks import flame_svg
from framecraft.graph import StackTree

tree = StackTree("all")
for key, count in s.tally.items():
    tree.add_path([(s.name_of(h), h) for h in reversed(key)], weight=count)
open("flame.svg", "w").write(flame_svg(tree, title="profile", unit="samples"))
```

---

## "Why is this function slow?"

Sample to find the region, then instrument **only that region**.

```python
from framecraft import MonitorTracer, TracerConfig
from framecraft.trace import compose
from framecraft._internal import codeinfo

with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN),
                                 include=lambda c: c.co_name in HOT)) as t:
    work()

from framecraft.analysis import exclusive_times, hot_lines, render_table
print(render_table(hot_lines(t.events, limit=20), ["location", "count", "pct"]))
print(render_table(exclusive_times(t.events), ["name", "calls", "total_ms", "self_ms", "pct_total"]))
```

`HOT` comes from the sampling pass. Two passes beat one expensive pass, because
the tracer only ever arms the frames you care about.

---

## "What was this function called with?"

```python
with FunctionTracer(TracerConfig(events=(CALL,), capture_args=True,
                                 include=lambda c: c.co_name == "handler")) as t:
    handler({"path": "/"}, rows=[1, 2, 3])

for ev in t.events:
    print(ev.arg_repr)      # 'self=..., request={...}, rows=[1, 2, 3]'
```

Reconstructed from `co_varnames[:argcount + kwonlyargcount]` and the live
bindings — deliberately **not** from `f_locals`, which would be an order of
magnitude more expensive. `self` is included because it is slot 0.

---

## "What did it return?"

```python
with FunctionTracer(TracerConfig(events=(RETURN,), capture_return=True,
                                 include=lambda c: c.co_name == "compute")) as t:
    compute(3)

print(t.events[0].retval_repr)          # '42'
```

Or with the far cheaper profile hook:

```python
with ProfileTracer(TracerConfig(events=(RETURN,),
                                include=lambda c: c.co_name == "compute")) as t:
    compute(3)
print(t.events[0].extra["duration_ns"])
```

---

## "What was the state when it failed?"

```python
with FunctionTracer(TracerConfig(events=(EXCEPTION,), capture_locals=True,
                                 include=lambda c: c.co_filename == __file__)) as t:
    try:
        parse_record("broken")
    except ValueError:
        pass

for ev in t.events:
    print(ev.location, ev.exception_type)
    for b in ev.locals:
        print("   ", b.scope, b.name, "=", b.value)
```

```
09_exceptions_and_unwinding.py:71  ValueError
    local text  = broken
    local parts = ['broken']
```

A traceback gives you location only. This gives you the values.

On 3.12+ `PY_UNWIND` is better still — the *last* moment the frame exists:

```python
with MonitorTracer(TracerConfig(events=(EXCEPTION,), capture_locals=True)) as t:
    ...
for ev in t.events:
    if ev.extra.get("phase") == "unwind":
        print(ev.qualname, {b.name: b.value for b in ev.locals})
```

---

## "Which records failed and why?"

```python
from framecraft.analysis import exception_summary, render_table
print(render_table(exception_summary(t.events), ["type", "raised_in", "location", "count"]))
```

Deduplicated by `(type, code_hash, line)`, because one failure produces one
event per frame it passes through and "5 exceptions" is almost never true.

---

## "Show me the call structure."

```python
from framecraft.graph import build_call_graph
from framecraft.sinks import callgraph_dot

with FunctionTracer(TracerConfig(events=(CALL, RETURN))) as t:
    work()

g = build_call_graph(t.events)
print([n.qualname for n in g.fan_in()][:10])     # most-called-by
print(g.find_cycles())                            # recursion
open("g.dot", "w").write(callgraph_dot(g, name="g", min_calls=2))
```

Then `dot -Tsvg -O g.dot`.

---

## "Which lines never executed?"

```python
from framecraft._internal import codeinfo
import dis

executed = {ev.lineno for ev in t.events if ev.kind == "line"}
source = open("mymod.py").read().splitlines()
code = compile(source, "mymod.py", "exec")

for ins in dis.get_instructions(code):
    line = ins.positions.lineno if ins.positions else None
    if line and line not in executed and not ins.opname.startswith("CACHE"):
        print(f"never ran: line {line}  ({ins.opname})")
```

Caveats worth stating: a line can be a docstring, a `pass`, or part of a
multi-line statement attributed elsewhere, so this over-reports. For real
coverage use `coverage.py`, which uses PEP 669's `BRANCH` event on 3.12+.

---

## "Which coroutine is stuck?"

```python
from framecraft import suspended_frames

for f in suspended_frames():
    if f.f_code.co_filename.endswith("myservice.py"):
        print(f.f_code.co_qualname, f.f_lineno,
              {k: str(v)[:60] for k, v in f.f_locals.items()})
```

The only way to find a coroutine nobody holds a reference to *through the
stack*. Recall (G17) it finds only reference cycles and lost containers — a
plainly dropped coroutine is freed immediately.

For tasks specifically:

```python
import asyncio
for task in asyncio.all_tasks():
    print(task.get_name(), task.get_coro(), task.done(), task.cancelled())
```

---

## "Trace a script I did not write."

```bash
fc-trace run theirprogram.py -o trace.jsonl --include theirpackage/
fc-trace report trace.jsonl --html report.html
```

Or in-process, for a test:

```python
with FunctionTracer(TracerConfig(include_paths=("theirpackage/",),
                                 capture_locals=True)) as t:
    theirpackage.main()
```

If something else in the process already owns the hook:

```python
with HookLease.current() or contextlib.nullcontext():   # no: use a fresh process
```

…which is to say: when `HookLease` raises, the honest answer is usually to
discover *who* has the slot. The message names the owner.

---

## "Compare two runs."

```bash
fc-trace run before.py -o before.jsonl
fc-trace run after.py  -o after.jsonl
fc-trace diff before.jsonl after.jsonl
```

```
  42 faster / 17 slower / 118 unchanged; net +3.21 percentage points
  + new: optimised_pipeline
  - gone: legacy_pipeline
```

Exit code 1 when the runs are not comparable, with the reason printed. Trust
that: two runs that did different amounts of work have no meaningful
per-function percentage difference.

---

## "Add a flame graph to a live service."

```python
from framecraft import FunctionTracer, TracerConfig
from framecraft.sinks import SVGFlameSink

sink = SVGFlameSink("/var/log/flame.svg")
t = FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN),
                                 include_paths=("myservice/",),
                                 buffer=False))
t.add_sink(sink)

try:
    with t:
        serve_forever()
finally:
    sink.close()          # renders and writes
```

The sink maintains the stack prefix incrementally, so the only thing that grows
is the tree — bounded by the number of *distinct stack prefixes*, not by the
number of events. Never turned on by default for a reason, though: it is still
~400x overhead.

---

## "Watch a hot function and nothing else."

On 3.12+:

```python
with MonitorTracer(events=()) as t:            # globally silent
    t.watch_code(hot.__code__, ("line", "call", "return"))
    for _ in range(1000):
        hot(i)
```

On any version:

```python
with FunctionTracer(TracerConfig(events=("line",),
                                 include=lambda c: c is hot.__code__)) as t:
    ...
```

---

## "Profile it and instrument it at the same time."

```python
from framecraft import SamplingTracer, MonitorTracer
from framecraft.trace import compose

with compose(SamplingTracer(interval=0.001),
             MonitorTracer(events=("call", "line", "return"),
                           include_paths=("myservice/",))) as (sampler, tracer):
    work()
```

Sampler first, so its measurement includes the tracer's overhead rather than
hiding it. This works because the sampler installs no hook and therefore takes
no hook slot (G11).

---

## "Measure the tracer."

```bash
python bench/overhead.py --seconds 2 --reps 5
```

The benchmark measures the baseline twice and reports the drift, because an
early version reported the sampler as *faster than no tracer at all* — which was
an ordering artefact, not a result. If the drift exceeds 5% the tool says the
slowdown column is untrustworthy.

---

## "Figure out which line allocated the memory."

```python
import tracemalloc
from framecraft import FunctionTracer, TracerConfig, LINE

per_line = {}
prev = 0
last = None

def attribute(event):
    global prev, last
    current, _peak = tracemalloc.get_traced_memory()
    if event.kind != LINE:
        return
    if last is not None:
        d = current - prev
        if d > 0:
            per_line[last] = per_line.get(last, 0) + d
    prev, last = current, (event.filename, event.lineno)

tracemalloc.start(1)
with FunctionTracer(TracerConfig(events=(LINE,),
                                 include=lambda c: c.co_filename == __file__)) as t:
    t.add_sink(type("A", (), {"write": lambda self, e: attribute(e)})())
    workload()
tracemalloc.stop()
```

A line event fires immediately before that line's bytecode, so everything
allocated between event *n* and *n+1* belongs to line *n*.

**Your own allocations are in the measurement.** `examples/16` measures a 65x
inflation. Two passes: sample with `tracemalloc` alone to find the lines, then
attribute knowing the distortion is yours.

---

## "Stop the tracer from tracing itself."

Handled by default — `codeinfo.is_internal()` excludes `framecraft/_internal`,
`core` and `trace`, which is where the hook plumbing lives. Sinks and the CLI are
deliberately *not* excluded, so user code inside them is still traced.

If your own hook module needs excluding, add it to your own tracer's
`should_trace`:

```python
def should_trace(self, code):
    return not code.co_filename.endswith("mytracer.py") and self.config.admits(code)
```

---

## When a tracer reports nothing

Checklist, in the order that finds the cause:

1. **`CountingSink()`** — if its counts are zero, the problem is filtering or
   hook installation, not your renderer.
2. Are frames already executing? `sys.settrace` does not touch them
   (`attach_existing`).
3. Is it another thread? `include_threads`, or `arm_thread()` inside it.
4. Did the global hook raise? `tracer.error_count`, and `FRAMECRAFT_DEBUG=1`.
5. Is `include_paths` matching? It compares *normalised* paths, and wins over
   everything.
6. Did you filter on `co_name == "<listcomp>"`? That silently drops all
   comprehension work on 3.12+.
7. Is the code a generator or coroutine? A resumed generator re-fires `call`;
   a suspended coroutine is on no stack at all.
8. Is another tracer installed? `sys.gettrace()` and `HookLease.current()`.