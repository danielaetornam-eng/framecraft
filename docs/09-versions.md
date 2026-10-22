# 09 — Version differences

Every behaviour that changed between CPython 3.10 and 3.14 and that affects
tracing. All of it is handled by `framecraft/_internal/compat.py` and the
tracers, and all of it is demonstrated by an example or a test.

Ground truth for this document: **CPython 3.14.6**, Windows. Where behaviour
differs by version it says so.

## Feature matrix

| feature | 3.10 | 3.11 | 3.12 | 3.13 | 3.14 |
|---|---|---|---|---|---|
| `sys.settrace` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `sys.setprofile` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `frame.f_trace_opcodes` | ✓ | ✓ | ✓ | ✓ | ✓ **but see G8** |
| `frame.f_trace_functions` | — | — | ✓ | ✓ | **absent** |
| `frame.clear()` | — | — | — | ✓ | ✓ |
| `dis.Positions` | — | ✓ `col` | ✓ `col` | ✓ `col` | ✓ **`col_offset`** |
| `code.co_qualname` | — | ✓ | ✓ | ✓ | ✓ |
| `code.co_exceptiontable` | — | ✓ | ✓ | ✓ | ✓ |
| `code.co_nlocalsplus` | — | ✓ | ✓ | ✓ | **absent** |
| `sys.monitoring` | — | — | ✓ | ✓ | ✓ |
| `sys.monitoring.DISABLE` | — | — | unusable | unusable | unusable |
| `int_max_str_digits` | — | 4300 | 4300 | 4300 | 4300 |
| inlined comprehensions (PEP 709) | — | — | ✓ | ✓ | ✓ |
| superinstructions | — | — | — | ✓ | ✓ |
| free-threaded build | — | — | — | opt-in | opt-in |

## Probe, do not compare

```python
HAS_MONITORING       = hasattr(sys, "monitoring")
HAS_F_TRACE_FUNCTIONS = hasattr(sys._getframe(), "f_trace_functions")
```

`frame.f_trace_functions` is the cautionary tale: added in 3.13, **gone in
3.14.6**, and read-only where it exists. A version comparison gets it wrong in
one direction or the other; `hasattr` is right on both.

## What changed in 3.11

**Inline caches.** Every instruction is followed by `CACHE` slots, so
`f_lasti` is a byte offset and instruction *index* ≠ byte offset.

```python
>>> [(i, ins.offset, ins.opname) for i, ins in
...    enumerate(dis.get_instructions(code))][:4]
[(0, 0, 'RESUME'), (1, 2, 'LOAD_GLOBAL'), (2, 12, 'LOAD_FAST_BORROW'), (3, 14, 'CALL')]
#                            ^^^ 10 bytes = 4 CACHE slots
```

Anything doing `instructions[f_lasti]` is indexing by byte offset. Use
`co_lines()` or a cached map.

**`co_positions()` / `dis.Positions`.** Column spans, so you can disambiguate a
multi-line statement. Attribute names changed in 3.14 (below).

**Zero-cost exceptions.** `co_exceptiontable` replaces `SETUP_FINALLY`, so
exception regions are *data*, not control flow. `codeinfo.exception_table()`
decodes it via `dis._parse_exception_table` (a private API, guarded).

**`co_qualname`.** Solves "which of these 400 lambdas".

**`int_max_str_digits = 4300`.** `str(10 ** 5000)` now **raises** `ValueError`.
This matters because `capture_value` calls `str()` to decide whether an int is
"big" — so `capture_value(10 ** 5000)` used to raise inside a trace hook, which
by G2 kills the program. The conversion itself is now guarded.

## What changed in 3.12

**PEP 669 landed.** Named events, per-code-object selection, coexistence. The
whole `MonitorTracer` chapter exists because of it.

**PEP 709: comprehensions are inlined.** In 3.11 and earlier a list
comprehension has its own code object (`<listcomp>`); in 3.12+ it is inlined into
the enclosing function. So:

- the opcode trace of the same source looks structurally different;
- a tracer that filters `co_name != "<listcomp>"` was, before 3.12, *silently
  dropping* comprehension work — and after 3.12, there is no such code object to
  drop. The filter flips from "removes a lot" to "removes nothing", so a hot-spot
  ranking changes for reasons unrelated to your code.

**`sys.monitoring.DISABLE` is broken.** A bare `object()` sentinel;
`set_events(tool_id, DISABLE)` raises `TypeError`. Use `0`.

## What changed in 3.13

**PEP 667: writable `f_locals`.** `f_locals` becomes a `FrameLocalsProxy`.
Reading it is cheap; *holding* it and reading later gives you values that
changed after your event fired. `dict(frame.f_locals)` is the correct snapshot
call on every version, and `frame.clear()` releases deliberately.

**Superinstructions.** `STORE_FAST_LOAD_FAST`,
`LOAD_FAST_BORROW_LOAD_FAST_BORROW`, … A tool that hard-codes opcode names from
3.11 will fail to recognise them. Match on the offset, not the name.

**`sys._is_gil_enabled()`.** On a free-threaded build, "waiting" and "computing"
are separable, and thread interleaving is real rather than GIL-serialised.

## What changed in 3.14 — the most disruptive

### Removed: the C-call event family

```python
>>> [n for n in ("C_CALL", "C_FUNCTION", "C_FUNCTION_RETURN")
...  if hasattr(sys.monitoring.events, n)]
[]
```

Replaced by `INSTRUCTION` (PEP 749), which fires for every bytecode **and** every
C call. `EVENT_MAP` sends `opcode` → `("INSTRUCTION",)` on 3.14 and to nothing
before it, so the same config string produces the same framecraft event kinds on
both.

