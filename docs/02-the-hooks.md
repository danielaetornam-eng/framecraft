# 02 — The four hooks

Each section states the mechanism, then the exact limits, then a reproducer for
the limit so you can confirm it on your interpreter rather than trusting this
document.

---

## 1. `sys.settrace`

### Mechanism

A **two-level** hook. One function, two roles:

```python
def global_trace(frame, event, arg):
    # Called ONLY for 'call'. Return the local hook, or None to decline.
    if event != "call":
        return local_trace
    if not interesting(frame.f_code):
        return None                       # <- the cheap filter
    frame.f_trace = local_trace           # <- see gotcha G8
    frame.f_trace_lines = True
    frame.f_trace_opcodes = False
    return local_trace

def local_trace(frame, event, arg):
    if event == "line":
        ...
    elif event == "return":
        ...                                # arg is the return value
    elif event == "exception":
        ...                                # arg is (type, value, tb)
    return local_trace                     # <- see gotcha G1

sys.settrace(global_trace)
```

The local hook's **return value replaces** `frame.f_trace`. Returning itself
changes nothing; returning another function swaps it mid-frame; returning `None`
leaves it alone on modern CPython (but was documented as "stops tracing" for
years, so do not rely on either reading).

### Limits

| limit | why |
|---|---|
| no line events for C code | it fires on bytecode |
| no C calls at all | `settrace` has no C vocabulary |
| no callee identity on `call` | `arg` is `None`; you would have to disassemble the calling instruction |
| no per-code-object opt-in | the filter is a Python call, once per *call*, which is as good as it gets |
| one slot per thread | `sys.settrace` writes a thread-state field |
| no events for frames that already existed | see `attach_existing` / `arm_thread` |

Reproducer for "no C calls":

```python
import sys
from framecraft import FunctionTracer, TracerConfig, C_CALL

def work(d):
    return d.get(1) + len(d)

with FunctionTracer(TracerConfig(events=(C_CALL,))) as t:
    work({1: 2})
assert t.events == []
```

### What framecraft does about it

`FunctionTracer`. It handles the four rules in chapter 04, reconstructs call
signatures from `co_varnames` (not `f_locals`), caches offset→line maps, and
excludes its own frames.

---

## 2. `sys.setprofile`

### Mechanism

One function, one role, five events, **and it sees C**:

```python
def profile(frame, event, arg):
    if event == "call":      pass   # a Python frame was created; arg None
    elif event == "return":  pass   # a Python frame returned; arg is the value
    elif event == "c_call":  pass   # about to call C; arg is the callable
    elif event == "c_return": pass  # C returned; arg is the value
    elif event == "c_exception": pass  # C raised; arg is the exception
```

There is no two-level structure and therefore no "return yourself" rule.

### Limits

| limit | why |
|---|---|
| **no line numbers** | the hook has no line vocabulary |
| no Python exception events | only `c_exception` exists |
| no callee identity on `call` | same as settrace |
| C-call **arguments are invisible** | `c_call` gives you the callable, and nothing else |
| owns the single `setprofile` slot | so does `cProfile` |

Reproducer for "C-call arguments are invisible":

```python
def work(d):
    return d.get("missing", "fallback")

with ProfileTracer(TracerConfig(events=(C_CALL,)), trace_c=True) as t:
    work({})
assert t.events[0].arg_repr is None          # you know it was dict.get
                                             # and not that it looked up 'missing'
```

Reproducer for "no line numbers":

```python
with ProfileTracer(TracerConfig(events=(CALL, RETURN))) as t:
    numeric_work(20)
assert all(e.lineno is None for e in t.events if e.kind == CALL)
```

### Why `cProfile` looks the way it does

`cProfile` *is* this hook plus statistics. Every limitation above is visible in
its output:

```
ncalls  tottime  percall  cumtime  filename:lineno(function)
    1    0.000    0.000    0.000    mymod.py:63(numeric_work)
```

Function-level, flat, no lines, no exceptions. `line_profiler` exists because
`setprofile` cannot answer the other question.

---

## 3. `sys.monitoring` (PEP 669)

### Mechanism

An explicit registry, not a global slot.

```python
import sys
m = sys.monitoring
m.use_tool_id(2, "mytool")                  # ids 0/1/2/5 are reserved on 3.14
m.register_callback(2, m.events.PY_START, on_start)   # (code, offset)
m.register_callback(2, m.events.LINE,     on_line)    # (code, lineno)
m.register_callback(2, m.events.PY_RETURN,on_return)  # (code, offset, retval)
m.register_callback(2, m.events.CALL,     on_call)    # (code, offset, callable, arg0)
m.set_events(2, m.events.PY_START | m.events.LINE)
...
m.set_events(2, 0)                          # 0, NOT sys.monitoring.DISABLE
m.free_tool_id(2)
```

### What it gives you that nothing else does

1. **The callee and its first argument.** `CALL` fires in the *calling* frame
   with `(code, offset, callable, arg0)`. Reproducer:

   ```python
   from framecraft import MonitorTracer, TracerConfig, CALL_SITE

   def dispatcher():
       return work({1: 2})

   with MonitorTracer(TracerConfig(events=(CALL_SITE,))) as t:
       dispatcher()
   print([(e.qualname_of_caller, e.c_name, e.arg_repr) for e in t.events])
   ```

   There is no `sys.settrace` equivalent. Its `call` event has `arg=None`, so
   you would have to disassemble the calling instruction and resolve the
   operand yourself.

