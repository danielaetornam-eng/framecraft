# 10 — API reference

Everything public, grouped by layer. Signatures are abbreviated where the
docstring has the detail.

## `framecraft` (root)

### Version and capability

| name | meaning |
|---|---|
| `__version__` | `"1.0.0"` |
| `PY_VERSION` | `sys.version_info[:3]` |
| `HAS_MONITORING` | `hasattr(sys, "monitoring")` — PEP 669 available |

### Event kinds

Use these instead of raw strings, so a typo is an `AttributeError` at import
time rather than a silently dropped event.

| name | value | produced by |
|---|---|---|
| `CALL` | `"call"` | a Python frame was entered |
| `CALL_SITE` | `"call_site"` | a call *expression* evaluated (PEP 669 `CALL`) |
| `RETURN` | `"return"` | a Python frame returned |
| `LINE` | `"line"` | about to execute a source line |
| `OPCODE` | `"opcode"` | one instruction (`settrace`) or `INSTRUCTION` (3.14+) |
| `EXCEPTION` | `"exception"` | raised here, or the frame is being torn down |
| `RAISE_SITE` | `"raise_site"` | a `raise` statement is about to execute (3.14+) |
| `YIELD` | `"yield"` | a generator suspended |
| `C_CALL` | `"c_call"` | a C function was entered |
| `C_RETURN` | `"c_return"` | a C function returned |
| `C_EXCEPTION` | `"c_exception"` | a C function raised |
| `SAMPLE` | `"sample"` | a stack snapshot |
| `KINDS` | tuple | all of the above |

Two distinctions worth keeping straight:

- `CALL` is a frame *entry*; `CALL_SITE` is a call *expression*. `CALL_SITE`
  carries the callee and its first argument, `CALL` does not.
- `EXCEPTION` fires both for the raise site and for the frame teardown;
  `extra["phase"]` is `"raise-opcode"`, `"raised"` or `"unwind"`.

### The event

```python
@dataclass(slots=True)
class TraceEvent:
    seq: int; kind: str; wall: float; cpu: float; perf_ns: int
    code_id: int | None; code_hash: str | None
    name: str; qualname: str; filename: str
    lineno: int | None; offset: int | None
    thread_id: int; thread_name: str; task: str | None
    depth: int; call_id: int | None; parent_call_id: int | None
    arg_repr: str | None; retval_repr: str | None
    exception_repr: str | None; exception_type: str | None
    opcode: str | None; arg: Any; argval: Any; c_name: str | None
    sample_count: int
    locals: list[LocalBinding]; stack: list[StackFrame]
    extra: dict
```

Methods: `.to_dict(include_locals=False)`, `.summary()`, `.location`,
`.short_filename`, `TraceEvent.from_dict()`.

`seq` is monotonic per tracer, except where the underlying API itself drops
events. Use it to prove you did not lose any.

**Clock semantics.** `perf_ns` is `time.perf_counter_ns()`; `wall` is
`perf_ns / 1e9` (same clock, derived); `cpu` is `time.process_time()` **only
when `TracerConfig.capture_cpu` is True**, else `0.0`. All interval arithmetic
should use `perf_ns`, where integer arithmetic is exact.

### Frames, namespaces, values

```python
current_frame(depth=0) -> FrameType | None      # == caller_frame(depth)
caller_frame(skip=0)  -> FrameType | None      # your caller; skip=1 for its caller
walk(start=None, max_depth=None) -> Iterator[FrameType]   # innermost first
frame_depth() -> int
stack_snapshot(include_locals=False, include_source=False, max_frames=None) -> list[StackFrame]
render_stack(max_frames=12, marker="->") -> str          # traceback order
describe(frame=None, include_locals=True) -> dict        # the reference dump
capture_locals(frame, *, policy=..., redactor=..., only=None, structured=True) -> list[LocalBinding]
resolve_name(frame, name) -> (scope, value)
scope_of(frame, name) -> "local"|"free"|"global"|"builtin"|"unknown"
is_suspended_frame(frame) -> bool
suspended_frames() -> list[FrameType]                     # via the GC
clocks(with_cpu=False) -> dict
safe_repr(obj, *, name="", policy=..., redactor=...) -> str
capture_value(obj, *, name="", policy=..., redactor=..., depth=0) -> dict
stack_key(frames, with_lines=True) -> StackKey
```

`caller_frame(0)` is the frame that called `caller_frame`. `walk()` internally
uses `caller_frame(1)`; if you reimplement `walk`, get this right or every depth
shifts by one.

`describe()` returns: `code`, `instruction_pointer`, `namespace_sizes`,
`trace_hooks`, `links`, `closure`, `exceptions`, `locals`, and on 3.11+
`line_table`, `exception_table`, `instructions`.

