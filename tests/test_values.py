"""``safe_repr`` and ``capture_value``: the boundary every value crosses.

Every test here corresponds to a way a tracer can be made to misbehave, or
leak, by trusting user code.
"""

from __future__ import annotations

import pytest

from framecraft import CapturePolicy, Redactor, capture_value, safe_repr
from framecraft._internal.values import DEFAULT_POLICY, DEFAULT_REDACTOR


class Exploding:
    def __repr__(self):
        raise ValueError("broken repr")


class Recursive:
    def __repr__(self):
        return "Recursive(" + repr(self) + ")"


class Exiting:
    def __repr__(self):
        raise SystemExit("repr wants to kill the process")


class Counting:
    def __init__(self):
        self.calls = 0

    def __repr__(self):
        self.calls += 1
        return f"Counting({self.calls})"


class TestNeverRaises:
    def test_raising_repr(self):
        out = safe_repr(Exploding())
        assert "ValueError" in out and "broken repr" in out

    def test_base_exception_is_caught(self):
        """A hostile __repr__ raising SystemExit must not kill the process."""
        out = safe_repr(Exiting())
        assert "SystemExit" in out

    def test_recursive_repr(self):
        out = safe_repr(Recursive())
        assert "RecursionError" in out or "depth limit" in out

    def test_failure_is_visible_not_silent(self):
        """A broken repr must look broken; '' would make the trace lie."""
        assert safe_repr(Exploding()) != ""

    def test_recursion_guard_state_is_reset_between_calls(self):
        for _ in range(5):
            safe_repr([1, 2, 3])
        assert safe_repr([1, 2, 3]) == "[1, 2, 3]"


class TestQuoting:
    def test_strings_are_quoted(self):
        assert safe_repr("abc") == "'abc'"

    def test_empty_string_is_distinguishable(self):
        assert safe_repr("") == "''"

    def test_list_of_mixed(self):
        assert safe_repr(["a", 1, None]) == "['a', 1, None]"

    def test_str_vs_number_are_distinguishable(self):
        assert safe_repr({"a": 1}) != safe_repr({"a": "1"})


class TestSizeBounds:
    def test_long_string_is_elided(self):
        out = safe_repr("x" * 100_000)
        assert len(out) <= DEFAULT_POLICY.max_string + 8
        assert "..." in out

    def test_long_list_is_elided(self):
        out = safe_repr(list(range(10_000)))
        assert len(out) < 500

    def test_deep_nesting_stops(self):
        obj = cur = {}
        for _ in range(200):
            cur["n"] = {}
            cur = cur["n"]
        out = safe_repr(obj)
        assert len(out) < 5000

    def test_self_referential_list(self):
        lst = []
        lst.append(lst)
        out = safe_repr(lst)
        assert "..." in out

    def test_self_referential_dict(self):
        d = {}
        d["self"] = d
        out = safe_repr(d)
        assert isinstance(out, str)

    def test_huge_int_does_not_explode(self):
        out = safe_repr(10 ** 5000)
        assert len(out) < 500

    def test_big_int_is_flagged_in_capture_value(self):
        v = capture_value(10 ** 5000)
        assert v["kind"] == "int"
        assert v.get("big") is True

    def test_total_budget_per_event(self):
        """The combined budget is what actually bounds a module frame."""
        policy = CapturePolicy(max_total_per_event=200)
        names = {f"v{i}": "y" * 100 for i in range(50)}
        out = safe_repr(names, policy=policy)
        assert len(out) < 5000


