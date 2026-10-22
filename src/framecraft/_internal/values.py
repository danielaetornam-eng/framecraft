"""Turning live objects into values you can safely print, store and diff.

This is the single most important module in the library for anyone writing a
real tracer, because it is where the *dangerous* behaviour lives.

A tracer runs inside the traced program.  The moment your hook does
``repr(obj)`` it has re-entered arbitrary user code, at an arbitrary point in
its own lifecycle.  That user code can:

*   raise (``__repr__`` raising is legal, ancient, and common),
*   recurse infinitely (a container containing itself: ``x = []; x.append(x)``),
*   block forever (a property that does I/O),
*   mutate the thing you are inspecting (a property with a side effect),
*   allocate (so the tracemalloc attribution you were collecting attributes to
    your own hook).

So :func:`safe_repr` is not a convenience wrapper.  It is a boundary.  Every
value that leaves a trace event passes through it.

Design of the guard:

    reprlib.Repr          handles size, and recursion via recursive_ellipsis
    our recursion guard    handles the case reprlib cannot: user __repr__ that
                          itself calls back into safe_repr, and containers
                          whose *identity* graph is cyclic but whose __repr__
                          does not implement the recursive-ellipsis protocol
    thread-local re-entrancy  makes a nested call cheap and terminating
"""

from __future__ import annotations

import re
import reprlib
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CapturePolicy",
    "Redactor",
    "safe_repr",
    "capture_value",
    "DEFAULT_POLICY",
    "DEFAULT_REDACTOR",
]

# Sentinel for "we bailed out".  Never used as a value: it exists so that
# ``safe_repr`` has an unambiguous "I failed" return distinct from any repr a
# user object could produce.
_FAILED = "\x1b[framecraft:unreprable\x1b[0m"


class _BudgetExhausted(Exception):
    """Internal: signals that a nested capture blew its budget."""


@dataclass(frozen=True)
class CapturePolicy:
    """Knobs controlling how much of an object we are willing to materialise.

    Attributes
    ----------
    max_string:
        Characters kept from a ``str``/``bytes`` before eliding the middle.
    max_items:
        Elements kept from any sequence/mapping/set before eliding the tail.
    max_depth:
        How deep into nested containers we descend.  Depth 1 means "only the
        outer container, with each element ``repr``-ed by the *container's* own
        repr machinery".
    max_repr_chars:
        Hard cap on the final string.  This is the only limit that is
        unconditionally enforced, because it is checked on a plain ``str``.
    max_total_per_event:
        Combined character budget across all values captured for one event.
        Stops one frame with a 400-variable namespace from producing a 2 MB line.
    timeout_seconds:
        Wall-clock guard.  Note the honest caveat: this is checked *between*
        nested captures, so it cannot interrupt a single ``__repr__`` that
        blocks forever.  Killing that requires running the capture in a separate
        process -- see ``docs/10-performance-and-safeguards.md``.
    enable_garbage_cycle_guard:
        Track ``id()`` of containers along the current path and elide on revisit.
        This catches ``x = []; x.append(x)`` for containers whose ``__repr__``
        does not honour ``reprlib``'s recursion protocol.
    """

    max_string: int = 200
    max_items: int = 12
    max_depth: int = 3
    max_repr_chars: int = 4000
    max_total_per_event: int = 20_000
    timeout_seconds: float = 0.25
    enable_garbage_cycle_guard: bool = True

    def evolve(self, **kw: Any) -> CapturePolicy:
        """Return a copy with ``kw`` overridden (dataclasses are frozen)."""
        data = {
            "max_string": self.max_string,
            "max_items": self.max_items,
            "max_depth": self.max_depth,
            "max_repr_chars": self.max_repr_chars,
            "max_total_per_event": self.max_total_per_event,
            "timeout_seconds": self.timeout_seconds,
            "enable_garbage_cycle_guard": self.enable_garbage_cycle_guard,
        }
        data.update(kw)
        return CapturePolicy(**data)