## `framecraft.trace`

### `TracerConfig`

```python
TracerConfig(
    events=(CALL, RETURN, LINE),
    include=None, exclude=None,               # predicates on the code object
    include_paths=(), exclude_paths=(),       # prefix filters on the filename
    capture_locals=False, capture_stack=False, capture_stack_depth=8,
    capture_args=True, capture_return=True,
    policy=CapturePolicy(), redactor=Redactor(),
    buffer=True, max_events=500_000,
    filter=None,                              # called AFTER the event is built
    on_error=None, include_threads=False, trace_asyncio_tasks=False,
    capture_cpu=False, timing=True,
)
```

Methods: `.wants(kind)`, `.admits(code)`.

Filter order: `exclude_paths` → `include_paths` → `exclude` → `include`. So
`exclude_paths` wins over everything, and `include` narrows last.

### `Tracer` (base)

`__enter__` / `__exit__`, `.start()`, `.stop()`, `.add_sink(sink)`,
`.stats()`, `.drain()`, `.drain()` clears the buffer.

Attributes: `.events`, `.dropped`, `.error_count`, `.active`, `.config`,
`.duration_ns`, `.uses_hook_slot`.

Set `FRAMECRAFT_DEBUG=1` to have hook errors print their tracebacks.

### `FunctionTracer` (alias `LineTracer`)

```python
FunctionTracer(config=None, *, opcode_events=False, **overrides)
```

Methods: `.should_trace(code)`, `.verbosity_for(frame)`,
`.apply_verbosity(frame)`, `.attach_existing(start=None, depth=None) -> int`,
`.arm_thread() -> bool`, `.attach_count`.

Module functions: `install_thread(tracer) -> bool`,
`uninstall_thread()`.

### `MonitorTracer`

```python
MonitorTracer(config=None, *, tool_id=None, tool_name="framecraft", **overrides)
```

Requires 3.12+; raises `RuntimeError` on older interpreters rather than failing
later.

Methods: `.watch_code(code, kinds)`, `.unavailable_events` (set of event names
the interpreter refused), `.depth_semantics` (str), `EVENT_MAP`.

`MonitorTracer(events=())` is legal and means *globally silent, arm me per code
object*.

### `ProfileTracer`

```python
ProfileTracer(config=None, *, trace_c=False, **overrides)
```

`trace_c=True` enables `c_call`/`c_return`/`c_exception`. Every `return` event
carries `extra["duration_ns"]`.

### `SamplingTracer`

```python
SamplingTracer(config=None, *, interval=0.001, trace_threads=True,
               max_depth=64, skip_own_thread=True, **overrides)
```

Attributes: `.tally` (`StackKey -> count`), `.self_tally`, `.inclusive_tally`,
`.line_tally`, `.names`, `.total_samples`, `.ticker`.

Methods: `.name_of(hash)`, `.report(limit=25)`, `.effective_hz`.

`uses_hook_slot = False`, so it composes freely with the others.

### Composition and decorators

```python
compose(*tracers) -> CompositeTracer    # indexable, iterable, rejects conflicts
trace_function(*kinds, capture_locals=True, scope="file", backend="auto")
trace_monitor(*kinds)                  # raises below 3.12
profile_function(*kinds)
```

Decorators leave the tracer on `fn.last_tracer`.

### `HookLease`

`.current()`, `.claim(tracer)`, `.release(tracer)`. Raises `RuntimeError` naming
the conflict rather than letting two tracers share one slot.

## `framecraft.sinks`

| name | purpose |
|---|---|
| `Sink` | the protocol: `write(event)` |
| `NullSink()` | discard |
| `CountingSink()` | `.total`, `.counts` — the first thing to install when a tracer appears dead |
| `TeeSink(*children)` | fan-out; one failure does not starve the others, and it is re-raised |
| `open_sink(spec)` | path / stream / sink → something with `.write` |
| `TextSink(stream, kinds=, colour="auto", show_locals=, show_stack=, width=, header=)` | one line per event |
| `CSVSink(stream=, path=)` | flat, spreadsheet-friendly |
| `JSONLSink(stream=, path=, include_locals=, include_stack=, flush_every=512)` | the interchange format |
| `SQLiteSink(path=, include_locals=, commit_every=2000, reset=)` | indexed, queryable |
| `SVGFlameSink(path, min_width_px=, title=, unit=)` | streaming flame graph; `.close()` writes |
| `DOTSink(path, min_calls=, max_nodes=, max_events=, render=)` | call graph; `.close()` writes |
| `HTMLReport(events, **kw)` | `.render()`, `.write(path)` |
| `flame_svg(tree, *, width=1200, row_height=18, title=, unit=, min_width_px=)` | pure function |
| `layout(tree, *, width=, row_height=, min_width_px=)` | pure function → `(rects, px_per_unit)` |
| `callgraph_dot(graph, *, name=, min_calls=, max_nodes=, show_cycles=, rankdir="LR")` | pure function |
| `render_dot(src, out_base, engine="dot")` | shells out; `None` if Graphviz absent |
| `render_report(events, **kw)` | the HTML string |
| `query_sqlite(path, sql, params=())` | convenience |

