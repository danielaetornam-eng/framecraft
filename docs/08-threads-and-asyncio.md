# 08 — Threads and asyncio

Three failure modes specific to concurrent code, and one that is a limit rather
than a bug.

## 1. The hook is per-thread, and it fails silently

`sys.settrace` writes a thread-state field. A thread you did not arm is not
traced, and the symptom is a profile where the main thread does all the work and
the workers vanish.

```python
with FunctionTracer(TracerConfig(events=(LINE,))):
    t = threading.Thread(target=work); t.start(); t.join()
# 0 events from the worker
```

Three levels of coverage:

```python
TracerConfig(include_threads=True)     # calls threading.settrace; new threads only
tracer.arm_thread()                    # from inside an already-running thread
tracer.attach_existing(frame)          # instrument frames that already exist
```

`include_threads` is the right answer when *you* start the threads, which is the
common case. `arm_thread` is the escape hatch for a pool that predates the
tracer.

### The trap, measured

`attach_existing` from the wrong thread **succeeds and reports nothing**:

```python
t = FunctionTracer(TracerConfig(events=(LINE,)))
with t:
    th = threading.Thread(target=worker, name="unarmed"); th.start()
    frame = sys._current_frames().get(th.ident)
    n = t.attach_existing(frame)        # >= 1: "success"
    th.join()

sum(1 for e in t.events if e.thread_name == "unarmed")   # 0
```

Because `frame.f_trace` only switches on the per-frame hook, and the interpreter
only consults that if a *global* hook is present **in the frame's own thread**.

```python
# no global hook anywhere        ->  0 events
# global hook in thread A,       ->  0 events
#   frame belongs to thread B
# global hook + f_trace in the   -> 30 events
#   same thread
```

`attach_count` exists so this is one glance rather than an experiment. Both
behaviours are pinned by tests, and `examples/11` demonstrates both side by side.

Note the trade-off: `arm_thread` changes that thread's behaviour until it exits.
Do it deliberately.

## 2. The sampler sees every thread; the instrumenter does not

```python
with SamplingTracer(interval=0.001) as s:
    for t in threads: t.start()
    work()
    for t in threads: t.join()
```

`sampling` reads `sys._current_frames()`, the interpreter's own thread table, so
it cannot have the failure mode above. A worker you forgot to arm is simply
visible.

This is one of the strongest arguments for sampling: "the program was idle" is
the wrong conclusion, and it is the conclusion you reach most easily.

### Two samplers must not sample each other

The sampler's own frames are `Thread.run` → `Event.wait` → `_sampler_loop`, and
they are the innermost frames at sample time — so they claim 100% of `self`
samples:

```python
| name        | self samples | self pct | confidence |
| Thread.run  |           89 |       50 | ok         |   # ← this is framecraft
| hotspot     |           46 |     25.8 | ok         |
```

Excluding the sampler's own thread is *correct* rather than merely tidy: those
samples describe framecraft, not the program. `skip_own_thread=True` by default.

## 3. asyncio: which task is this frame in?

PEP 669 reports coroutine frames individually but has no idea which `Task` owns
one. By the time a coroutine is running, the creating frame is long gone.

```python
from framecraft import MonitorTracer, TracerConfig

with MonitorTracer(TracerConfig(events=("call", "line", "return"),
                                trace_asyncio_tasks=True)) as t:
    await main()

# without trace_asyncio_tasks:  events from 3 tasks, all with task=None
# with    trace_asyncio_tasks:  same events, task='producer-1' / 'consumer-1' / ...
```

The mapping has to be recorded at task *creation*, which is the only moment the
creating frame still exists — hence a task factory rather than a lookup.

```python
def factory(loop, coro, **kwargs):
    outer._task_origins[coro] = {"name": _coro_name(coro),
                                 "created_by": [f.location for f in stack_snapshot(4)]}
    return asyncio.Task(coro, loop=loop, **kwargs) if inner is None else inner(loop, coro, **kwargs)
```

