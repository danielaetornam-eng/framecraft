# 04 — Building a tracer

The design of `FunctionTracer` and `MonitorTracer`, and the reasoning behind
each part. Read this before writing your own hook.

## The shape

```python
with FunctionTracer(TracerConfig(events=("call", "line", "return"))) as t:
    work()

for event in t.events:
    print(event.summary())
```

Two decisions worth defending.

**Context manager, not a function.** The most common way to ship a broken
tracer is to forget to uninstall it. A tracer left installed makes the rest of
the process dramatically slower *and keeps references to your buffers*, and the
symptom appears long after you stopped looking.

**One `TracerConfig`, not fourteen kwargs.** So that a config can be built once,
stored, logged, and passed around — which is what makes "reproduce with the same
settings" possible.

## The global hook: decide once per call

```python
def _global_trace(self, frame, event, arg):
    if event != "call":
        return self._local
    code = frame.f_code
    if not self.should_trace(code):
        return None                     # decline: zero cost inside
    call_id = self._next_seq()
    back = frame.f_back
    parent = self._tls.calls.get(id(back)) if back is not None else None
    self._tls.calls[id(frame)] = call_id
    self._tls.depth += 1
    if self.config.wants(CALL):
        self._guard(lambda: self._emit(self._make_event(CALL, frame, call_id=call_id,
                                                          parent_call_id=parent,
                                                          depth=self._tls.depth - 1,
                                                          arg_repr=self._args_repr(frame))))
    self._apply_verbosity(frame)
    return self._local
```

Design points, each of which was a bug first:

**Filtering happens here, not per event.** Returning `None` is checked once per
call, in C. Filtering `line` events in Python costs a call per line.

**`id(frame)` as the call key.** You cannot set attributes on a frame, so you
need a side table. `id()` is safe *because* a frame is alive exactly while it is
on the stack, so no two live frames can share an id, and the entry is deleted on
`return`. An alternative is a per-thread LIFO stack, which leaks the moment
tracing stops mid-call because the matching `return` never arrives.

**The parent is the caller's `id(frame)`.** Not "the top of a stack" — the
caller's frame, looked up in the same table. No stack to keep in sync.

**`frame.f_trace = self._local` before the flags.** See gotcha G8.

## The local hook: never raise, always return self

```python
def _local(self, frame, event, arg):
    if event == "line":
        if self.config.wants(LINE):
            try:
                self._emit(self._make_event(LINE, frame, depth=self._tls.depth))
            except BaseException as exc:
                self._note_error(exc)
        return self._local
    ...
```

Two things happen on every path:

1. `return self._local` — including the `return` and `exception` paths, because
   forgetting it is how a frame goes dark silently (G1).
2. the body is wrapped. Not for tidiness: an exception escaping a hook crashes
   the traced program *and* unsets the hook (G2).

The `try/except` is inlined on the two hottest branches rather than delegated to
`self._guard(lambda: ...)`. At a million events a second, an extra Python frame
per event is measurable; the lambda form costs one, the `try` form costs none.

## Error isolation, in layers

```python
def _note_error(self, exc):
    self.error_count += 1
    if self.config.on_error is not None:
        try:
            self.config.on_error(exc)
        except BaseException:
            pass
    if os.environ.get("FRAMECRAFT_DEBUG"):
        traceback.print_exception(exc)
```

Why each layer:

- **Count first.** A tracer that silently dies halfway is worse than one that
  crashes, because a crash is visible. `error_count` appears in every report.
- **`on_error` is itself guarded.** A callback that raises inside the error
  handler must not escalate.
- **`FRAMECRAFT_DEBUG` defaults off.** A production tracer that prints
  tracebacks from inside a hook is its own outage.

`TeeSink` adds the same guarantee at the fan-out level: one failing sink does
not stop the others, and the first exception is re-raised so it lands in
`error_count` rather than disappearing.

## Snapshotting, never retaining

An event holds a `StackFrame` dataclass, never a `types.FrameType`.

```python
# a retained frame keeps its entire local namespace alive -- including
# anything reachable from it -- for as long as the trace exists
event.stack.append(StackFrame.from_frame(frame))
```

Demonstrated in `examples/14` with a 2 MB object held by a traced frame: the
weakref is dead after the function returns, while the trace still shows the
object as text.

Two consequences you should internalise:

- `f_locals` is read **once** per event and copied. Never store the proxy (3.13+)
  or the dict (≤3.12) — both are wrong: the first is live, the second is stale.
- `f_locals` is expensive (§ chapter 05). `describe()` reads it exactly once.

## Hook ownership

```python
class HookLease:
    _owner = None

    @classmethod
    def claim(cls, tracer):
        if cls._owner is not None and cls._owner is not tracer:
            raise RuntimeError("tracing hook already owned by ...")
        cls._owner = tracer
```

What happens without it: tracer B installs its own global hook; tracer A keeps
the `f_trace` values it already set on running frames; every new frame is
traced by B. You get a trace that is half yours and half someone else's, and it
looks entirely plausible.

