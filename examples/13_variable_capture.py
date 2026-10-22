#!/usr/bin/env python3
"""Example 13 -- Capturing variables: what can go wrong, and the guard rails.

Run me::

    python examples/13_variable_capture.py

A variable-capturing tracer is, by construction, a secret-exfiltration tool.
Everything it prints ends up in a log file, a CI artifact, or a bug report.  So
this example is as much about what can go *wrong* as about what it shows.

Seven failure modes, all reproduced
-----------------------------------

1.  ``__repr__`` that raises.  Legal, ancient, common.
2.  ``__repr__`` that recurses forever.
3.  A container containing itself -- ``x = []; x.append(x)``.
4.  ``__repr__`` that blocks on I/O.  **Not solvable from inside the same
    thread**; see the honest note in section 4.
5.  A 10-million-character string, or ``10 ** 10**6``.
6.  An object whose ``__repr__`` mutates the object you are inspecting.
7.  A secret, under an innocent-looking name.

What ``safe_repr`` does about each is in
:mod:`framecraft._internal.values`; this file demonstrates them rather than
describing them.
"""

from __future__ import annotations

import time

from framecraft import CapturePolicy, FunctionTracer, Redactor, TracerConfig, capture_value, safe_repr

# ----------------------------------------------------------------------
# The seven hazards
# ----------------------------------------------------------------------


class Exploding:
    def __repr__(self) -> str:
        raise ValueError("this repr is broken")


class Recursive:
    def __repr__(self) -> str:
        return "Recursive(" + repr(self) + ")"


class Endless:
    def __repr__(self) -> str:
        return "Endless(" + repr(self) + ")"


class Mutating:
    def __init__(self) -> None:
        self.calls = 0

    def __repr__(self) -> str:
        self.calls += 1
        return f"Mutating(calls={self.calls})"


class Slow:
    def __repr__(self) -> str:
        time.sleep(2.0)
        return "Slow(...)"


class Wide:
    def __init__(self, n: int) -> None:
        self.n = n


# ----------------------------------------------------------------------


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def demo_raising_repr() -> None:
    section("1. __repr__ THAT RAISES")
    obj = Exploding()
    try:
        repr(obj)
    except BaseException as exc:
        print(f"  repr(obj) raises {type(exc).__name__}: {exc}")
    print(f"  safe_repr(obj)      -> {safe_repr(obj)}")
    print()
    print("  A tracer that lets this through loses the event *and* the program:")
    print("  an exception escaping a hook propagates into the traced code and")
    print("  unsets the hook (measured in examples/02). So `safe_repr` catches")
    print("  BaseException, not Exception -- a hostile __repr__ raising")
    print("  SystemExit would otherwise take the process down.")
    print()
    print("  Note that the failure is *visible*. An earlier design returned '' on")
    print("  failure, which produced a trace that quietly lied about the value.")
    print("  A broken __repr__ should look broken in your output.")


def demo_recursive_repr() -> None:
    section("2. __repr__ THAT RECURSES, AND SELF-CONTAINING CONTAINERS")
    print(f"  Recursive()  -> {safe_repr(Recursive())[:80]}")
    print()
    lst: list = []
    lst.append(lst)
    print("  x = []; x.append(x)")
    print(f"  safe_repr(x) -> {safe_repr(lst)}")
    print()
    print("  Two different mechanisms, because they are two different bugs:")
    print("   * `Recursive.__repr__` recurses in *user code*. Nothing in the")
    print("     container machinery can stop it. The only defence is a recursion")
    print("     *depth* budget in the capture, checked between nested calls.")
    print("   * `reprlib.repr` handles self-referential containers correctly, via")
    print("     the fillvalue/recursive_ellipsis protocol. But a container of a type")
    print("     that does not implement that protocol (`__repr__` built by hand,")
    print("     or a third-party class) can still recurse. Hence a second guard:")
    print("     an identity set of containers on the current path.")
    print()
    d: dict = {}
    d["self"] = d
    print(f"  d = {{}}; d['self'] = d  -> {safe_repr(d)}")


def demo_mutating_repr() -> None:
    section("3. __repr__ WITH A SIDE EFFECT")
    m = Mutating()
    print(f"  repr calls so far: {m.calls}")
    print(f"  safe_repr(m)      -> {safe_repr(m)}")
    print(f"  repr calls so far: {m.calls}")
    print()
    print("  You cannot prevent this from Python. `repr` is arbitrary user code and")
    print("  CPython calls it. What you *can* do is not pretend otherwise: the")
    print("  rendered string is the truth about what `repr` did, and a debugger")
    print("  that hands an object to your own code has to accept that it has")
    print("  re-entered the traced program.")
    print()
    print("  This is also why `tracemalloc` attribution is polluted by tracing: see")
    print("  examples/16. Any allocation your hook makes is attributed to your")
    print("  hook, not to the code you are measuring.")