DEFAULT_POLICY = CapturePolicy()


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


@dataclass
class Redactor:
    """Replaces values that look like secrets before they are written anywhere.

    This exists because a variable-capturing tracer is, by construction, a
    secret exfiltration tool.  Anything you print from a frame namespace is
    going to end up in a log file, a CI artifact, or a bug report.  Redaction
    is not a complete defence (a secret under an innocent name still leaks) but
    it is the difference between "no accidental plaintext passwords in artifacts"
    and "some".

    Matching is on *name* by default, with an optional value-level pattern pass
    for things like ``AKIA[0-9A-Z]{16}`` that appear under innocuous names.
    """

    name_patterns: tuple[re.Pattern[str], ...] = field(
        default_factory=lambda: (
            # `token` on its own is far too broad: it matches a text tokenizer's
            # `token` variable, `max_tokens`, `n_tokens`, `token_id`, and every
            # NLP function called `tokenize`. Redacting those makes traces
            # useless for exactly the code you most want to read. Credentials
            # are named `*_token` / `token_*`, so require an affix.
            re.compile(r"(^|_)(pass(word|wd)?|pwd|secret|credential)s?($|_)"),
            re.compile(r"(^|_)(api[_-]?key|apikey|auth[_-]?key|private[_-]?key)($|_)"),
            re.compile(r"(^|_)(auth|access|refresh|id|api|bearer|session|csrf|xsrf)"
                       r"[_-]?token($|_)"),
            re.compile(r"token[_-]?(value|secret)($|_)"),
            re.compile(r"(^|_)(auth|session|cookie)($|_)"),
            re.compile(r"(^|_)(signature|salt|nonce)($|_)"),
            re.compile(r"^_+$"),
        )
    )
    value_patterns: tuple[tuple[str, re.Pattern[str]], ...] = field(
        default_factory=lambda: (
            ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
            ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
            ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.=]{16,}")),
            # JWT: header.payload.signature.  The signature must be included --
            # a pattern that stops at the second dot leaves the signature in
            # cleartext, which is a signature you can verify offline.
            (
                "jwt",
                re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
            ),
            # Connection strings carry credentials in the authority component.
            # A name like DATABASE_URL or cfg looks perfectly innocent, which is
            # exactly why this layer exists.
            (
                "url_credentials",
                re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/]*@[^\s'\"]+", re.I),
            ),
            # Common key=value / key: value forms in config dumps and env output.
            (
                "inline_secret",
                re.compile(
                    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|"
                    r"client[_-]?secret)\b\s*[=:]\s*\S+"
                ),
            ),
            ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/\S+")),
            ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
        )
    )
    placeholder: str = "<redacted>"
    enabled: bool = True

    def name_is_sensitive(self, name: str) -> bool:
        if not self.enabled:
            return False
        return any(p.search(name) for p in self.name_patterns)

    def scrub(self, name: str, rendered: str) -> str:
        """Return ``rendered`` with sensitive substrings replaced."""
        if not self.enabled:
            return rendered
        out = rendered
        for _label, pat in self.value_patterns:
            out = pat.sub(self.placeholder, out)
        if len(out) > 4096:
            # A very large rendered value (e.g. a whole SQL dump) is expensive
            # to scan and almost always a case where per-value matching is
            # enough.  Truncate first, then scrub, to bound the work.
            out = out[:4096] + "...<truncated>"
        return out


DEFAULT_REDACTOR = Redactor()


# --------------------------------------------------------------------------
# re-entrancy + budget state
# --------------------------------------------------------------------------


@dataclass
class _CaptureState:
    depth: int = 0
    spent: int = 0
    path_ids: set[int] = field(default_factory=set)
    started: float = 0.0


class _State(threading.local):
    """Thread-local capture state.

    Tracers run on every thread.  A module-level global would make captures on
    thread B interfere with thread A's depth guard and, worse, make the guard
    itself racy.  ``threading.local`` is the correct (and only cheap) tool.
    """

    def __init__(self) -> None:
        self.state = _CaptureState()


