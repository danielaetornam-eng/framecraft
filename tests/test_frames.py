"""Frame introspection: the read-only half of the library.

These tests exist because every tracer bug eventually turns out to be a
misunderstanding of something in here.
"""

from __future__ import annotations

import gc
import itertools
import sys

import pytest

from framecraft import (
    caller_frame,
    capture_locals,
    current_frame,
    describe,
    frame_depth,
    is_suspended_frame,
    render_stack,
    resolve_name,
    scope_of,
    stack_snapshot,
    suspended_frames,
    walk,
)


def target(a, b=2, *rest, kw=None, **extra):
    local_only = a + b
    return local_only


def closure_factory(n):
    """Return ``inner``, which closes over ``n``."""

    def inner(x):
        return x + n

    return inner


def module_frame():
    """Return a live `<module>` frame.

    There is no shortcut: a module's frame exists only while its top-level code
    is running, and by the time a test can ask, the module has finished
    importing.  Walking ``f_back`` does not help either -- the bottom of the
    stack is the test runner, not this module.

    So we make one.  ``exec`` a module-level code object and grab its frame from
    a global trace hook, which is exactly what ``fc-trace inspect`` does.  It is
    the smallest honest demonstration of the two-level ``settrace`` protocol in
    the whole suite.
    """
    captured: list = []

    def want_it(frame, event, arg):
        if frame.f_code.co_name == "<module>" and not captured:
            captured.append(frame)
        return None  # decline: we only want the frame, not a trace

    source = "alpha = 1\nbeta = [alpha, alpha]\ngamma = 'x' * 100\n"
    code = compile(source, "<framecraft-test-module>", "exec")
    glb: dict = {}
    saved = sys.gettrace()
    sys.settrace(want_it)
    try:
        exec(code, glb)
    finally:
        sys.settrace(saved)
    assert captured, "failed to capture a module frame"
    return captured[0]


def parked_generator(limit=3):
    """A generator a test can park mid-iteration."""
    i = 0
    while i < limit:
        yield i
        i += 1


class TestNavigation:
    def test_caller_frame_is_the_caller(self):
        def outer():
            return caller_frame()

        fr = outer()
        assert fr.f_code.co_name == "outer"

    def test_caller_frame_skip(self):
        def a():
            return caller_frame(skip=0)

        def b():
            return a()

        assert b().f_code.co_name == "a"

    def test_current_frame_depth_zero_is_caller(self):
        fr = current_frame(0)
        assert fr.f_code.co_name == "current_frame"

    def test_walk_starts_at_the_innermost_real_frame(self):
        """walk() must start at ITS caller's caller, not at itself.

        `walk` calls `caller_frame`, so asking for depth 0 returns `walk`'s own
        frame and every depth in the stack comes out one too deep.  Silent, and
        it looks like plausible output.
        """
        def deepest():
            return list(walk())

        frames = deepest()
        assert frames[0].f_code.co_name == "deepest"
        assert frames[0].f_code.co_qualname.endswith("deepest")

    def test_walk_ends_at_the_bottom_cleanly(self):
        frames = list(walk())
        assert frames[-1].f_back is None
        # Under pytest the outermost frame is the runner, not <module>, so
        # assert on the *invariant* rather than the name.
        assert len(frames) > 2

    def test_walk_respects_max_depth(self):
        def a():
            def b():
                return list(walk(max_depth=2))

            return b()

        assert len(a()) == 2

    def test_frame_depth_counts_this_frame_up(self):
        assert frame_depth() >= 3  # _depth -> test -> pytest internals

    def test_stack_snapshot_depth_is_innermost_first(self):
        snaps = stack_snapshot()
        assert [s.depth for s in snaps] == list(range(len(snaps)))

    def test_snapshot_holds_no_frames(self):
        """The whole point: a StackFrame is a copy, not a live frame."""
        snaps = stack_snapshot()
        for s in snaps:
            assert not hasattr(s, "f_back")
            assert isinstance(s.filename, str)

    def test_render_stack_is_traceback_ordered(self):
        def inner():
            return render_stack()

        text = inner()
        assert 'File "' in text
        # Outermost first, exactly like traceback.print_stack. Reading it
        # top-down as call order is the single most common way to misread a
        # stack, so the order is worth asserting.
        assert text.index("test_render_stack_is_traceback_ordered") < text.index("inner")


