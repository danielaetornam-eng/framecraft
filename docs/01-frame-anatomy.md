# 01 — Frame anatomy

Everything a tracer does is a consequence of the data structures on this page.
If you skip it, every later chapter will have a moment where you think "that
cannot possibly be true" and you will be wrong.

## Code objects are the blueprint

A **code object** is immutable, compiled, and shared.

```python
def leaf(a, b=2):
    c = a + b
    return c * 2

code = leaf.__code__
code.co_name          # 'leaf'  -- a *string*, not the function
code.co_qualname      # 'leaf'  -- 'Outer.method' for methods (3.11+)
code.co_filename      # the path handed to compile()
code.co_firstlineno   # 1
code.co_varnames      # ('a', 'b', 'c')  -- in fast-local slot order
code.co_cellvars      # names this code turns into cells
code.co_freevars      # names read from enclosing scopes
code.co_names         # global/attribute names touched
code.co_consts        # literals, nested code objects
code.co_nlocals, code.co_stacksize, code.co_flags
code.co_code          # the bytecode, bytes
```

Two things catch people:

**`code.co_name` is a string.** Ten thousand calls to `leaf` share one code
object. This is the reason a tracer must never key its bookkeeping on the code
object: two concurrent activations of `leaf` are indistinguishable by code and
must be distinguished by frame.

**`co_qualname` exists only on 3.11+**, and `<lambda>` needs disambiguating
because every lambda in the program is called `<lambda>`:

```python
>>> sorted({f.__qualname__ for f in [x for x in (lambda: 0).__code__ for _ in "abc"]})
['<listcomp>.<lambda>']
```

`framecraft._internal.codeinfo.qualified_name()` handles this by appending the
line number for lambdas and a marker for module code.

## Frames are the activations

```python
>>> import sys
>>> def show():
...     f = sys._getframe(0)
...     print(f.f_code.co_name, f.f_lineno, sorted(f.f_locals))
...
>>> show()
show 2 ['f']
```

| group | attributes | writable? |
|---|---|---|
| identity | `f_code` | no |
| program counter | `f_lasti`, `f_lineno` | no (derived) |
| namespaces | `f_locals`, `f_globals`, `f_builtins` | `f_locals` is a proxy on 3.13+ |
| link | `f_back` | no |
| tracing controls | `f_trace`, `f_trace_lines`, `f_trace_opcodes` | yes |

Three facts that break naive assumptions:

**`f_locals` is expensive on Python < 3.13.** Every read builds a *new* dict by
iterating every name in the frame. On 3.13+ (PEP 667) it is a
`FrameLocalsProxy`; `dict(frame.f_locals)` is then the correct snapshot call,
and `frame.clear()` exists to release a frame's locals.

**`f_locals` for a *function* frame contains locals only.** Globals and builtins
are in `f_globals` and `f_builtins`. For a *module* frame, `f_locals is
f_globals` — so capturing a module frame means capturing your program's entire
namespace. And `co_varnames` is **empty** for a module code object, because
module-level names are never fast locals.

**You cannot set attributes on a frame.** It has no `__dict__`. Every trick
that appears to — `frame._traceback`, `frame.f_locals` round-trips — is a
read-only attribute.

## Namespaces and scope resolution

CPython resolves a name in a fixed order. `scope_of()` reimplements it exactly:

1. `co_varnames` → **local**
2. `co_cellvars` / `co_freevars` → **free**
3. `f_globals` → **global**
4. `builtins.__dict__` → **builtin**
5. otherwise **unknown**

```python
>>> from framecraft import scope_of, caller_frame
>>> def demo():
...     local_x = 1
...     frame = caller_frame()
...     return [(n, scope_of(frame, n)) for n in ("local_x", "sys", "len", "nope")]
...
>>> demo()
[('local_x', 'local'), ('sys', 'global'), ('len', 'builtin'), ('nope', 'unknown')]
```

The distinction matters for two reasons:

- A debugger that only looks in `f_globals` reports `len` as missing, forever.
- A capture that classifies `len` as "not found" is telling you something false.

Closure cells are the subtle case. CPython's
`PyFrame_FastToLocalsWithError` copies **both** cellvars and freevars into
`f_locals` under their own names, so they are reachable through `f_locals` even
though they are not in `co_varnames`:

