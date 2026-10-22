# 06 — Performance

## The headline

A line tracer costs roughly **three orders of magnitude**. A sampler costs
roughly **nothing**. That is not a tuning problem; it is the difference between
measuring and distorting.

Run the numbers yourself:

```bash
python bench/overhead.py --seconds 2 --reps 5
```

### About benchmark noise, before you trust any number

On the machine these docs were written on (CPython 3.14.6, Windows), the same
command produces figures that differ by 3–4x between runs. So:

- **`bench/overhead.py` measures the baseline twice, once before and once after
  the tracer runs, and prints the drift.** If it exceeds 5% it tells you the
  slowdown column is untrustworthy. That check exists because an early version
  of this benchmark reported the sampler as *faster than no tracer at all*,
  which is not a result, it is an ordering artefact.
- **The reliable measurements are the micro-benchmarks below**, because each is a
  single operation timed 100 000 times.
- **Trust the ordering, not the ratio.** `setprofile` < `settrace call/return` <
  `settrace call/line/return` << `+ locals` ≈ `sampler ≈ baseline` is stable.
  `52x` versus `393x` is not.

## Micro-benchmarks that *are* stable

These are per-operation costs on CPython 3.14.6 / Windows, 100 000 iterations:

| operation | ns | note |
|---|---|---|
| `time.perf_counter_ns()` | 202 | the cheapest clock |
| `time.perf_counter()` | 190 | same clock, float |
| `time.process_time()` | **684** | queries the OS on Windows |
| `threading.current_thread().name` | 324 | |
| `threading.get_ident()` | 169 | |

And the composite:

```python
>>> def clocks_bad():
...     return {"wall": time.perf_counter(), "cpu": time.process_time(),
...             "perf_ns": time.perf_counter_ns(),
...             "thread_id": threading.get_ident(),
...             "thread_name": threading.current_thread().name}
```

That is ~1570 ns of bookkeeping **before a single event object exists**, paid
once per event. At a million events a second it is 1.6 seconds of pure overhead
per second of tracing.

### Two optimisations, and their measured effect

**Derive `wall` from `perf_ns`.** `perf_counter_ns()` and `perf_counter()` read
the *same clock* at different resolutions, so `wall = perf_ns / 1e9` costs
nothing:

```python
perf_ns = time.perf_counter_ns()          # 202 ns
wall = perf_ns / 1e9                      # free
cpu = time.process_time() if capture_cpu else 0.0
```

The two reads are separated by up to ~600 ns, so `wall` is not bit-identical —
but `wall` is only ever used for human-readable durations, while every interval
computation uses `perf_ns`, where integer arithmetic is exact.

**Make `capture_cpu` opt-in.** `process_time()` is 3.4x the cost of
`perf_counter_ns()` and buys one distinction: "burned CPU" versus "waited". Under
the GIL that rarely changes a conclusion. Turn it on for a thread that blocks on
I/O.

**Cache thread identity.** `threading.local` initialised once per thread, not once
per event.

### Where the rest of the time goes

Two more that only show up under profiling:

**`codeinfo.offset_to_line` rescans `co_lines()`.** `co_lines()` is a generator
of spans, so calling it once per `LINE` event is O(instructions) per event. This
alone made a PEP 669 tracer *slower* than `sys.settrace` — the interpreter's
dispatch is cheap and we were throwing it away on a line-table scan. Fix: a
per-code-object dict, built once (`_OpcodeIndex`).

**`TracerConfig.admits` normalises a path string.** `filename.replace("\\", "/")`
per event is a fresh string allocation on the hot path. Fix: memoise the verdict
per code object, keyed on the code object itself (identity hash, and a strong
reference so `id()` reuse cannot confuse it).

```python
cached = self._admits_cache.get(code)      # dict lookup, no allocation
if cached is None:
    cached = (not codeinfo.is_internal(code)) and self.config.admits(code)
    self._admits_cache[code] = cached
```

## The cost model

Roughly, per event, in order of magnitude:

```
interpreter dispatch            ~50-200 ns   (checked in C)
your Python callback            ~1-3 µs      (call + return + argument passing)
  ├─ one clock read             ~200 ns
  ├─ thread identity            ~0           (cached)
  ├─ filter verdict             ~100 ns      (memoised dict lookup)
  ├─ offset -> line             ~50 ns       (memoised dict lookup)
  ├─ TraceEvent construction    ~400 ns      (a 30-field dataclass)
  └─ safe_repr, if capturing     ~1-10 µs     (dominates everything)
buffer append / sink write      ~100 ns      (buffer=True)
```