### Removed: `EXCEPTION`

```python
>>> hasattr(sys.monitoring.events, "EXCEPTION")
False
```

Surviving exception-family events: `RAISE`, `PY_UNWIND`, `PY_THROW`, `RERAISE`,
`EXCEPTION_HANDLED`.

Without remapping, `try: raise X except X: pass` is **completely invisible** on
3.14 — the frame is caught in, so it is not torn down and `PY_UNWIND` never
fires. `EVENT_MAP[EXCEPTION]` therefore includes `RAISE`, and
`MonitorTracer._h_raise` emits an `EXCEPTION`-kind event with
`phase="raise-opcode"`.

### Added

`PY_RESUME`, `JUMP`, `BRANCH_LEFT`, `BRANCH_RIGHT`, `STOP_ITERATION`, `RAISE`,
`EXCEPTION_HANDLED`, `PY_THROW`, `RERAISE`, `INSTRUCTION`.

### Reserved tool ids grew

```
DEBUGGER_ID=0  COVERAGE_ID=1  PROFILER_ID=2  OPTIMIZER_ID=5
free: 3, 4            # two, not four
```

`PROFILER_ID` and `OPTIMIZER_ID` were added in 3.14. `compat.free_tool_ids()`
scans `get_tool()` and excludes the reserved set, because the documented range
lagged.

### Broken: the C-return validation

```python
m.use_tool_id(4, "p")
m.register_callback(4, m.events.C_RETURN, cb)
m.register_callback(4, m.events.C_RAISE,  cb)
m.set_events(4, m.events.C_RETURN | m.events.C_RAISE)
# ValueError: cannot set C_RETURN or C_RAISE events independently
```

Registering an unrelated `CALL` callback makes it succeed. `MonitorTracer`
retries without the offending group and records what it lost in
`unavailable_events`.

### `dis.Positions` renamed its columns

```python
>>> p = next(iter(dis.get_instructions(compile("x=1", "<t>", "exec")))).positions
>>> p.col_offset, p.end_col_offset
0, 1
>>> p.col          # AttributeError on 3.14
```

`describe()` reads both spellings through `_pos_attr`, so one place to update
when 3.15 renames something else.

### `co_nlocalsplus` disappeared

```python
>>> getattr(compile("x=1", "<t>", "exec"), "co_nlocalsplus", "absent")
'absent'
```

Frame-size accounting must feature-detect.

## The measured 3.14 `f_trace_opcodes` regression

This one deserves its own entry because the documented idiom silently loses
*every* opcode event:

```python
# documented order
def glob(frame, event, arg):
    if event == "call":
        frame.f_trace_opcodes = True     # LOST
        return local
# -> {'line': 7, 'return': 1}

# assign f_trace first
def glob(frame, event, arg):
    if event == "call":
        frame.f_trace = local           # must come first
        frame.f_trace_opcodes = True
        return local
# -> {'line': 7, 'opcode': 26, 'return': 1}
```

`f_trace_lines` survives; `f_trace_opcodes` does not. The flag setter needs a
live trace object to write into, and the "return the local function" path has
not created one yet at that point.

`FunctionTracer._apply_verbosity` assigns `frame.f_trace` first and then sets
the flags, and returns the local function as well — so it is correct whether or
not a future version fixes the underlying quirk. Pinned by
`test_opcode_events_produce_events`.

## Event callback arity, measured vs documented

Declare `*args`. Always.

| event | documented | observed on 3.14.6 |
|---|---|---|
| `PY_START` | `(code, offset)` | 2 |
| `PY_RETURN` | `(code, offset, retval)` | 3 |
| `PY_YIELD` | `(code, offset, retval)` | 3 |
| `LINE` | `(code, lineno)` | 2 |
| `CALL` | `(code, offset, callable, arg0)` | 4 |
| `C_RETURN` | `(code, offset, callable, arg0, retval)` | **4 — no retval** |
| `C_RAISE` | `(code, offset, callable, arg0)` | 4 |
| `INSTRUCTION` | `(code, offset, opname, arg, argrepr)` | **2 — no opname** |

`MonitorTracer._payload(args, n)` returns the first `n` positional arguments and
pads with `None`. A version that grows an argument degrades to "I did not get
that field"; a version that drops one degrades to `None`. Neither raises inside
a hook, which by G2 would kill the process.

## Behavioural probes in the suite

Not all of it is `hasattr`-able. These were measured and pinned:

| behaviour | how it is pinned |
|---|---|
| opcode events need `f_trace` assigned first | `test_opcode_events_produce_events` |
| `f_trace` needs a global hook in that thread | `test_attach_existing_cross_thread_without_arming_sees_nothing` |
| `C_RETURN`/`C_RAISE` rejected alone on 3.14.6 | `test_unavailable_events_are_recorded` |
| handled raises invisible without `RAISE` | `test_raise_site_is_observed_on_314` |
| a resumed generator re-fires `call` | `test_generator_produces_multiple_call_events` |
| one failure, one event per frame | `test_one_failure_produces_one_event_per_frame` |

## Writing your own version-tolerant code

The pattern, in full:

```python
def pick(*names):
    """First attribute that exists among `names`."""
    for n in names:
        v = getattr(sys.monitoring.events, n, None)
        if v is not None:
            return v
    return None

def call(event, *args):
    """`*args` + positional unpacking with None padding."""
    n = expected_arity.get(event, len(args))
    padded = tuple(args[:n]) + (None,) * max(0, n - len(args))
    return handler(*padded)
```

And the rule that makes it stick: **never let a version difference decide
control flow through an exception.** Every `hasattr` / `getattr(..., default)`
probe above was added because the original code raised `AttributeError` at
install time on a version the author had not tried.