class TestScopes:
    def test_local_scope(self):
        frame = caller_frame()
        assert scope_of(frame, "frame") == "local"

    def test_global_scope(self):
        frame = caller_frame()
        assert scope_of(frame, "sys") == "global"

    def test_builtin_scope(self):
        frame = caller_frame()
        assert scope_of(frame, "len") == "builtin"

    def test_unknown_scope(self):
        frame = caller_frame()
        assert scope_of(frame, "definitely_not_a_real_name_xyz") == "unknown"

    def test_free_variable_is_free_not_local(self):
        """`n` must be a *free* variable.

        That requires the compiler to see a reference to it inside a nested
        function.

        Passing `n` as a parameter does not make it free -- it is an ordinary
        local. That distinction is the whole point of co_cellvars/co_freevars.
        """
        holder = {}

        def make(n):
            def peek():
                if n is not None:  # forces n to become a cellvar of `make`
                    holder["f"] = caller_frame()

            peek()

        make(5)
        code = holder["f"].f_code
        # `n` is a *cellvar of make* and a *freevar of peek*. The frame we are
        # inspecting is peek's, so the name lives in co_freevars -- which is
        # exactly the distinction that makes scope_of() worth having.
        assert "n" in code.co_freevars
        assert "n" not in code.co_varnames
        assert scope_of(holder["f"], "n") == "free"

    def test_resolve_name_builtin(self):
        scope, value = resolve_name(caller_frame(), "len")
        assert scope == "builtin"
        assert value is len

    def test_resolve_name_free_variable(self):
        holder = {}

        def make(n):
            def peek():
                if n is not None:
                    holder["f"] = caller_frame()
                    holder["seen"] = n

            peek()

        make(7)
        assert holder["seen"] == 7
        scope, value = resolve_name(holder["f"], "n")
        assert scope == "free"
        assert value == 7


class TestCaptureLocals:
    def test_function_frame_yields_locals_only(self):
        """`f_locals` for a *function* frame contains locals, nothing else.

        This is the behaviour worth pinning down: a naive "capture f_locals"
        tracer on a function frame gives you the parameters and the local
        variables and *nothing else*. Globals and builtins are reachable through
        `f_globals`/`f_builtins`, not through `f_locals`, so a tool that claims
        to have captured a frame's whole namespace from `f_locals` alone is
        wrong.
        """
        local_int = 1          # noqa: F841 - present precisely to be captured
        local_list = [1, 2]    # noqa: F841
        frame = caller_frame()
        bindings = {b.name: b for b in capture_locals(frame)}
        assert bindings["local_int"].value == "1"
        assert bindings["local_int"].scope == "local"
        assert bindings["local_list"].scope == "local"
        assert "sys" not in bindings
        assert "len" not in bindings
        # ...and they are all real locals, i.e. in co_varnames.
        for name in bindings:
            assert name in frame.f_code.co_varnames

    def test_resolve_name_sees_globals_and_builtins(self):
        """`scope_of`/`resolve_name` go further than `f_locals` does."""
        assert scope_of(caller_frame(), "sys") == "global"
        assert scope_of(caller_frame(), "len") == "builtin"
        assert resolve_name(caller_frame(), "len")[1] is len

    def test_capture_is_capped(self):
        """Capturing must be bounded, not "return everything you can see"."""
        bindings = capture_locals(caller_frame())
        assert len(bindings) <= len(caller_frame().f_code.co_varnames)

    def test_structured_is_json_like(self):
        structured = {"a": 1, "b": [1, 2]}   # noqa: F841 - ditto
        bindings = {b.name: b for b in capture_locals(caller_frame())}
        assert bindings["structured"].structured["kind"] == "mapping"

    def test_only_filter(self):
        wanted = 1     # noqa: F841
        unwanted = 2   # noqa: F841
        names = {x.name for x in capture_locals(caller_frame(), only=["wanted"])}
        assert names == {"wanted"}

    def test_module_frame_is_huge(self):
        """Documents *why* capture is opt-in, in an assertion form.

        `co_varnames` for a module frame contains every global, so capturing
        locals of module-level code is a per-event copy of your program's entire
        namespace.
        """
        import json as _json  # noqa: F401  - imported so it is in the module globals

        mf = module_frame()
        assert mf.f_code.co_name == "<module>"
        # co_varnames is EMPTY for a module code object: module-level names are
        # globals, not fast locals. A tracer that iterates co_varnames alone sees
        # an empty module frame and concludes there is nothing to capture.
        assert mf.f_code.co_varnames == ()
        # ...and f_locals IS f_globals for a module frame, so the capture sees
        # every global.
        assert mf.f_locals is mf.f_globals
        bindings = capture_locals(mf, structured=False)
        names = {b.name for b in bindings}
        assert {"alpha", "beta", "gamma"} <= names
        assert all(b.scope == "global" for b in bindings if b.name != "__builtins__")