_STATE = _State()


def _elide_middle(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    head = limit // 2
    tail = limit - head - 3
    return s[:head] + "..." + (s[-tail:] if tail > 0 else "")


def _has_sensitive_key(mapping: Any, redactor: Redactor, depth: int = 0) -> bool:
    """True if any key anywhere under ``mapping`` looks like a credential name.

    Recurses, because the interesting case is nested: ``config["db"]["password"]``
    is where credentials actually live, and a top-level-only check leaves that
    entirely exposed while appearing to work.  Bounded by ``depth`` so a deeply
    nested config cannot make the guard itself expensive -- and if it is that
    deep, the caller is better off with an allowlist.
    """
    if not redactor.enabled or depth > 4:
        return False
    try:
        items = mapping.items()
    except AttributeError:
        return False
    for k, v in items:
        if isinstance(k, str) and redactor.name_is_sensitive(k):
            return True
        if isinstance(v, dict) and _has_sensitive_key(v, redactor, depth + 1):
            return True
    return False


def _render_mapping(mapping: dict, policy: CapturePolicy, redactor: Redactor) -> str:
    """Render a dict, redacting values by their key names.

    Mirrors what ``repr`` produces (``{k: v, ...}``, with ``repr()`` on keys and
    values) so the output stays recognisable, but routes every value through the
    name-aware capture.
    """
    parts = []
    for i, (k, v) in enumerate(mapping.items()):
        if i >= policy.max_items:
            parts.append("...")
            break
        key_repr = _safe_repr(k, policy, redactor, "")
        if isinstance(k, str) and redactor.name_is_sensitive(k):
            parts.append(f"{key_repr}: {redactor.placeholder}")
        else:
            inner = _safe_repr(v, policy, redactor, str(k))
            parts.append(f"{key_repr}: {inner}")
    return "{" + ", ".join(parts) + "}"


def _safe_repr(
    obj: Any,
    policy: CapturePolicy,
    redactor: Redactor,
    name: str,
    scrub_result: bool = True,
) -> str:
    state = _STATE.state

    if state.depth > policy.max_depth:
        return f"<... depth limit reached ({type(obj).__name__})>"

    if policy.timeout_seconds and (state.started and time.perf_counter() - state.started > policy.timeout_seconds):
        raise _BudgetExhausted

    state.depth += 1
    oid = id(obj)
    is_container = isinstance(obj, (list, tuple, set, frozenset, dict))
    if policy.enable_garbage_cycle_guard and is_container:
        if oid in state.path_ids:
            state.depth -= 1
            return f"<... recursive {type(obj).__name__} ...>"
        state.path_ids.add(oid)
    pre_scrubbed = False
    try:
        if isinstance(obj, str):
            # Quoted, like repr().  Rendering a bare str unquoted makes
            # `{'a': 1}` indistinguishable from `{'a': '1'}` in the output, and
            # makes an empty string look like a missing value.
            raw: str = _elide_middle(repr(obj), policy.max_string + 2)
        elif isinstance(obj, (bytes, bytearray, memoryview)):
            b = bytes(obj)
            raw = f"<{type(obj).__name__} len={len(b)}> " + repr(b[: policy.max_string])
        elif isinstance(obj, dict) and redactor.enabled and _has_sensitive_key(obj, redactor):
            # `reprlib.repr` renders the whole dict in one call, so a value under
            # a sensitive *key* can never be redacted -- the key name is not
            # consulted.  `{"api_key": "sk-live-...", "rows": [1, 2, 3]}` is the
            # single most common way a secret ends up in a log, so this case gets
            # its own renderer that redacts per entry.
            raw = _render_mapping(obj, policy, redactor)
            pre_scrubbed = True
        elif is_container:
            raw = reprlib.repr(obj)
            raw = _elide_middle(raw, policy.max_repr_chars)
        else:
            # `repr()` may raise, may recurse, may be arbitrarily slow.  There
            # is no way to make that safe from inside the same thread, which is
            # exactly why `timeout_seconds` above is documented as a soft guard.
            raw = repr(obj)
            raw = _elide_middle(raw, policy.max_repr_chars)
    finally:
        state.depth -= 1
        if policy.enable_garbage_cycle_guard and is_container:
            state.path_ids.discard(oid)

    if redactor.enabled and redactor.name_is_sensitive(name):
        return redactor.placeholder
    if pre_scrubbed or not scrub_result:
        # Already scrubbed entry by entry by _render_mapping.  Running the
        # pattern pass again over the *composed* string lets a greedy pattern
        # such as `token\s*=\s*\S+` match across a boundary and eat the key as
        # well as the value, turning `{'api_key': 'sk-...'}` into `{<redacted>}`
        # -- unreadable, and it hides which entry was sensitive.
        return raw
    return redactor.scrub(name, raw)


def safe_repr(
    obj: Any,
    *,
    name: str = "",
    policy: CapturePolicy = DEFAULT_POLICY,
    redactor: Redactor = DEFAULT_REDACTOR,
) -> str:
    """Render ``obj`` defensively.

    Returns a string that is *always* safe to write to a log: it never raises,
    never recurses without bound, and never exceeds ``policy.max_repr_chars``
    by more than a small constant.

    The failure mode is deliberately visible.  A tracer that silently returns
    ``""`` when ``__repr__`` explodes produces a trace that lies to you.  We
    return a marked sentinel instead, so a broken ``__repr__`` shows up in your
    output as a broken ``__repr__``.
    """
    state = _STATE.state
    if state.depth == 0:
        state.started = time.perf_counter()
        state.spent = 0
        state.path_ids = set()
    try:
        out = _safe_repr(obj, policy, redactor, name)
    except _BudgetExhausted:
        out = f"<... time budget exceeded ({type(obj).__name__})>"
    except RecursionError:
        out = f"<... RecursionError in repr ({type(obj).__name__})>"
    except BaseException as exc:
        # Deliberately catching BaseException: a hostile or buggy __repr__ can
        # raise SystemExit or KeyboardInterrupt, and a tracer must never be the
        # reason a program dies.  We do not re-raise because losing one value is
        # always better than losing the whole trace.
        etype = type(exc).__name__
        out = f"<... __repr__ raised {etype}: {str(exc)[:120]}>"
    finally:
        if state.depth == 0:
            state.started = 0.0
    state.spent += len(out)
    if state.spent > policy.max_total_per_event:
        return f"<... event capture budget exhausted at {name!r}>"
    return out


# --------------------------------------------------------------------------
# Structured capture (for sinks that want real values, not just strings)
# --------------------------------------------------------------------------

_PRIMITIVES = (bool, int, float, complex, str, bytes, type(None))


def capture_value(
    obj: Any,
    *,
    name: str = "",
    policy: CapturePolicy = DEFAULT_POLICY,
    redactor: Redactor = DEFAULT_REDACTOR,
    depth: int = 0,
) -> Any:
    """Return a JSON-serialisable *structure* describing ``obj``.

    Unlike :func:`safe_repr` this preserves types where it can, which is what
    makes traces analysable by a program instead of by grep::

        {"kind": "dict", "n": 3, "items": [["a", {"kind": "int", "value": 1}], ...]}

    Primitives become ``{"kind": ..., "value": ...}``.  Everything else is
    summarised.  The ``kind`` discriminator is what the HTML report and the
    diff tool key off.
    """
    if depth > policy.max_depth:
        return {"kind": "depth-limit", "type": type(obj).__name__}

    if isinstance(obj, bool):
        return {"kind": "bool", "value": obj}
    if obj is None:
        return {"kind": "null"}
    if isinstance(obj, int):
        # Note: arbitrary-precision ints are a classic tracer memory bomb
        # (10**10**6).  We truncate the *literal*, not the value -- and we must
        # guard the conversion itself, because `str()` on a huge int raises
        # ValueError under CPython's int_max_str_digits limit (4300 by default
        # since 3.11).  A ValueError here would propagate out of a trace hook.
        try:
            digits = len(str(obj))
        except ValueError:
            return {"kind": "int", "value": "<exceeds int_max_str_digits>", "big": True}
        if digits > 40:
            return {"kind": "int", "value": _elide_middle(str(obj), 40), "big": True}
        return {"kind": "int", "value": obj}
    if isinstance(obj, float):
        return {"kind": "float", "value": obj}
    if isinstance(obj, complex):
        return {"kind": "complex", "value": [obj.real, obj.imag]}
    if isinstance(obj, str):
        if redactor.enabled and redactor.name_is_sensitive(name):
            return {"kind": "redacted"}
        return {"kind": "str", "value": _elide_middle(obj, policy.max_string)}
    if isinstance(obj, (bytes, bytearray)):
        b = bytes(obj)
        return {"kind": "bytes", "len": len(b), "head": repr(b[:32])}
    if isinstance(obj, type):
        return {"kind": "type", "value": f"{obj.__module__}.{obj.__qualname__}"}
    if callable(obj) and not isinstance(obj, type):
        # Functions and methods are extremely common in a namespace and are far
        # more usefully reported by name than as an opaque object with an id.
        name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
        if name:
            mod = getattr(obj, "__module__", "?")
            return {"kind": "callable", "value": f"{mod}.{name}"}
    if isinstance(obj, BaseException):
        return {
            "kind": "exception",
            "type": f"{type(obj).__module__}.{type(obj).__qualname__}",
            "args": [safe_repr(a, name=name, policy=policy, redactor=redactor) for a in obj.args][:4],
        }

    if isinstance(obj, Mapping):
        oid = id(obj)
        if policy.enable_garbage_cycle_guard and oid in _STATE.state.path_ids:
            return {"kind": "recursive", "type": type(obj).__name__}
        _STATE.state.path_ids.add(oid)
        try:
            items: list[Any] = []
            for i, (k, v) in enumerate(obj.items()):
                if i >= policy.max_items:
                    items.append(["...", f"<+{len(obj) - policy.max_items} more>"])
                    break
                items.append(
                    [
                        safe_repr(k, policy=policy, redactor=redactor),
                        capture_value(v, name=str(k), policy=policy, redactor=redactor, depth=depth + 1),
                    ]
                )
            return {"kind": "mapping", "type": type(obj).__name__, "n": len(obj), "items": items}
        finally:
            _STATE.state.path_ids.discard(oid)

    if isinstance(obj, (list, tuple, set, frozenset)):
        oid = id(obj)
        if policy.enable_garbage_cycle_guard and oid in _STATE.state.path_ids:
            return {"kind": "recursive", "type": type(obj).__name__}
        _STATE.state.path_ids.add(oid)
        try:
            seq = []
            for i, v in enumerate(obj):
                if i >= policy.max_items:
                    seq.append(f"<+{len(obj) - policy.max_items} more>")
                    break
                seq.append(capture_value(v, policy=policy, redactor=redactor, depth=depth + 1))
            return {
                "kind": "list" if isinstance(obj, list) else ("set" if isinstance(obj, (set, frozenset)) else "tuple"),
                "type": type(obj).__name__,
                "n": len(obj),
                "items": seq,
            }
        finally:
            _STATE.state.path_ids.discard(oid)

    if isinstance(obj, (Iterable,)) and not isinstance(obj, _PRIMITIVES):
        # Generators, iterators, files... describable but not enumerable safely.
        return {"kind": "iterable", "type": f"{type(obj).__module__}.{type(obj).__qualname__}"}

    # Opaque object: give identity + size class, which is usually enough to
    # know "this is a different DBConnection than last time".
    entry: dict[str, Any] = {
        "kind": "object",
        "type": f"{type(obj).__module__}.{type(obj).__qualname__}",
    }
    try:
        entry["id"] = id(obj)
        entry["repr"] = safe_repr(obj, name=name, policy=policy, redactor=redactor)
    except BaseException:
        pass
    return entry