`TraceEvent.to_dict()` is the JSONL schema and is **append-only**: add keys, do
not rename them.

## `framecraft.graph`

```python
CallGraph()                # .add_call .add_return .finish .find_cycles .roots
                            # .leaves .fan_in .to_dict  (+ lazy adjacency)
CallNode                   # ncalls self_ns total_ns elapsed_ns exceptions
                            # callees callers mean_ns to_dict
build_call_graph(events, attribute_self_time=True) -> CallGraph

StackTree(root_label="all")  # .add_stack .add_path .walk .hottest_paths
                             # .collapse .total .to_dict
build_stack_tree(events, min_weight=1) -> StackTree

Timeline()                 # .add_call .add_exit .threads .thread_name .overlaps
Span                       # start_ns end_ns duration_ns child_ns
                            # uninstrumented_ns closed descendants to_dict
build_timeline(events) -> Timeline
```

## `framecraft.analysis`

```python
summarise_events(events, *, dropped=0, errors=0, duration_ns=None) -> dict
hot_lines(events, *, limit=25, root=None) -> list[dict]
hot_functions(events, *, limit=25, root=None) -> list[dict]
exclusive_times(events, *, limit=25) -> list[dict]
inclusive_times(events, *, limit=25) -> list[dict]
exception_summary(events, *, limit=20) -> list[dict]
recursion_report(events) -> dict
render_table(rows, columns, *, headers=None, max_width=200) -> str
confidence_note(samples) -> "noise"|"low"|"ok"|"high"

TraceProfile(label); profile_trace(events, label=...) -> TraceProfile
DiffRow(...);  diff_traces(before, after, *, label_before, label_after,
                            min_pct_delta=1.0, limit=40,
                            comparable_tolerance=0.05) -> dict
render_diff(diff) -> str
load_jsonl(path) -> list[TraceEvent]
```

`diff_traces` returns `comparable`, `reason`, `before`, `after`,
`new_functions`, `removed_functions`, `rows`, `summary`. `render_diff` prints a
`NOT COMPARABLE` banner when `comparable` is False.

## `framecraft._internal`

Private. Use only if you are writing a tracer.

| module | contents |
|---|---|
| `compat` | every version flag, `monitoring_event()`, `free_tool_ids()`, `DISABLE_EVENTS`, `RESERVED_TOOL_IDS`, `probe()` |
| `codeinfo` | `code_id()`, `code_hash()`, `qualified_name()`, `describe_code()`, `offset_to_line()`, `instructions_at_line()`, `exception_table()`, `is_internal()` |
| `values` | `CapturePolicy`, `Redactor`, `safe_repr`, `capture_value` |

`codeinfo.is_internal(code)` is the guard every tracer uses to exclude itself.

## `fc-trace`

```
fc-trace run <script> [args...] [--engine auto|settrace|monitoring|profile|sample]
                             [--events call,line,return] [-o PATH]
                             [--format jsonl|csv|sqlite|text|html|dot|svg]
                             [--locals] [--stack] [--stack-depth N] [--args]
                             [--include PREFIX] [--exclude PREFIX]
                             [--interval S] [--c-calls] [--buffer] [--min-flame N]
fc-trace report <trace> [--html PATH] [--top N] [--min-flame N] [--no-flame]
fc-trace graph <trace> [--kind flame|call|both] [-o PREFIX]
               [--min-calls N] [--max-nodes N] [--min-flame N]
fc-trace diff <before> <after> [--min-pct F] [--json]
fc-trace query <db> <SQL>
fc-trace inspect <file.py[:LINE|::name]> [--json] [--limit N]
fc-trace demo [--engine ...] [--html PATH]
```

Options for `run` may appear **anywhere**; unrecognised arguments and anything
after `--` are forwarded to the script. (`argparse.REMAINDER` was tried and
rejected: it swallowed `-o` and every other flag, so `fc-trace run app.py -o x`
silently wrote to the default path.)

Exit codes: `0` success, `1` for an incomparable diff, `SystemExit` with a
message for bad arguments. A trace is written **even when the script raises** —
the crash is usually the thing you are debugging.