class TestSuspendedFrames:
    def test_generator_frame_is_suspended(self):
        g = parked_generator()
        next(g)
        assert is_suspended_frame(g.gi_frame)
        g.close()

    def test_normal_frame_is_not_suspended(self):
        assert not is_suspended_frame(caller_frame())

    def test_suspended_frames_finds_parked_generator(self):
        g = parked_generator()
        next(g)
        try:
            found = [f for f in suspended_frames()
                     if f.f_code.co_name == "parked_generator"]
            assert found, "parked generator not found by the gc sweep"
            assert is_suspended_frame(found[0])
        finally:
            g.close()

    def test_suspended_frames_excludes_running_frames(self):
        """A running frame is on a real stack; this is not a gc sweep."""
        running = caller_frame()
        assert running not in suspended_frames()

    def test_suspended_frames_excludes_finished_generators(self):
        g = parked_generator()
        next(g)
        g.close()
        gc.collect()
        assert not [f for f in suspended_frames() if f.f_code.co_name == "parked_generator"]


class TestDescribe:
    def test_shape(self):
        info = describe(caller_frame())
        for key in ("code", "instruction_pointer", "namespace_sizes",
                    "trace_hooks", "links", "closure", "exceptions", "locals"):
            assert key in info, f"describe() is missing {key!r}"

    def test_locals_are_in_varname_order(self):
        info = describe(caller_frame())
        slots = [r["slot"] for r in info["locals"]]
        assert slots == sorted(slots)

    def test_line_table_present_on_311_plus(self):
        info = describe(target.__code__ and caller_frame())
        assert "line_table" in info

    def test_exception_table_key_always_present(self):
        info = describe(caller_frame())
        assert "exception_table" in info  # possibly empty; the key is the contract

    def test_line_table_spans_are_ordered_and_non_overlapping(self):
        info = describe(caller_frame())
        spans = info["line_table"]
        assert spans, "co_lines() produced nothing"
        for (_s1, e1, _l1), (s2, _e2, _l2) in itertools.pairwise(spans):
            assert e1 <= s2, "line table spans must not overlap"


class TestCliInspectTarget:
    """`fc-trace inspect` parses `file.py`, `file.py:123`, `file.py::name`."""

    @pytest.mark.parametrize(
        "target,expect",
        [
            ("x.py", ("x.py", "")),
            ("x.py:12", ("x.py", "12")),
            ("x.py::func", ("x.py", "func")),
            ("x.py::Outer.method", ("x.py", "Outer.method")),
            ("C:/src/app.py:42", ("C:/src/app.py", "42")),
        ],
    )
    def test_parsing(self, target, expect):
        from framecraft.cli.main import _split_inspect_target, build_parser

        ns = build_parser().parse_args(["inspect", target])
        path, spec = _split_inspect_target(ns.target)
        assert (path, spec) == expect