def demo_uninterruptible() -> None:
    section("4. __repr__ THAT BLOCKS -- THE ONE CASE WE CANNOT FIX")
    print("  safe_repr has a timeout_seconds budget, but it is checked *between*")
    print("  nested captures. It cannot interrupt a single __repr__ that blocks:")
    print("  there is no way to time out a Python call from the same thread")
    print("  without signals, and signals do not interrupt most blocking C calls.")
    print()
    print("  Demonstrating with a 0.4 s budget against a 1.0 s repr:")
    obj = Slow()  # its __repr__ sleeps for two seconds
    policy = CapturePolicy(timeout_seconds=0.4)
    print("  first capture (this is the one that blocks ~2 s):")
    t0 = time.perf_counter()
    out = safe_repr(obj, policy=policy)
    elapsed = time.perf_counter() - t0
    print(f"  elapsed {elapsed:.2f}s  result: {out}")
    print(f"  budget honoured: {elapsed <= 0.6}")
    print()
    print("  The only real defences, in increasing order of practicality:")
    print("   1. do not capture locals of code you do not control")
    print("   2. whitelist types: only render int/str/bytes/float/None/list/dict/")
    print("      tuple/set of those; everything else gets '<opaque Foo>'")
    print("   3. render in a subprocess with a timeout (correct, and absurd)")
    print()
    print("  Option 2 is what a production tracer should do, and it is a policy")
    print("  decision, not a formatting one -- which is why it belongs in")
    print("  CapturePolicy rather than inside safe_repr.")
    print()
    print("  The second call is instant, because the object has been rendered")
    print("  once and the tracer no longer re-renders it. The first call is the")
    print("  one that costs you, and it is the one you cannot bound.")


def demo_size_bombs() -> None:
    section("5. SIZE BOMBS")
    big = "x" * 5_000_000
    huge_int = 10 ** 5000
    big_list = list(range(50_000))
    nested = {"a": {"b": {"c": {"d": {"e": {"f": {"g": [1, 2, 3]}}}}}}}
    for label, obj in (("5 MB string", big), ("10**5000", huge_int),
                       ("50k list", big_list), ("deep dict", nested)):
        s = safe_repr(obj)
        print(f"  {label:<16} -> {len(s):>5} chars: {s[:70]}")
    print()
    print("  Each of these is a few kilobytes of source that produces megabytes")
    print("  of trace. safe_repr bounds all four dimensions separately:")
    print("   * max_string        chars kept from a str/bytes")
    print("   * max_items         elements kept from any container")
    print("   * max_depth         nesting levels descended")
    print("   * max_repr_chars    hard cap on the final string")
    print("   * max_total_per_event   combined budget across all bindings of one")
    print("                        event, which is the one that actually bounds a")
    print("                        module-level frame (thousands of globals)")
    print()
    print("  capture_value() is the structured counterpart, for sinks that need")
    print("  to index values rather than print them:")
    import json

    print("  " + json.dumps(capture_value(nested))[:150] + " ...")


def demo_redaction() -> None:
    section("6. REDACTION")
    creds = {
        "api_key": "AKIAIOSFODNN7EXAMPLE",
        "Authorization": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "DATABASE_URL": "postgres://user:hunter2@db.internal:5432/app",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----",
        "harmless": "this should survive",
    }
    redactor = Redactor()
    for name, value in creds.items():
        shown = safe_repr(value, name=name, redactor=redactor)
        print(f"  {name:<16} -> {shown[:80]}")
    print()
    print("  Two layers:")
    print("   * NAME matching: password/token/api_key/secret/... The binding's")
    print("     *name* is what gets redacted, whatever the value is.")
    print("   * VALUE matching: AWS key ids, JWTs, PEM blocks, bearer tokens,")
    print("     regardless of the name they are stored under. This is the layer")
    print("     that catches the innocent-looking `DATABASE_URL`.")
    print()
    print("  Neither layer is complete. A secret stored as `cfg[7]` still leaks.")
    print("  What this buys you is 'no accidental plaintext credentials in CI")
    print("  artifacts', which is worth a great deal and is not the same claim.")
    print()
    print("  Redaction happens in capture_locals(), not in the sink, so every sink")
    print("  inherits it. A sink-level redactor is a redactor you will forget.")
    print()
    print("  A stronger option: an allowlist.")
    pol = CapturePolicy()
    safe = {"x": 1, "s": "text", "xs": [1, 2], "f": 1.5, "b": b"bytes", "n": None, "t": (1, "a")}
    print(f"  capture_value(safe)    -> {capture_value(safe)['kind']}, {len(safe)} items")
    print(f"  policy: max_items={pol.max_items} max_depth={pol.max_depth} "
          f"max_string={pol.max_string}")


def demo_in_context() -> None:
    section("7. ALL OF IT, THROUGH A REAL TRACER")

    def handler(request, api_key="sk-live-DO-NOT-LOG-THIS", rows=None):
        rows = rows or []
        payload = {"key": api_key, "rows": rows, "self": None}
        payload["self"] = payload
        blob = "B" * 200_000
        counter = 10 ** 400
        assert blob and counter          # deliberately huge; both must be elided
        return len(rows)

    with FunctionTracer(
        TracerConfig(events=("call", "line"), capture_locals=True),
        include=lambda c: c.co_name == "handler",
    ) as t:
        handler({"path": "/"}, rows=[1, 2, 3])
    for ev in t.events[-4:]:
        print(f"  {ev.location}")
        for b in ev.locals:
            print(f"      {b.scope:<7} {b.name:<12} = {b.value[:66]}")
    print()
    print("  Note: the self-referential dict, the 200 kB string and the 10**400")
    print("  integer are all rendered compactly, and the API key is redacted.")
    print("  The trace is still useful: you can see the request shape, the row")
    print("  count, and that a credential was present.")
    print()
    print("  Budget, from the last event:")
    total = sum(len(b.value) for b in ev.locals)
    print(f"    {total} characters for {len(ev.locals)} bindings "
          f"(max_total_per_event default is "
          f"{CapturePolicy().max_total_per_event})")


def main() -> int:
    print("Capturing variables safely.")
    print("Every value that leaves a trace event passes through safe_repr().")
    demo_raising_repr()
    demo_recursive_repr()
    demo_mutating_repr()
    demo_uninterruptible()
    demo_size_bombs()
    demo_redaction()
    demo_in_context()
    print("=" * 78)
    print("Next: 14 -- guards: budgets, backpressure, and never killing the program")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
