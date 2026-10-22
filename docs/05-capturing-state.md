# 05 — Capturing state safely

A variable-capturing tracer is, by construction, a secret-exfiltration tool.
Everything it prints ends up in a log file, a CI artifact, or a bug report.
This chapter is about both halves: not crashing, and not leaking.

## What `f_locals` costs

```python
>>> import timeit
>>> n = 20000
>>> print(f"{timeit.timeit(lambda: caller_frame().f_locals, number=n) / n * 1e9:.0f} ns")
```

Two facts:

- **On Python < 3.13**, every read of `f_locals` builds a *new* dict by
  iterating every name in the frame. It is not a view; it is a materialisation.
- **On 3.13+** (PEP 667) it is a `FrameLocalsProxy`. Reading it is cheap, but
  *holding* it and reading later gives you values that changed after your event
  fired.

```python
dict(frame.f_locals)          # the correct snapshot call, every version
frame.clear()                 # 3.13+: release the locals deliberately
```

`capture_locals()` reads once and returns copies.

## Why module frames are the expensive case

For a `<module>` frame, `f_locals is f_globals` — so capturing module-level code
copies your program's entire namespace. And `co_varnames` is **empty** for a
module code object, so a tracer that iterates `co_varnames` sees nothing at all
and concludes there is no state to capture (G6).

Measured cost, from `examples/03` section C:

| configuration | events | locals captured | traced wall |
|---|---|---|---|
| locations only | 67 | 0 | 1.03 ms |
| + locals | 67 | 294 | **14.00 ms** |
| + stack snapshot | 67 | 0 | 8.00 ms |
| + args and returns | 67 | 0 | 1.06 ms |

## The seven hazards

All reproduced in `examples/13`.

| # | hazard | mechanism | guarded? |
|---|---|---|---|
| 1 | `__repr__` raises | catches `BaseException` | yes |
| 2 | `__repr__` recurses | depth budget between nested captures | yes |
| 3 | container contains itself | `reprlib` + identity-path set | yes |
| 4 | `__repr__` blocks on I/O | — | **no** |
| 5 | 5 MB string / `10**5000` / 50k list | four independent size limits | yes |
| 6 | `__repr__` mutates the object | — | no (unfixable) |
| 7 | a secret under an innocent name | name patterns + value patterns | mostly |

### 1. `__repr__` that raises

```python
>>> class Exploding:
...     def __repr__(self): raise ValueError("broken")
>>> safe_repr(Exploding())
'<... __repr__ raised ValueError: broken>'
```

`BaseException`, not `Exception`: a hostile `__repr__` raising `SystemExit`
would otherwise take the process down — and the process being taken down is the
one you were trying to debug.

Note the failure is **visible**. An earlier design returned `""`, which produced
traces that quietly lied about values. A broken `__repr__` should look broken in
your output.

### 2 and 3. Recursion, two mechanisms

```python
>>> safe_repr(Recursive())          # __repr__ recursing in *user* code
'<... RecursionError in repr (Recursive)>'
>>> x = []; x.append(x); safe_repr(x)
'[[[[[[[[...]]]]]]]'
>>> d = {}; d["self"] = d; safe_repr(d)
"{'self': {'self': {'self': {'self': {'self': {'self': {'self': {...}}}}}}}"
```

`reprlib.repr` handles self-referential containers via the recursive-ellipsis
protocol. But a type that does not implement that protocol — a hand-written
`__repr__`, or a third-party class — can still recurse, hence a second guard: an
identity set of containers on the current path.

The recursion budget is thread-local. A module-level global would make captures
on thread B interfere with thread A's depth guard, and make the guard itself
racy.

### 4. `__repr__` that blocks — the one you cannot fix

```python
>>> class Slow:
...     def __repr__(self): time.sleep(2.0); return "Slow(...)"
>>> t0 = time.perf_counter(); safe_repr(Slow(), policy=CapturePolicy(timeout_seconds=0.4))
' Slow(...)'
>>> time.perf_counter() - t0
2.0
```

The budget is checked *between* nested captures. It cannot interrupt a single
blocking `repr`: there is no way to time out a Python call from the same thread
without signals, and signals do not interrupt most blocking C calls.

The real defences, in order of practicality:

1. do not capture locals of code you do not control;
2. **allowlist types** — render `int/str/bytes/float/None` and containers *of
   those*; everything else becomes `<opaque Foo>`;
3. render in a subprocess with a timeout (correct, and absurd).

Option 2 is a policy decision, not a formatting one, which is why it belongs in
`CapturePolicy`.

### 5. Size bombs

```python
>>> for label, obj in (("5 MB string", "x"*5_000_000),
...                     ("10**5000", 10**5000),
...                     ("50k list", list(range(50_000)))):
...     print(f"{label:<14} -> {len(safe_repr(obj)):>5} chars")
5 MB string    ->  200 chars
10**5000       ->  151 chars
50k list       ->   23 chars
```

Five independent limits, because they bound different things:

| field | bounds | default |
|---|---|---|
| `max_string` | chars kept from a str/bytes | 200 |
| `max_items` | elements kept from a container | 12 |
| `max_depth` | nesting levels descended | 3 |
| `max_repr_chars` | the final string | 4000 |
| `max_total_per_event` | **all bindings of one event** | 20000 |

The last one is the one that actually bounds a module frame: thousands of
bindings, each individually small, still add up.

A subtlety: `capture_value(10 ** 5000)` has to call `str()` on the int to decide
whether it is "big", and `str()` on a huge int **raises** `ValueError` under
CPython's `int_max_str_digits` limit (4300 since 3.11). A `ValueError` inside a
hook is fatal (G2), so the conversion itself is guarded.

### 6. `__repr__` with a side effect

