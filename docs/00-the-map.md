# 00 — The map

## Pick your engine

There are four ways to observe Python execution, and they answer different
questions. Choosing wrong is the single biggest source of wasted effort.

| | `sys.settrace` | `sys.setprofile` | `sys.monitoring` | sampling |
|---|---|---|---|---|
| Needs | 3.x | 3.x | **3.12+** | 3.x |
| Python line events | yes | **no** | yes | no |
| Python call/return | yes | yes | yes | no |
| Python exceptions | yes | no | yes (incl. `PY_UNWIND`) | no |
| Opcode / instruction | yes (with a trap, ch. 03) | no | yes (`INSTRUCTION`, 3.14+) | no |
| C function calls | **no** | yes | 3.12/3.13 only | no |
| C call *arguments* | no | no | yes (`CALL`, `arg0`) | no |
| Call *site* (who called whom) | no | no | **yes** | no |
| Per-code-object opt-in | no (Python predicate) | no | **yes, in C** | no |
| Coexists with pdb / coverage | no | no | **yes** | n/a |
| Overhead | 300–600x per line | 25–40x per call | 500x per line¹ | ~1x |
| Statistical | exact | exact | exact | approximate |

¹ The Python-level cost of your callback dominates; see chapter 06.

## The decision procedure

Ask two questions in this order.

### 1. Do you need wall-clock attribution?

**Yes** → sample. `SamplingTracer(interval=0.001)`. Reasons, in order of
importance:

- instrumenting tracers add cost *per line*, so they tax line-dense code hardest
  — precisely the code you are usually trying to measure;
- a sampled profile has low, roughly uniform overhead, so the program behaves
  the way it will in production while you watch it;
- you can profile code that cannot be instrumented (a C extension, a signal
  handler) and threads you never hooked.

The price is exactness. See `examples/07`.

**No** → instrument. You get every event, exactly, with line numbers, arguments,
return values and exceptions. Use `MonitorTracer` on 3.12+ and `FunctionTracer`
before; they emit the *same* `TraceEvent` objects, so everything downstream is
engine-agnostic.

### 2. Do you need to see into C?

**Yes** → you are on 3.12 or 3.13, add `ProfileTracer` alongside. On 3.14 the
C-call events were removed from `sys.monitoring` and the replacement is
`INSTRUCTION`, which fires for C calls too (chapter 09).

**No** → `FunctionTracer` / `MonitorTracer` alone.

## The canonical pairing

The combination that is right far more often than any single engine:

```python
from framecraft import SamplingTracer, MonitorTracer
from framecraft.trace import compose

with compose(SamplingTracer(interval=0.001),          # cheap: where is the time?
             MonitorTracer(events=("call", "line", "return"))) as (sampler, tracer):
    work()

# 1. sample first -- it costs nothing and tells you which three functions matter
print(sampler.report(10))
# 2. then trace, filtered to those three, to find out *why*
```

Two engines, two questions, one run. Order matters: the sampler is entered
first so its measurement includes the tracer's overhead rather than hiding it.

This only works because `SamplingTracer` installs no hook and therefore takes no
hook slot — see chapter 03, gotcha G11.

## Which events?

```python
from framecraft import CALL, LINE, RETURN, EXCEPTION, YIELD, CALL_SITE, C_CALL
```

| Kind | Means | Use it for |
|---|---|---|
| `call` | a Python frame was entered | counting calls, building a tree |
| `line` | about to execute a source line | hot lines, coverage |
| `return` | a Python frame returned | inclusive/exclusive time |
| `exception` | raised here / frame torn down | failure analysis |
| `opcode` | one instruction (settrace) or `INSTRUCTION` (3.14) | understanding control flow |
| `yield` | a generator suspended | coroutine debugging |
| `call_site` | a call *expression* evaluated | "who calls this", dependency analysis |
| `c_call`, `c_return`, `c_exception` | a C function was called | where the interpreter spends time |
| `sample` | a stack snapshot | sampling |

Two rules:

- **`call` is not a call count.** A generator resumed N times produces N `call`
  events. Measured in `examples/03` and pinned by
  `test_generator_produces_multiple_call_events`.
- **`exception` is not an exception count.** One failure produces one event per
  frame it propagates through. Deduplicate by `(type, code_hash, lineno)` —
  `exception_summary()` does.

## What to capture

Capture is *not* free, and the costs are wildly different from each other.

| Setting | Cost | Notes |
|---|---|---|
| locations | free | already in the event |
| `capture_args` | ~free | read from `co_varnames`, not `f_locals` |
| `capture_return` | ~free | one `safe_repr` |
| `capture_locals` | **5–40x** | the single most expensive option |
| `capture_stack` | O(depth) per event | only when chasing one event |

Measured in `examples/03` section C: adding `capture_locals` to a small
workload took traced wall time from 1.03 ms to 14.00 ms. That is not a typo.

The pragmatic default:

```python
TracerConfig(events=(CALL, LINE, RETURN),
             capture_args=True, capture_return=True,
             capture_locals=False)
```

…then re-run with `capture_locals=True` filtered to one function when you
actually need the values.

## The 30-second version

```bash
fc-trace demo                                    # see it work
fc-trace run yourprogram.py -o trace.jsonl       # capture
fc-trace report trace.jsonl --html report.html   # understand
fc-trace inspect yourprogram.py::suspect         # open the hood
```

## Where to go next

- Never used a frame object? **01**.
- About to write your own hook? **02**, then **04**.
- Want the list of things that will bite you? **03**.