So: **the callback, not the hook, is the cost.** PEP 669's fast dispatch only
helps if your callback is cheap, and a callback that allocates a dataclass and
reads a clock has already spent more than the dispatch saved.

## The knobs, ranked by effect

| change | effect |
|---|---|
| sample instead of instrument | **~1000x** |
| drop `capture_locals` | **~5-40x** |
| drop `line` events | **~3-5x** |
| `include_paths=` so frames are never armed | **~2-10x** |
| `timing=False` | ~1.1-1.3x |
| `capture_cpu=False` (default) | ~1.1x |
| `buffer=False` + streaming sink | memory, not time |
| `max_depth`/`max_items` in `CapturePolicy` | bounds worst case |

`include_paths` is worth calling out because it is not a filter — it stops the
frame being armed at all, so the cost inside the excluded function is exactly
zero. Filtering `line` events in Python costs a call per line even when you
discard the event.

## Choosing a filter strategy

```python
TracerConfig(include_paths=("mypkg/",))                   # best: not armed
TracerConfig(include=lambda c: c.co_name in HOT)           # fine, once per call
TracerConfig(filter=lambda ev: ev.filename.endswith("x")) # LAST resort, per event
```

`TracerConfig.filter` runs **after** the event has already been built and
captured. It cannot make tracing free; it can only avoid the buffer and the
sinks.

## Memory is a separate axis

Time and memory are usually conflated, and they behave differently.

```python
t = FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN), buffer=False))
t.add_sink(JSONLSink(path="trace.jsonl"))
with t:
    long_running_service()
```

From `examples/14`, 120 600 line events:

| | buffer | file |
|---|---|---|
| events | 120 600 | 68 MB |
| retained in memory | 120 600 | 0 |
| **vs `buffer=False`** | 0 | 68 MB |

Same events, same bytes on disk. The only difference is the in-memory copy —
about 24 MB for that run at roughly 200 bytes an event. A service doing a
million line events per minute holds a quarter of a million events per minute of
uptime. That is not a cache.

Memory is proportional to **stack diversity**, not sample count, for the
sampler: `SamplingTracer.tally` is keyed by stack, so 10^8 samples over 500
distinct stacks is 500 entries.

## Tracemalloc: you are polluting your own measurement

```python
tracemalloc.start(3)
with FunctionTracer(...):
    workload()
_, peak = tracemalloc.get_traced_memory()
```

Measured on `v1_workload(40)` (`examples/16`):

```
peak without a tracer:        6,875 bytes
peak with    a tracer:      470,889 bytes
```

A 65x inflation. Every event object, every rendered string and the buffer itself
is attributed to your hook. No tool can separate them after the fact.

The honest way to use the two together is two passes: **sample first** with
`tracemalloc` alone to find the allocating lines, **then** attribute with the
tracer knowing the distortion is yours.

## What a good profiler does, and a tracer does not

| question | right tool |
|---|---|
| where did the time go? | **sampler** — low, uniform overhead |
| how many times was this called? | instrumenter (exact) |
| what did this call return? | instrumenter |
| which lines ran? | instrumenter, or coverage |
| why is this slow? | both: sample to find, instrument to explain |
| how much memory did this allocate? | `tracemalloc`, untraced |

The pairing:

```python
from framecraft import SamplingTracer, MonitorTracer, TracerConfig
from framecraft.trace import compose

with compose(SamplingTracer(interval=0.001),
             MonitorTracer(events=("call", "line", "return"),
                           include_paths=("mypkg/",))) as (sampler, tracer):
    work()

print(sampler.report(10))       # 1. where the time is
# 2. now filter `tracer` to those ten functions and re-run
```

## Checklist

- [ ] sample for timing, instrument for control flow
- [ ] `include_paths` rather than a per-event `filter`
- [ ] `capture_locals` only when you are chasing a specific event
- [ ] `timing=True` unless you are counting only
- [ ] `capture_cpu` only for I/O-bound threads
- [ ] `buffer=False` + a streaming sink for anything long-running
- [ ] `effective_hz` in every sampled report
- [ ] `dropped`/`errors`/`coverage_ratio` in every report
- [ ] measure the baseline twice when benchmarking
- [ ] remember your own allocations when using `tracemalloc`