```python
>>> m = Mutating(); safe_repr(m)
'Mutating(calls=1)'
>>> m.calls
1
```

You cannot prevent this from Python. `repr` is arbitrary user code and CPython
calls it. What you *can* do is not pretend otherwise — and note the knock-on
effect: **any allocation your capture makes is attributed to your capture**, which
is why combining a tracer with `tracemalloc` inflates the measurement by ~65x
(`examples/16`).

## Redaction

Two layers, and the second is the one that earns its keep.

### Layer 1: the name

```python
>>> Redactor().name_is_sensitive("auth_token")
True
>>> Redactor().name_is_sensitive("token")
False
```

The subtlety is *specificity*. A pattern containing `token` matches `token`,
`tokens`, `max_tokens`, `n_tokens`, `token_id`, `tokenize`, and every NLP
function called `tokenize` — which makes your traces useless for exactly the
code you most want to read. Credentials are named `*_token` or `token_*`, so the
pattern requires an affix:

```python
re.compile(r"(^|_)(auth|access|refresh|id|api|bearer|session|csrf|xsrf)[_-]?token($|_)")
```

Redacted: `auth_token`, `access_token`, `api_token`, `bearer_token`,
`session_token`, `csrf_token`, `xsrf_token`, `refresh_token`, `client_secret`,
`password`, `passwd`, `pwd`, `secret`, `api_key`, `apikey`, `auth_key`,
`private_key`, `auth`, `session`, `cookie`, `signature`, `salt`, `nonce`,
`_secret`.

Not redacted: `token`, `tokens`, `max_tokens`, `n_tokens`, `token_id`,
`tokenize`, `rows`, `text`, `value`, `user`, `host`, `name`, `count`.

### Layer 2: the value

Regardless of the name it is stored under.

```python
>>> for value in ("AKIAIOSFODNN7EXAMPLE",
...               "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
...               "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SECRETSIG",
...               "postgres://user:hunter2@db.internal:5432/app",
...               "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
...               "sk-proj-abcdefghij1234567890"):
...     print(safe_repr(value, name="innocuous"))
'<redacted>'
```

Two lessons from writing these patterns:

- **Patterns must be maximal.** The first JWT pattern I wrote ended at the second
  dot, which left the signature in cleartext — and a signature you can verify
  offline. Consume the whole thing.
- **Connection strings carry credentials in the authority component**, and
  `DATABASE_URL` looks perfectly innocent. That is the entire argument for having
  a value layer.

### The most common real leak

```python
>>> safe_repr({"api_key": "sk-live-XXXXXXXXXXXX", "rows": [1, 2]})
"{'api_key': <redacted>, 'rows': [1, 2]}"
```

`reprlib.repr` renders a whole dict in one call, so a value under a sensitive
*key* cannot be redacted — the key name is never consulted. Dicts containing a
sensitive key therefore get a **per-entry renderer**.

That renderer then has a second bug to avoid: running the pattern pass again over
the *composed* string lets a greedy pattern like `token\s*=\s*\S+` match across a
boundary and eat the key as well as the value, turning
`{'api_key': 'sk-...'}` into `{<redacted>}`. So composed output is marked
pre-scrubbed and not re-scrubbed.

### Where redaction happens

In `capture_locals()`, **not** in the sink. A sink-level redactor is a redactor
you will forget when you write the new sink.

### What it does not buy

A secret stored as `cfg[7]` still leaks. What you get is "no accidental
plaintext credentials in CI artifacts", which is worth a great deal and is not the
same claim. State it accurately in your own tooling's docs.

## Structured capture

`safe_repr` is for humans. Sinks that need to *index* values want structure:

```python
>>> capture_value({"a": 1, "b": [1, 2]})
{'kind': 'mapping', 'type': 'dict', 'n': 2,
 'items': [["'a'", {'kind': 'int', 'value': 1}],
           ["'b'", {'kind': 'list', 'type': 'list', 'n': 2, 'items': [...]}]]}
```

The `kind` discriminator is the contract. Kinds: `null`, `bool`, `int`, `float`,
`complex`, `str`, `bytes`, `type`, `callable`, `exception`, `mapping`, `list`,
`tuple`, `set`, `iterable`, `object`, `depth-limit`, `recursive`.

Every value is JSON-serialisable — `bytes` become a length plus a head,
exceptions become type plus args, and so on.

## Strings must be quoted

```python
>>> safe_repr("abc"), safe_repr("")
("'abc'", "''")
>>> safe_repr({"a": 1}) == safe_repr({"a": "1"})
False
```

Rendering a bare string unquoted makes `{'a': 1}` indistinguishable from
`{'a': '1'}` and makes an empty string look like a missing value. That was a real
bug in an earlier version of this module.

## The policy object

```python
policy = CapturePolicy(
    max_string=200,          # chars from a str
    max_items=12,            # elements from a container
    max_depth=3,             # nesting levels
    max_repr_chars=4000,     # final string
    max_total_per_event=20000,
    timeout_seconds=0.25,    # soft: between nested captures only
    enable_garbage_cycle_guard=True,
)
Allowlist = policy.evolve(max_depth=1, max_items=4)
```

All frozen; `evolve()` returns a copy.

## Checklist for anything that touches values

- [ ] `BaseException` is caught, not `Exception`
- [ ] recursion is bounded by depth *and* by identity
- [ ] sizes are bounded on five independent axes
- [ ] strings are quoted
- [ ] `dict()`-style snapshotting, never a retained proxy
- [ ] redaction by name *and* by value, at capture time
- [ ] `str()` on huge ints is guarded against the digit limit
- [ ] the blocking-`repr` limitation is documented rather than implied fixed
- [ ] allocation attribution accounts for your own allocations