`_install_task_factory` chains any previous factory rather than replacing it, and
`_restore_hooks` puts the old one back — a tracer that silently replaces your
task factory breaks `asyncio`'s own debug mode.

### `sys.set_coroutine_origin_tracking_depth`

```python
sys.set_coroutine_origin_tracking_depth(1)
```

Records the creating frame on each coroutine, so a stack dump of a suspended
coroutine can include where it came from. Costs memory per coroutine, so it is
off by default.

### Suspended coroutines are on no stack

`sys._current_frames()` never lists one. So:

- a **sampling profiler can never attribute time to a suspended coroutine** — the
  coroutine is doing nothing observable by definition;
- an instrumenting tracer records every *resume*, and `PY_YIELD` records every
  suspension, which is why coroutine debugging needs a hook and not a profiler;
- if a coroutine is abandoned, `suspended_frames()` is the only way to find it
  (and only if a reference cycle or a lost container keeps it alive — G17).

```python
>>> [f.f_code.co_qualname for f in suspended_frames() if "fetch" in f.f_code.co_qualname]
['fetch', 'outer']
```

## 4. The limit: you cannot see blocking

This is the most important thing on the page, and it is a **limit**.

A tracer fires on bytecode. A thread blocked in `lock.acquire()`, `sleep()`, or
`socket.recv()` executes no bytecode and produces no events. So a trace of a
program that was waiting on a network call looks like a trace of a program that
was busy.

Three tools, three levels of honesty:

**`time.sleep` and I/O** — visible as `uninstrumented_ns`. Named, not hidden.

**GIL contention** — invisible. Two threads' wall-clock spans overlap (so
`overlaps()` reports it), but you cannot attribute the overlap to either. On a
free-threaded 3.13+ build (`sys._is_gil_enabled()`) this goes away.

**A true deadlock** — invisible in any trace, because a deadlocked thread has no
frames to walk. What you need is `sys._current_frames()` on *all* threads from an
out-of-process observer, which the sampler gives you by construction and which a
hook-based tracer cannot: by the time you can call `sys._current_frames()`, the
deadlocked thread has not returned.

### Timelines show what a flame graph cannot

```python
tl = build_timeline(events)
for start, end, a, b in tl.overlaps():
    print(f"{(start-base)/1e6:8.2f} - {(end-base)/1e6:8.2f} ms  "
          f"{tl.thread_name(a)} || {tl.thread_name(b)}")
```

```
3.10 -     3.19 ms  w0 || MainThread
3.19 -     3.33 ms  w0 || MainThread
...
wall time with >=2 threads inside a span: 8.47 ms of 12.36 ms (69%)
```

The sweep line is O(n log n) — the quadratic version is trivially avoidable and is
the slowest code in the library otherwise.

One implementation note worth recording, because its failure mode is invisible:
the per-thread stack is maintained with `setdefault`, **not**
`dict.get(tid, [])`. `dict.get` with a default does not store the default, so
the stack is discarded and every span gets duration zero and every overlap count
is zero — which looks like "no concurrency" rather than like a broken timeline.

## Debugging checklist

| symptom | likely cause |
|---|---|
| a worker produces no events | `include_threads` not set, or `arm_thread` not called (G4, G5) |
| `attach_existing` returns > 0 but nothing arrives | no global hook in that thread (G5) |
| the sampler profiles only `Thread.run` | `skip_own_thread` is off |
| coroutine frames have `task=None` | `trace_asyncio_tasks` not set |
| `asyncio` debug mode stops working | the task factory was replaced, not chained |
| spans all have duration 0 | the per-thread stack used `dict.get` instead of `setdefault` |
| two threads look sequential | GIL contention; `overlaps()` will show the overlap, the cause will not |
| everything looks idle | the program was blocked; use the sampler from outside the process |
| a coroutine vanished | refcounting freed it (G17); look for the cycle, not the name |

## Threads and redaction

One thing specific to concurrency: `safe_repr`'s recursion guard is a
`threading.local`. A module-level global would make captures on thread B
interfere with thread A's depth budget, and make the guard itself racy. Pinned
by `test_capture_works_concurrently`.