2. **`PY_UNWIND`.** Fires with the in-flight exception *before* the frame is
   destroyed — the only moment its locals exist in a failed state:

   ```python
   with MonitorTracer(TracerConfig(events=(EXCEPTION,), capture_locals=True)) as t:
       try:
           failing()
       except ValueError:
           pass
   for e in t.events:
       if e.extra.get("phase") == "unwind":
           print(e.qualname, e.exception_type, {b.name: b.value for b in e.locals})
   ```

   This works because the frame's `f_back` is still intact and CPython keeps the
   frame alive for the duration of the callback. Verified in `examples/09`.

3. **Per-code-object events, checked in C.**

   ```python
   with MonitorTracer(events=()) as t:              # globally silent
       t.watch_code(interesting.__code__, ("line",))
       noisy()                                     # not traced at all
       interesting(5)                              # traced
   ```

   The equivalent with `sys.settrace` is a Python predicate call *per line*.

4. **Coexistence.** pdb (tool 0), coverage.py (tool 1) and you (tool 3) can all
   be active at once. With `sys.settrace` the last writer wins and the first
   writer's per-frame `f_trace` values remain, producing a mixture that looks
   entirely plausible.

### Limits

| limit | why |
|---|---|
| 3.12+ only | |
| callbacks get a **code object**, not a frame | see below |
| tool ids are scarce | only 3 and 4 are free on 3.14 |
| **arity is not stable across versions** | see below |
| `C_CALL`/`C_FUNCTION` **removed in 3.14** | replaced by `INSTRUCTION` |
| 3.14.6 rejects `C_RETURN` unless an unrelated `CALL` callback is registered | see chapter 03, G10 |

### The two sharp edges

**No frame.** Callbacks receive `(code, offset, …)`. If you need the live frame
(for locals, or for real stack depth) you must find it by walking `f_back` and
matching `f_code is code`. That is O(depth) per event, so `MonitorTracer` only
does it when a capture actually needs it and otherwise uses an O(1) counter —
see `MonitorTracer._ctx`.

**Arity.** Measured on CPython 3.14.6:

| event | documented | observed |
|---|---|---|
| `PY_START` | `(code, offset)` | 2 |
| `PY_RETURN` | `(code, offset, retval)` | 3 |
| `LINE` | `(code, lineno)` | 2 |
| `CALL` | `(code, offset, callable, arg0)` | 4 |
| `C_RETURN` | `(code, offset, callable, arg0, retval)` | **4 — no retval** |
| `INSTRUCTION` | `(code, offset, opname, arg, argrepr)` | **2 — no opname** |

So every callback in `MonitorTracer` is declared `*args` and unpacks by position
with `None` padding. A version that grows an argument degrades to "I did not get
that field" instead of a `TypeError` storm inside a hook.

---

## 4. Sampling

### Mechanism

No hook at all. A daemon thread wakes on a timer, calls `sys._current_frames()`,
and tallies the resulting stacks.

```python
with SamplingTracer(interval=0.001) as s:
    work()
print(s.report(10))
```

### Why the numbers are trustworthy

Instrumenting hooks add cost per bytecode, which is **not proportional across
your program**. A function with many short lines is taxed hardest. That is not a
small distortion — it is the *opposite* of what you want when the question is
"which code is expensive". A sampling profiler does not perturb the thing it
measures.

Measured slowdown, `spin(50)` × 4000, best-of-5 (see `bench/overhead.py`):

| configuration | slowdown |
|---|---|
| baseline | 1.0x |
| `setprofile` call/return | 28x |
| `settrace` call/return | 36x |
| `settrace` call/line/return | **393x** |
| `settrace` call/line/return + locals | 1879x |
| sampling 1 kHz | **0.6x** (i.e. within noise) |

### Limits

| limit | consequence |
|---|---|
| statistical, not exact | never trust a hot spot with < 30 samples |
| no line numbers in `self` samples | the sampler records `f_lineno` of the leaf |
| no arguments, returns, exceptions | |
| cannot see into C beyond the frame | |
| cannot see suspended coroutines at all | they are on no stack |
| **the requested rate is not the achieved rate** | see below |

### The timer

`time.sleep(0.001)` cannot deliver 1 ms on Windows; the default scheduler tick
is ~15.6 ms. Measured on this machine: a requested 1 ms interval achieved
**35–48 Hz**. So every report prints `effective_hz`, and `SamplingTracer.stats()`
includes `min_seconds_for_1000_samples`.

A profile that claims "1 kHz sampling" without checking is lying, and a tool that
prints the achieved rate next to the percentages is the difference between
those two.

### Statistical floor

`SamplingTracer.report()` annotates every row with
`confidence_note(samples)`:

| samples | verdict |
|---|---|
| < 10 | `noise` |
| 10–29 | `low` |
| 30–999 | `ok` |
| ≥ 1000 | `high` |

Read the confidence column before the percentage column. A function at 4% of
runtime with 6 samples is not a finding.

---

## Choosing

```python
def best_engine(events=("call", "line", "return")):
    from framecraft import HAS_MONITORING, FunctionTracer, MonitorTracer
    return MonitorTracer if HAS_MONITORING else FunctionTracer
```

`compose()` exists for the combinations that make sense (sampler + instrumenter,
instrumenter + profiler) and *rejects* the ones that cannot work — two
`settrace` tracers, say — with a message naming the conflict, rather than
letting the second silently win.