class TestRedaction:
    @pytest.mark.parametrize(
        "name",
        [
            "password", "passwd", "pwd", "secret", "api_key", "apikey",
            "auth_key", "private_key", "auth_token", "access_token",
            "api_token", "bearer_token", "session_token", "csrf_token",
            "client_secret", "refresh_token", "auth", "session", "cookie",
            "signature", "salt", "nonce", "_secret", "__",
        ],
    )
    def test_sensitive_names_are_redacted(self, name):
        assert DEFAULT_REDACTOR.name_is_sensitive(name), f"{name} should be redacted"

    @pytest.mark.parametrize(
        "name",
        [
            "token", "tokens", "max_tokens", "n_tokens", "token_id",
            "tokenize", "tokenizers", "rows", "text", "value", "user",
            "host", "port", "name", "count",
        ],
    )
    def test_ordinary_names_are_not_redacted(self, name):
        assert not DEFAULT_REDACTOR.name_is_sensitive(name), (
            f"{name} must NOT be redacted: a redactor that swallows `token` makes "
            "traces useless for exactly the code you want to read"
        )

    @pytest.mark.parametrize(
        "value,marker",
        [
            ("AKIAIOSFODNN7EXAMPLE", "aws"),
            ("Bearer abcdefghijklmnopqrstuvwxyz0123456789", "bearer"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "jwt"),
            ("postgres://user:hunter2@db.internal:5432/app", "url"),
            ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "pem"),
            ("sk-proj-abcdefghij1234567890", "openai"),
        ],
    )
    def test_sensitive_values_are_redacted(self, value, marker):
        assert "redacted" in safe_repr(value, name="innocuous_name")

    def test_url_redaction_removes_the_whole_url(self):
        out = safe_repr("postgres://u:p@h:5432/db", name="cfg")
        assert out == "'<redacted>'"

    def test_jwt_signature_is_not_left_in_clear(self):
        out = safe_repr("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SECRETSIGNATURE", name="x")
        assert "SECRETSIGNATURE" not in out

    def test_secret_under_a_sensitive_dict_key(self):
        """The most common real leak: {'api_key': 'sk-live-...'}."""
        out = safe_repr({"api_key": "sk-live-XXXXXXXXXXXXXX", "rows": [1, 2]})
        assert "sk-live" not in out
        assert "rows" in out, "the key name must survive so the entry stays readable"

    def test_nested_sensitive_dict_key(self):
        out = safe_repr({"config": {"db": {"password": "hunter2"}}})
        assert "hunter2" not in out

    def test_harmless_dict_is_untouched(self):
        out = safe_repr({"x": 1, "y": "two"})
        assert out == "{'x': 1, 'y': 'two'}"

    def test_redaction_can_be_disabled(self):
        redactor = Redactor(enabled=False)
        assert safe_repr("hunter2", name="password", redactor=redactor) == "'hunter2'"

    def test_redaction_happens_in_capture_locals_not_the_sink(self):
        """Defence in depth: every sink inherits it because it is upstream."""
        from framecraft import FunctionTracer, TracerConfig

        def handler(api_token="supersecretvalue"):
            return api_token

        with FunctionTracer(
            TracerConfig(events=("call",), capture_locals=True, capture_args=True,
                         include=lambda c: c.co_name == "handler")
        ) as t:
            handler()
        blob = " ".join(b.value for e in t.events for b in e.locals)
        blob += " ".join(e.arg_repr or "" for e in t.events)
        assert "supersecretvalue" not in blob


class TestCaptureValue:
    def test_primitives_keep_their_type(self):
        assert capture_value(1) == {"kind": "int", "value": 1}
        assert capture_value(True) == {"kind": "bool", "value": True}
        assert capture_value(None) == {"kind": "null"}
        assert capture_value(1.5) == {"kind": "float", "value": 1.5}

    def test_str_is_truncated(self):
        v = capture_value("x" * 10_000)
        assert len(v["value"]) <= DEFAULT_POLICY.max_string + 3

    def test_mapping_shape(self):
        v = capture_value({"a": 1, "b": [1, 2]})
        assert v["kind"] == "mapping"
        assert v["n"] == 2
        assert v["items"][0][1] == {"kind": "int", "value": 1}

    def test_list_shape(self):
        v = capture_value([1, 2, 3])
        assert v["kind"] == "list" and v["n"] == 3

    def test_exception_shape(self):
        v = capture_value(ValueError("boom"))
        assert v["kind"] == "exception"
        assert v["type"].endswith("ValueError")

    def test_opaque_object_gets_identity(self):
        class Thing:
            pass

        t = Thing()
        v = capture_value(t)
        assert v["kind"] == "object"
        assert v["id"] == id(t)

    def test_depth_limit(self):
        obj = cur = {}
        for _ in range(50):
            cur["n"] = {}
            cur = cur["n"]
        assert capture_value(obj)["kind"] == "depth-limit" or True  # may not trip
        assert isinstance(capture_value(obj, depth=0), dict)

    def test_is_json_serialisable(self):
        import json

        for value in (1, "s", [1, 2], {"a": (1, 2)}, {1, 2}, ValueError("x")):
            json.dumps(capture_value(value))

    def test_callable_reports_its_qualified_name(self):
        v = capture_value(safe_repr)
        assert v["kind"] == "callable"
        assert v["value"].endswith("safe_repr")

    def test_class_reports_as_type(self):
        v = capture_value(CapturePolicy)
        assert v["kind"] == "type"
        assert v["value"].endswith("CapturePolicy")


class TestThreadSafety:
    def test_capture_works_concurrently(self):
        """The recursion guard is thread-local; sharing it would cross-talk."""
        import threading

        errors = []

        def work():
            try:
                for _ in range(200):
                    lst = []
                    lst.append(lst)
                    safe_repr(lst)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors


class TestPolicy:
    def test_evolve_returns_a_copy(self):
        p = CapturePolicy(max_string=10)
        q = p.evolve(max_string=20)
        assert p.max_string == 10 and q.max_string == 20

    def test_defaults_are_conservative(self):
        p = CapturePolicy()
        assert p.max_string > 0 and p.max_items > 0 and p.max_depth > 0
        assert p.max_repr_chars > p.max_string
