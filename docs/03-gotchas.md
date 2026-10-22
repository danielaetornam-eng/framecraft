# 03 — Gotchas catalogue

Every entry has a **reproducer** you can paste into a REPL and a **fix**. Each
one was found by writing it, running it, and being wrong.

Index: [G1](#g1) [G2](#g2) [G3](#g3) [G4](#g4) [G5](#g5) [G6](#g6) [G7](#g7)
[G8](#g8) [G9](#g9) [G10](#g10) [G11](#g11) [G12](#g12) [G13](#g13) [G14](#g14)
[G15](#g15) [G16](#g16) [G17](#g17) [G18](#g18) [G19](#g19) [G20](#g20)

---

## G1. A local trace function must return itself

The return value of the local hook **replaces** `frame.f_trace`.

```python
import sys
from collections import Counter

def work():
    a = 1; b = 2; c = 3
    return a + b + c

seen = Counter()
def local(frame, event, arg):
    seen[event] += 1
    return None              # <-- implicit in any def that forgets

def glob(frame, event, arg):
    return local if event == "call" else None

sys.settrace(glob); work(); sys.settrace(None)
print(seen)
```

On modern CPython this keeps tracing, because `trace_trampoline` only assigns
when the result is not `None`:

```
Counter({'line': 3, 'return': 1})
```

But the **documented contract** is that you return yourself, and older
interpreters obeyed it. More importantly, forgetting the return is how you
*intend* a swap and get silence instead.

**Fix:** `return local_trace` on every path, including the exception path.
Prefer a `try/finally`:

```python
def local(frame, event, arg):
    try:
        handle(frame, event, arg)
    finally:
        return local_trace
```

Pinned by `tests/test_tracers.py`.

---

## G2. An exception in a hook kills the traced program *and* the tracer

```python
import sys
log = []
state = {"n": 0}

def local(frame, event, arg):
    log.append((event, frame.f_lineno))
    if event == "line":
        state["n"] += 1
        if state["n"] == 3:
            raise RuntimeError("hook bug")
    return local

def glob(frame, event, arg):
    return local if event == "call" else None

sys.settrace(glob)
try:
    def after():
        p = 1; q = 2; r = 3
        return p + q + r
    after()
except RuntimeError as exc:
    print("caught:", exc)
    print("gettrace() ->", sys.gettrace())
sys.settrace(None)
```

Two separate harms from one typo:

1. the exception **propagates into the traced program**, surfacing as if raised
   at whichever line was executing — so your program dies with a stack trace
   pointing at innocent code;
2. CPython **unsets the global hook**, so `sys.gettrace()` returns `None` and
   every later frame is untraced.

**Fix:** wrap every hook body. `Tracer._guard` catches `BaseException`, counts
it in `error_count`, and notifies `on_error`. Set `FRAMECRAFT_DEBUG=1` to see
the tracebacks instead of hiding them.

---

## G3. The global hook returning `None` is the only cheap filter

```python
def glob(frame, event, arg):
    return local            # instruments EVERYTHING, including C-adjacent stdlib
```

Returning `None` declines the frame at frame-entry, costing one Python call per
**call**. Filtering `line` events instead costs a Python call per **line**, which
for a tight loop is two orders of magnitude worse.

`TracerConfig.include_paths` / `include` / `exclude` are all evaluated in the
global hook for exactly this reason.

---

## G4. `sys.settrace` is per-thread, and missing one is silent

```python
import threading
from framecraft import FunctionTracer, TracerConfig, LINE

out = []
with FunctionTracer(TracerConfig(events=(LINE,))):
    t = threading.Thread(target=lambda: out.append(1))
    t.start(); t.join()
# no events from the worker: it was never hooked
```

`sys.settrace` writes a thread-state field. `threading.settrace` covers threads
started *after* it runs, which is why `TracerConfig(include_threads=True)` exists.

**Fix:** `include_threads=True` when you start the threads yourself. The
symptom when you forget — "the program was idle" — is a wrong conclusion, not a
missing feature.

---

## G5. `f_trace` assignment needs a global hook **in that thread**

This is the most expensive one to find, because the call *succeeds*.

```python
import sys, threading

# (1) no global hook anywhere
seen = []
def local(frame, event, arg):
    seen.append(event); return local
def deep(n):
    if n == 0:
        f = sys._getframe()
        while f is not None:
            f.f_trace = local; f.f_trace_lines = True
            f = f.f_back
        x = 1; y = 2
        return x + y
    return deep(n - 1) + 1
deep(3)
print("no global hook      ->", len(seen), "events")

sys.settrace(local)
seen.clear()
deep(3)
sys.settrace(None)
print("with a global hook  ->", len(seen), "events")
```

Measured:

```
no global hook      -> 0 events
with a global hook  -> 30 events
```

Cross-thread is the same story:

```python
t = FunctionTracer(TracerConfig(events=(LINE,)))
with t:
    th = threading.Thread(target=worker); th.start()
    frame = sys._current_frames().get(th.ident)
    n = t.attach_existing(frame)     # returns >= 1: "success"
    th.join()
assert not [e for e in t.events if e.thread_name == th.name]   # ...and zero events
```

The docs state the requirement — "a global tracing function must have been
installed with `settrace()` in order to enable the runtime tracing machinery" —
and it is per-thread, because the hook slot is thread state.

**Fix:** `arm_thread()` **from inside** the thread you want to attach to. Note
the trade-off: it changes that thread's behaviour until it exits, so do it
deliberately. `FunctionTracer.attach_count` exists so "it attached to N frames
and saw nothing" is one glance rather than an experiment.

Demonstrated in `examples/11`; pinned by two tests.

---

## G6. `co_nlocals` is empty for module frames

```python
code = compile("a = 1\nb = 2\n", "<t>", "exec")
print(code.co_nlocals, code.co_nlocalsplus)
# () None
```

Module-level names are **globals**, not fast locals. A tracer that iterates
`co_varnames` to capture locals sees an empty module frame and concludes there is
nothing to capture. Meanwhile `frame.f_locals is frame.f_globals` for a module
frame, so a *correct* capture of module code yields your program's entire
namespace — which is why capturing module-level code per line event is the most
expensive thing a tracer can do.

**Fix:** iterate `f_locals`, not `co_varnames`, and cap the result.
`capture_locals()` does both and labels each binding's scope.

---

## G7. `f_locals` on a function frame has no globals or builtins

```python
def f():
    import sys
    return sorted(__import__("framecraft").capture_locals(__import__("sys")._getframe()))

print(f())
# ['sys']      <- only the local import; `len` and `framecraft` are not there
```

`f_locals` for a function frame is the fast-locals array. Globals live in
`f_globals`, builtins in `f_builtins`. A tool that claims to have captured a
function's whole namespace from `f_locals` is wrong.

**Fix:** `scope_of()` / `resolve_name()` implement the full four-step lookup if
you need to go looking.

---

## G8. `f_trace_opcodes` is silently dropped on CPython 3.14

The documented idiom:

```python
def glob(frame, event, arg):
    if event == "call":
        frame.f_trace_opcodes = True     # <-- LOST
        return local
```

Measured on 3.14.6:

```
flags-then-return      -> {'line': 7, 'return': 1}          # no opcodes
assign f_trace first   -> {'line': 7, 'opcode': 26, 'return': 1}
```

`f_trace_lines` survives; `f_trace_opcodes` does not. Assigning `frame.f_trace`
*before* the flags works, because the flag setter then has a live trace object to
write into.

**Fix:** always `frame.f_trace = self._local` first, then the flags.
`FunctionTracer._apply_verbosity` does both, so it is correct whether or not a
future version fixes the underlying quirk.

---

## G9. `frame.f_trace_functions` does not exist

Added in 3.13 as a read-only accessor (CPython defines only a getter,
`frame_get_trace_functions`), and **absent again on 3.14.6**.

```python
>>> hasattr(sys._getframe(), "f_trace_functions")
False
```

So it is neither a control nor reliably readable. Detect it with `hasattr`, not
with a version comparison.

**Fix:** express "call/return only" as `f_trace_lines = False`, which is
writable and is what the flag actually does.

---

## G10. `sys.monitoring` API changes, measured

Three separate things bite on 3.14:

**(a) The C-call event family was removed.** `C_CALL`, `C_FUNCTION` and
`C_FUNCTION_RETURN` are gone; `INSTRUCTION` (PEP 749) replaced them.

```python
>>> [n for n in ("C_CALL", "C_FUNCTION", "C_FUNCTION_RETURN")
...  if hasattr(sys.monitoring.events, n)]
[]
```

**(b) `EXCEPTION` is gone too.** The surviving exception-family events are
`RAISE`, `PY_UNWIND`, `PY_THROW`, `RERAISE`, `EXCEPTION_HANDLED`.

Without mapping `RAISE` in, `try: raise X except X: pass` is *completely
invisible* to a PEP 669 tracer on 3.14 — the frame is caught in, so `PY_UNWIND`
never fires for it.

**(c) `set_events` rejects the C pair.** On 3.14.6:

```python
m.use_tool_id(4, "p")
m.register_callback(4, m.events.C_RETURN, cb)
m.register_callback(4, m.events.C_RAISE,  cb)
m.set_events(4, m.events.C_RETURN | m.events.C_RAISE)
# ValueError: cannot set C_RETURN or C_RAISE events independently
```

Registering an unrelated `CALL` callback makes the same call succeed, which is
what makes it a bug rather than a rule.

**Fixes, all in `compat.py` and `MonitorTracer`:**

- probe `sys.monitoring.events` for what exists before touching it;
- declare every callback `*args` and unpack by position with `None` padding;
- map `RAISE` into the `exception` kind;
- `_set_events_with_fallback` retries without the offending group and records
  what it lost in `unavailable_events`, so the report says so instead of
  producing a thinner trace than you asked for.

---

## G11. A sampler blocks every real tracer if it takes the hook lease

`sys.settrace` is one slot per thread, so `framecraft` serialises tracers through
`HookLease`. If a `SamplingTracer` claimed it — despite installing no hook — then
`compose(SamplingTracer(...), FunctionTracer(...))` would fail, which is the one
combination the library most wants to support.

```python
with SamplingTracer(interval=0.001):
    with FunctionTracer(TracerConfig(events=(CALL,))):
        ...     # works
```

**Fix:** `Tracer.uses_hook_slot` is `False` for `SamplingTracer`, and
`start()`/`stop()` consult it. Pinned by `test_sampler_does_not_take_the_hook_lease`.

---

## G12. `sys.monitoring.DISABLE` is not a valid argument to `set_events`

```python
m.set_events(2, m.DISABLE)
# TypeError: 'object' object cannot be interpreted as an integer
```

`DISABLE` is a bare `object()` sentinel. The value that disables a tool is
plain `0`, which is the same thing as `sys.monitoring.events.NO_EVENTS`.

**Fix:** `compat.DISABLE_EVENTS = 0`, used everywhere.

---

## G13. Free tool ids are scarce and not where you think

On 3.14 the reserved set is `{0: DEBUGGER, 1: COVERAGE, 2: PROFILER,
5: OPTIMIZER}`, so the free range is **3 and 4** — two ids, not four. The
`PROFILER_ID` and `OPTIMIZER_ID` reservations were added in 3.14 and the docs
lagged.

```python
>>> sorted(compat.RESERVED_TOOL_IDS), compat.free_tool_ids()
([0, 1, 2, 5], [3, 4])
```

**Fix:** `compat.free_tool_ids()` scans `get_tool()` over `range(0, 6)` and
excludes the reserved set. Never hard-code a range. Always `free_tool_id()` on
exit or the next `use_tool_id` in the process fails.

---

## G14. PEP 669 callbacks do not receive a frame

You get `(code, offset, …)`. If you need the live frame — for locals, or for
real stack depth — you must walk `f_back` matching `f_code is code`.

Two consequences:

- The naive implementation hard-codes a hop count from the callback to the
  traced frame. It is 4 today and changes when you refactor. `MonitorTracer`
  instead skips leading frames whose code belongs to framecraft's own machinery,
  so the number means "depth in the traced program".
- `PY_UNWIND` fires while the frame is being destroyed, and walking `f_back`
  from inside the callback *does* find it — `f_back` is still intact and
  CPython keeps the frame alive for the callback's duration. That is what makes
  `PY_UNWIND` the only event at which a failed frame's locals are readable.

---

## G15. Frame depth from `f_back` is wrong for unwinding events

For `LINE`, the matching frame is the innermost one, so counting frames below it
gives real depth. For `PY_RETURN` and `PY_UNWIND` the frame is being detached,
so it is not.

**Fix:** `MonitorTracer` always reports the **counter** (how many admitted frames
are live beneath), and `depth_semantics` says so in the report. Counters can
drift if an event kind is unhandled, which is why the string is published rather
than assumed.

---

## G16. Frames are not GC-tracked, so `gc.get_objects()` finds nothing

```python
import gc, types
def g1():
    x = 1
    yield 1
    yield 2
gen = g1(); next(gen)
print(gc.is_tracked(gen), gc.is_tracked(gen.gi_frame))
# True False
```

A frame is only itself GC-tracked when it happens to hold references to other
tracked objects, so the small frames you most want to find are invisible.

**Fix:** `suspended_frames()` collects *owners* (generators, coroutines,
async generators) and reads `gi_frame`/`cr_frame`/`ag_frame`. Pinned by
`test_suspended_frames_finds_parked_generator`.

---

## G17. A dropped coroutine is freed, not collected

```python
parked = []
def park(n):
    c = outer(n); c.send(None); parked.append(c)
park(1)
del parked
len([f for f in suspended_frames() if ...])   # 0
```

CPython is reference counted: dropping the last reference frees immediately and
the cyclic GC never sees it. `del` is a `free()`, not a leak. The sweep only
finds objects kept alive by a **reference cycle** or by a container whose handle
you lost.

`examples/10` section 5 shows the failing version first and then the case that
actually exists.

---

## G18. Generators produce many `call` events

One `counter(3)` expression. Events from `examples/03`:

```
by kind: {'call': 4, 'line': 11, 'return': 4}
```

Four `call` events for one call, because the frame is created once but `call`
fires on every **resume**. A function-call counter built on `call` events
over-counts generators by exactly the consumption count — the single most common
way a Python profiler's call counts look wrong.

---

## G19. One failure produces many `exception` events

`examples/09`, tracing one `BatchError` through four frames:

```
depth=3  parse_record     ValueError
depth=2  load_batch       ValueError
depth=3  parse_record     ValueError
depth=2  load_batch       BatchError
depth=1  process_batch    BatchError
```

The exception propagates and every frame it passes through gets an event. That
is correct, and it means `len(exception events) != len(exceptions raised)`.

**Fix:** deduplicate by `(type, code_hash, lineno)`. `exception_summary()` does.

---

## G20. Ten silent-failure traps, in one place

| trap | symptom | guard in framecraft |
|---|---|---|
| locals of a module frame | whole program in every event | `capture_locals` cap + scope labels |
| self-referential container | `repr` loops forever | `reprlib` + identity-path guard |
| `__repr__` that raises | tracer dies with the program | `safe_repr` catches `BaseException` |
| `__repr__` that blocks | tracer hangs forever | **not fixable in-thread** — see below |
| secret under a dict key | plaintext in a CI artifact | per-key renderer, value patterns |
| `f_trace` assigned cross-thread | zero events, no error | `arm_thread()`, `attach_count` |
| two tracers on one slot | a plausible mixture of both | `HookLease` raises |
| hook raises | program crashes, tracer dies | `_guard` counts it |
| tracer left installed | program 30x slower forever | `__exit__` restores, including `None` |
| frame retained | slow leak of the whole namespace | events hold copies, never frames |

### The one that cannot be fixed

A `__repr__` that blocks on I/O. `CapturePolicy.timeout_seconds` is checked
*between* nested captures, so it cannot interrupt a single `repr` call: there is
no way to time out a Python call from the same thread without signals, and
signals do not interrupt most blocking C calls. Measured in `examples/13`: a
0.4 s budget against a 2 s `repr` yields 2.0 s.

The real defences, in order of practicality:

1. do not capture locals of code you do not control;
2. **allowlist types** — render `int/str/bytes/float/None/list/dict/tuple/set`
   of those and everything else becomes `<opaque Foo>`;
3. render in a subprocess with a timeout (correct, and absurd).

Option 2 is a policy decision, which is why it belongs in `CapturePolicy`
rather than inside `safe_repr`.