`compose()` is the supported way to run several tracers, and it rejects
unconstructible combinations up front:

```python
compose(SamplingTracer(), FunctionTracer())   # fine -- the sampler takes no slot
compose(FunctionTracer(), FunctionTracer())   # RuntimeError: one global slot
```

And `Tracer.uses_hook_slot` is what makes the first line legal (G11).

## Installation and restoration

```python
def start(self):
    if self.active:
        return self
    if self.uses_hook_slot:
        HookLease.claim(self)
    self._prev_trace = sys.gettrace()
    self._prev_profile = sys.getprofile()
    try:
        self._start()
        self.active = True
    except BaseException:
        if self.uses_hook_slot:
            HookLease.release(self)
        raise
    return self

def stop(self):
    try:
        self._stop()
    finally:
        self.active = False
        if self.uses_hook_slot:
            HookLease.release(self)
        self._restore_hooks()
```

Two details that are easy to skip and expensive to debug:

**Restore the *previous* hook, including `None`.** Restoring the wrong hook
breaks someone else's debugger; restoring `None` when something was there
before breaks it in the other direction.

**Detach every `f_trace` you set, before clearing the global hook.**

```python
def _stop(self):
    _detach_all(self._local)     # walk sys._current_frames(), clear our f_trace
    sys.settrace(None)
```

Otherwise already-running frames keep calling into a stopped tracer, and the
symptom is events appearing after the `with` block.

## Streaming vs buffering

```python
t = FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN), buffer=False))
t.add_sink(JSONLSink(path="trace.jsonl"))
with t:
    long_running_service()
```

`buffer=True` is for post-hoc renderers (the HTML report, the call graph) that
need the whole run. For anything long-running it is a leak: measured in
`examples/14`, 120 600 line events over a few seconds is ~24 MB of dataclasses,
and a service doing a million line events per minute holds a quarter of a
million events per minute of uptime.

When the buffer does overflow:

```python
if len(self.events) >= self.config.max_events:
    del self.events[: len(self.events) // 10]     # drop the oldest 10% at once
    self.dropped += 1
```

`del events[:10%]` is O(10%), done once per 10% of the buffer. `list.pop(0)`
would be O(n) *per event* and would dominate the whole tracer on the overflow
path.

And `dropped` is reported everywhere — `coverage_ratio` in every summary, a
warning banner in the HTML report. A trace that lost 99% of its events and said
nothing is worse than no trace.

## Configuring the capture

```python
TracerConfig(
    events=(CALL, LINE, RETURN),
    capture_locals=False,       # 5-40x
    capture_stack=False,        # O(depth) per event
    capture_args=True,          # ~free: from co_varnames, not f_locals
    capture_return=True,        # ~free: one safe_repr
    include_paths=("mypkg/",),
    exclude_paths=("site-packages",),
    max_events=500_000,
    timing=True,                # one clock read per event
    capture_cpu=False,          # +684 ns/event on Windows
)
```

Filter ordering is deliberate and worth knowing:

1. `exclude_paths` — checked first, so a path you never want traced is rejected
   before we pay for prefix matching;
2. `include_paths` — if non-empty the file *must* match;
3. `exclude`, then `include` — so `include` narrows last.

## Testing your own hook

Two rules, from `tests/conftest.py`:

**One tracer at a time.** `sys.settrace` is one slot per thread, and `HookLease`
enforces it. If a test leaks an active tracer, every later test sees a trace it
did not ask for. The autouse `clean_hooks` fixture is the safety net:

```python
@pytest.fixture(autouse=True)
def clean_hooks():
    saved = sys.gettrace()
    yield
    if sys.gettrace() is not saved:
        sys.settrace(saved)
```

**Write contracts, not coverage.** Each test should encode a rule whose
violation produces a *plausible but wrong* trace. Good ones from this suite:

```python
def test_generator_produces_multiple_call_events():
    """One Python-level call, but `call` fires on every resume."""

def test_attach_existing_cross_thread_without_arming_sees_nothing():
    """`attach_existing` succeeds and reports nothing. Pin the behaviour,
    not the wish -- the failure mode is silent."""

def test_siblings_tile_their_parent():
    rects, _ = layout(tree, width=1000)
    assert sum(r["w"] for r in children) == pytest.approx(by_label["b"]["w"])
```

## The checklist

- [ ] the local hook returns itself on every path
- [ ] every hook body is wrapped
- [ ] failures are counted and reportable
- [ ] frames are snapshot, never retained
- [ ] the filter is in the global hook
- [ ] the previous hook is restored, including `None`
- [ ] `f_trace` is detached before the global hook is cleared
- [ ] the tracer excludes its own package
- [ ] `buffer=False` unless a post-hoc renderer needs it
- [ ] `dropped`/`errors` are reported
- [ ] one clock read per event, `capture_cpu` off by default
- [ ] redaction happens at capture, not in the sink
- [ ] a test asserts the hook is uninstalled afterwards