```python
def make(n):
    def inner(x):
        return x + n        # `n` becomes a cellvar of make, a freevar of inner
    return inner

g = make(5)
assert "n" in g.__code__.co_freevars        # note: freevars, not cellvars
```

## The instruction pointer

`f_lasti` is a **byte offset** into `co_code`, not an instruction index. Since
3.11 the stream contains inline `CACHE` entries:

```
 idx  offset  opname
   0       0  RESUME
   1       2  LOAD_GLOBAL
   2      12  LOAD_FAST_BORROW      <- jump of 10 bytes = 4 CACHE slots
   3      14  CALL
```

Any code doing `instructions[frame.f_lasti]` is indexing a list by byte offset.
It works until it doesn't. Use `code.co_lines()` (a generator of
`(start, end, lineno)` spans) or `framecraft`'s cached `_OpcodeIndex`.

`f_lineno` is derived from `f_lasti` through that table, and there are offsets
with no line at all — `CACHE` slots and the synthetic return. `None` is a
legitimate answer; treating it as `0` produces phantom "line 0" entries.

## Walking the stack

```python
from framecraft import walk
for frame in walk():
    print(frame.f_code.co_qualname, frame.f_lineno)
```

`walk()` yields **innermost first** and stops cleanly at `f_back is None`. A
naive loop without the `None` check raises `AttributeError` at the bottom of the
stack — which is why the helper exists.

A trap worth knowing: `walk()` calls `caller_frame(1)`, not
`caller_frame(0)`. `caller_frame(0)` is `walk`'s *own* caller, which is
`walk`. Getting this wrong shifts every depth in the stack by one and it looks
like plausible output.

Read a rendered stack **bottom-up** like a traceback. Frame 0 is innermost and
is printed *last*. Reading top-down as "call order" is the most common way to
misread a stack.

## Frames that are not on any stack

A generator, coroutine or async-generator frame is real and has real locals, and
while suspended it is on *no* stack. It is reachable only through the object
that owns it:

```python
g = counter(10)
next(g)
g.gi_frame.f_locals      # {'limit': 10, 'n': 1, 'received': None}
```

| object type | accessor |
|---|---|
| generator | `gen.gi_frame` |
| coroutine | `coro.cr_frame` |
| async generator | `agen.ag_frame` |

Consequences:

- `sys._current_frames()` never lists a suspended coroutine, so **a sampling
  profiler can never attribute time to one**.
- `suspended_frames()` finds them via the garbage collector. The frames
  themselves are usually *not* GC-tracked, so you must collect the owner objects
  and read `gi_frame` — searching for frames directly silently finds almost
  nothing. Measured in `examples/10`.
- Reference counting frees a dropped coroutine immediately; the GC sweep only
  finds objects kept alive by a **reference cycle** or by a container whose
  handle you lost. This surprises people and is demonstrated honestly in
  `examples/10` section 5.
- A generator's `call` event fires on every **resume**, not once. So a call
  counter built on `call` events over-counts generators by exactly the
  consumption count.

## What you can and cannot find

| question | answer |
|---|---|
| which function is this | `f_code.co_qualname` |
| which line is it on | `f_lineno` |
| what are its parameters | `f_locals` ∩ `co_varnames[:argcount + kwonlyargcount]` |
| what did it return | only from a `return` hook's `arg` |
| what did it call | not directly — see `call_site` (ch. 02) |
| what is in its closure | `f_locals` ∩ `co_freevars` |
| how deep is it | `sum(1 for _ in walk(frame))`, or a tracer's counter |
| is it suspended | `co_flags & (CO_GENERATOR\|CO_COROUTINE\|CO_ASYNC_GENERATOR)` |
| can I write to it | `f_locals[...] = x` works, but only reliably on 3.13+ |

## The frame-size accounting, briefly

`co_nlocalsplus` (3.11+) is the size of the *fast-locals plus cells* array —
the single flat array the interpreter indexes. `co_stacksize` is the operand
stack. Both exist so CPython can allocate one block for a frame:

```
frame allocation ≈ (co_nlocalsplus + co_stacksize + 1) * sizeof(PyObject*)
```

`co_nlocalsplus` was removed again in later 3.x builds; `describe()` reports
whatever exists rather than assuming.

## Where to go next

- **02** — the four hooks, and what each can and cannot see.
- **03** — the trap catalogue; several of the traps above recur for hooks.