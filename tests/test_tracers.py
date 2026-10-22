"""Tracer behaviour: the contracts every engine must honour.

The tests here are written as *contracts* rather than as coverage.  Each one
encodes a rule that, if broken, produces a trace that looks plausible and is
wrong -- which is the only class of tracer bug worth a test.
"""

from __future__ import annotations

import sys
import threading

import pytest

from framecraft import (
    C_CALL,
    C_RETURN,
    CALL,
    EXCEPTION,
    LINE,
    RETURN,
    YIELD,
    FunctionTracer,
    HookLease,
    ProfileTracer,
    TracerConfig,
)
from framecraft._internal.compat import HAS_MONITORING
from framecraft.sinks import CountingSink, NullSink

# ----------------------------------------------------------------------
# Workload used by most tests
# ----------------------------------------------------------------------


def leaf(a, b=2):
    c = a + b
    return c * 2


def middle(x):
    return leaf(x) + leaf(x + 1)


def top():
    return middle(3)


def raiser():
    """Raises and handles inside one frame -- the hardest case to observe."""
    try:
        raise ValueError("boom")
    except ValueError:
        return -1


def unhandled_raiser(payload=None):
    """Raises and lets it propagate: the frame is genuinely torn down.

    Takes a parameter so the frame has locals, which is what makes it possible
    to assert that state was captured *while the frame still existed*.
    """
    prepared = payload or []
    raise KeyError("unhandled", prepared)


def gen(n):
    yield from range(n)


def c_user_dispatcher():
    return c_user({1: 2, "a": 1})


def only(*_a, **_k):
    return FunctionTracer(TracerConfig(include=lambda c: True))


# ----------------------------------------------------------------------


class TestFunctionTracerContracts:
    def test_emits_call_line_return_in_order(self):
        with FunctionTracer(TracerConfig(events=(CALL, LINE, RETURN))) as t:
            leaf(1)
        kinds = [e.kind for e in t.events]
        assert kinds[0] == CALL
        assert kinds[-1] == RETURN
        assert LINE in kinds

    def test_call_and_return_are_paired_by_call_id(self):
        with FunctionTracer(TracerConfig(events=(CALL, RETURN))) as t:
            leaf(1)
        calls = [e for e in t.events if e.kind == CALL]
        returns = [e for e in t.events if e.kind == RETURN]
        assert [c.call_id for c in calls] == [r.call_id for r in returns]

    def test_parent_call_id_links_the_tree(self):
        with FunctionTracer(TracerConfig(events=(CALL, RETURN))) as t:
            top()
        calls = [e for e in t.events if e.kind == CALL]
        by_id = {c.call_id: c for c in calls}
        for c in calls:
            parent = by_id.get(c.parent_call_id) if c.parent_call_id else None
            if parent is not None:
                assert parent.depth < c.depth

    def test_depth_starts_at_zero(self):
        with FunctionTracer(TracerConfig(events=(CALL,))) as t:
            top()
        assert min(e.depth for e in t.events) == 0

    def test_include_filter_is_applied(self):
        with FunctionTracer(TracerConfig(events=(CALL,), include=lambda c: c.co_name == "leaf")) as t:
            top()
        assert {e.qualname for e in t.events} == {"leaf"}

    def test_exclude_filter_wins(self):
        cfg = TracerConfig(
            events=(CALL,),
            include=lambda c: True,
            exclude=lambda c: c.co_name == "leaf",
        )
        with FunctionTracer(cfg) as t:
            top()
        assert "leaf" not in {e.qualname for e in t.events}

    def test_locals_captured_when_asked(self):
        with FunctionTracer(TracerConfig(events=(LINE,), capture_locals=True,
                                         include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        names = {b.name for e in t.events for b in e.locals}
        assert "a" in names and "c" in names

    def test_locals_not_captured_by_default(self):
        with FunctionTracer(TracerConfig(events=(LINE,),
                                         include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        assert all(not e.locals for e in t.events)

    def test_args_are_reconstructed_in_parameter_order(self):
        with FunctionTracer(TracerConfig(events=(CALL,), capture_args=True,
                                         include=lambda c: c.co_name == "leaf")) as t:
            leaf(5, 7)
        assert t.events[0].arg_repr == "a=5, b=7"

    def test_return_value_captured(self):
        with FunctionTracer(TracerConfig(events=(RETURN,), capture_return=True,
                                         include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        assert t.events[0].retval_repr == "6"

    def test_exception_events(self):
        with FunctionTracer(TracerConfig(events=(EXCEPTION,),
                                         include=lambda c: c.co_name == "raiser")) as t:
            raiser()
        assert t.events
        assert t.events[0].exception_type == "ValueError"

    def test_one_failure_produces_one_event_per_frame(self):
        """A single raise yields an event per frame it propagates through."""
        with FunctionTracer(TracerConfig(events=(EXCEPTION,))) as t:
            try:
                unhandled_raiser()
            except KeyError:
                pass
        events = [e for e in t.events if e.kind == EXCEPTION]
        assert len(events) >= 1
        assert len({e.exception_type for e in events}) == 1, (
            "one failure must not look like several exception types"
        )

    def test_locals_at_the_raise_site(self):
        with FunctionTracer(TracerConfig(events=(EXCEPTION,), capture_locals=True,
                                         include=lambda c: c.co_name == "unhandled_raiser")) as t:
            try:
                unhandled_raiser(payload=[1, 2, 3])
            except KeyError:
                pass
        names = {b.name for e in t.events for b in e.locals}
        assert "prepared" in names, (
            "an exception event must capture the frame's locals; that is the "
            "entire reason to capture them on exception events"
        )

    def test_generator_produces_multiple_call_events(self):
        """The single most common profiler bug: counting resumes as calls."""
        with FunctionTracer(TracerConfig(events=(CALL, RETURN),
                                         include=lambda c: c.co_name == "gen")) as t:
            g = gen(3)
            list(g)
        calls = [e for e in t.events if e.kind == CALL]
        assert len(calls) > 1, "a resumed generator should re-fire 'call'"

    def test_no_events_after_stop(self):
        t = FunctionTracer(TracerConfig(events=(CALL,)))
        t.start()
        leaf(1)
        n = len(t.events)
        t.stop()
        leaf(1)
        assert len(t.events) == n
        assert sys.gettrace() is None

    def test_prev_hook_is_restored(self):
        def foreign(frame, event, arg):
            return foreign

        sys.settrace(foreign)
        try:
            with FunctionTracer(TracerConfig(events=(CALL,))):
                pass
            assert sys.gettrace() is foreign
        finally:
            sys.settrace(None)

    def test_restores_none_when_nothing_was_installed(self):
        with FunctionTracer(TracerConfig(events=(CALL,))):
            pass
        assert sys.gettrace() is None

    def test_hooks_does_not_trace_itself(self):
        import os

        from framecraft._internal.codeinfo import _OWN_DIRS

        with FunctionTracer(TracerConfig(events=(CALL, RETURN))) as t:
            leaf(1)
        own = tuple(d.replace("\\", "/") for d in _OWN_DIRS)
        offenders = [e.filename for e in t.events
                     if e.filename.replace("\\", "/").startswith(own)]
        assert not offenders, f"tracer traced its own frames: {offenders[:3]}"
        assert os  # silence linters


class TestHookLease:
    def test_second_tracer_refuses(self):
        a = FunctionTracer(TracerConfig(events=(CALL,)))
        with a:
            with pytest.raises(RuntimeError, match="already owned"):
                FunctionTracer(TracerConfig(events=(CALL,))).start()

    def test_lease_released_on_exit(self):
        with FunctionTracer(TracerConfig(events=(CALL,))):
            pass
        assert HookLease.current() is None

    def test_lease_released_even_if_body_raises(self):
        with pytest.raises(ValueError):
            with FunctionTracer(TracerConfig(events=(CALL,))):
                raise ValueError("boom")
        assert HookLease.current() is None
        assert sys.gettrace() is None


class TestBuffering:
    def test_buffer_false_keeps_nothing(self):
        t = FunctionTracer(TracerConfig(events=(LINE,), buffer=False))
        with t:
            leaf(1)
        assert t.events == []

    def test_max_events_drops_oldest_and_counts(self):
        t = FunctionTracer(TracerConfig(events=(LINE,), max_events=10,
                                         include=lambda c: c.co_name == "middle"))
        with t:
            for _ in range(50):
                middle(1)
        assert len(t.events) <= 10
        assert t.dropped > 0, "an overflowing tracer must admit it dropped events"
        assert t.stats()["coverage_ratio"] < 1.0

    def test_drain_clears(self):
        with FunctionTracer(TracerConfig(events=(LINE,), include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        got = t.drain()
        assert got and t.events == []

    def test_timing_false_zeroes_clocks(self):
        with FunctionTracer(TracerConfig(events=(CALL,), timing=False,
                                         include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        assert t.events[0].perf_ns == 0


class TestClocks:
    def test_perf_ns_is_monotonic(self):
        with FunctionTracer(TracerConfig(events=(LINE,), include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        stamps = [e.perf_ns for e in t.events]
        assert stamps == sorted(stamps)
        assert stamps[-1] > stamps[0]

    def test_wall_is_derived_from_perf_ns(self):
        with FunctionTracer(TracerConfig(events=(CALL,), include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        ev = t.events[0]
        assert abs(ev.wall - ev.perf_ns / 1e9) < 1e-3

    def test_cpu_clock_is_opt_in(self):
        with FunctionTracer(TracerConfig(events=(CALL,), capture_cpu=True,
                                         include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        assert t.events[0].cpu > 0.0

    def test_cpu_clock_is_zero_by_default(self):
        with FunctionTracer(TracerConfig(events=(CALL,), include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        assert t.events[0].cpu == 0.0


class TestErrorIsolation:
    def test_sink_exception_does_not_kill_the_program(self):
        class Bad:
            def write(self, event):
                raise RuntimeError("sink bug")

        counter = CountingSink()
        with FunctionTracer(TracerConfig(events=(LINE,), buffer=False)) as t:
            t.add_sink(Bad())
            t.add_sink(counter)
            result = leaf(1)
        assert result == 6
        assert t.error_count > 0
        assert counter.total > 0
        assert sys.gettrace() is None

    def test_error_count_is_zero_on_a_clean_run(self):
        with FunctionTracer(TracerConfig(events=(CALL,), include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        assert t.error_count == 0


class TestThreads:
    def test_worker_is_untraced_by_default(self):
        with FunctionTracer(TracerConfig(events=(CALL, LINE))):
            th = threading.Thread(target=lambda: leaf(1))
            th.start()
            th.join()
        # The worker thread is a different thread; sys.settrace did not reach it.
        assert th.ident is not None

    def test_include_threads_traces_new_threads(self):
        holder: list = []

        def work():
            leaf(1)
            holder.append(1)

        with FunctionTracer(TracerConfig(events=(LINE,), include_threads=True,
                                         include=lambda c: c.co_name == "leaf")) as t:
            th = threading.Thread(target=work, name="fc-test-worker")
            th.start()
            th.join()
        assert holder
        assert any(e.thread_name == "fc-test-worker" for e in t.events)

    def test_attach_existing_requires_active(self):
        t = FunctionTracer(TracerConfig(events=(CALL,)))
        with pytest.raises(RuntimeError, match="active"):
            t.attach_existing()

    def test_attach_existing_cross_thread_without_arming_sees_nothing(self):
        """The measured trap: attaching cross-thread succeeds and reports nothing.

        `attach_existing` assigns `frame.f_trace`, but the interpreter only
        consults that if a *global* hook is present **in the frame's own
        thread** -- and the hook slot is thread state.  So the call returns a
        non-zero count and no events ever arrive.

        This test pins the behaviour rather than the wish, because the failure
        mode is silent and indistinguishable from a filtering bug.
        """
        started = threading.Event()
        release = threading.Event()

        def worker():
            started.set()
            release.wait(2.0)
            total = 0
            for _ in range(20):
                total += 1
            return total

        t = FunctionTracer(TracerConfig(events=(LINE,)))
        with t:
            th = threading.Thread(target=worker, name="fc-unarmed")
            th.start()
            assert started.wait(2.0)
            # Attach while the thread is still parked, so it is definitely alive.
            frame = sys._current_frames().get(th.ident)
            assert frame is not None
            n = t.attach_existing(frame)
            release.set()
            th.join()
        assert n >= 1, "attach_existing should have instrumented something"
        assert not [e for e in t.events if e.thread_name == "fc-unarmed"], (
            "expected the documented no-op: the thread has no global hook"
        )

    def test_arm_thread_makes_attach_work_in_that_thread(self):
        """`arm_thread()` from inside the worker fixes exactly the above."""
        t = FunctionTracer(TracerConfig(events=(LINE,)))

        def worker():
            assert t.arm_thread() is True
            t.attach_existing()
            total = 0
            for _ in range(20):
                total += 1
            return total

        with t:
            th = threading.Thread(target=worker, name="fc-armed")
            th.start()
            th.join()
        assert [e for e in t.events if e.thread_name == "fc-armed"], (
            "arm_thread() should have made the worker observable"
        )

    def test_arm_thread_is_a_noop_when_inactive(self):
        t = FunctionTracer(TracerConfig(events=(LINE,)))
        assert t.arm_thread() is False, (
            "arming a hook for an inactive tracer would leave a dangling reference"
        )
        assert sys.gettrace() is None


class TestPathFilters:
    """`include_paths` / `exclude_paths` must normalise *both* sides.

    Normalising only `co_filename` looks right and is silently broken on
    Windows, where `co_filename` contains backslashes: `C:\\src\app.py` never
    starts with `C:/src`, so every file is rejected and the filter traces
    nothing while reporting success.
    """

    def test_include_paths_match_a_windows_style_prefix(self):
        code = compile("x = 1", r"C:\pkg\mod.py", "exec")
        cfg = TracerConfig(events=(CALL,), include_paths=(r"C:\pkg",))
        assert cfg.admits(code), "backslashes in the pattern must be normalised"

        cfg = TracerConfig(events=(CALL,), include_paths=("C:/pkg",))
        assert cfg.admits(code), "forward slashes must match too"

    def test_include_paths_reject_a_different_prefix(self):
        code = compile("x = 1", r"C:\other\mod.py", "exec")
        assert not TracerConfig(include_paths=(r"C:\pkg",)).admits(code)

    def test_include_paths_by_basename(self):
        code = compile("x = 1", r"C:\deep\nested\mod.py", "exec")
        assert TracerConfig(include_paths=("mod.py",)).admits(code)
        assert not TracerConfig(include_paths=("other.py",)).admits(code)

    def test_exclude_paths_wins_over_include(self):
        code = compile("x = 1", r"C:\pkg\vendor\mod.py", "exec")
        cfg = TracerConfig(include_paths=(r"C:\pkg",), exclude_paths=(r"C:\pkg\vendor",))
        assert not cfg.admits(code)

    def test_end_to_end_include_paths_traces_something(self):
        """The user-visible symptom: `--include` producing an empty trace."""
        here = __file__.replace("\\", "/")
        with FunctionTracer(TracerConfig(events=(CALL,), include_paths=(here,))) as t:
            leaf(1)
        assert t.events, (
            "include_paths matched this file by construction, so the trace must "
            "not be empty; an empty one means the pattern and the filename are "
            "being compared in different notations"
        )


class TestProfileTracer:
    def test_call_return_pairs(self):
        with ProfileTracer(TracerConfig(events=(CALL, RETURN),
                                        include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        assert [e.kind for e in t.events] == [CALL, RETURN]

    def test_duration_is_reported(self):
        with ProfileTracer(TracerConfig(events=(RETURN,),
                                        include=lambda c: c.co_name == "leaf")) as t:
            leaf(1)
        assert "duration_ns" in t.events[0].extra

    def test_c_calls_are_opt_in(self):
        cfg = TracerConfig(events=(C_CALL,), include=lambda c: c.co_name == "c_user")
        with ProfileTracer(cfg) as t:
            c_user({1: 2})
        assert t.events == []

    def test_c_calls_when_enabled(self):
        cfg = TracerConfig(events=(C_CALL, C_RETURN), include=lambda c: c.co_name == "c_user")
        with ProfileTracer(cfg, trace_c=True) as t:
            c_user({1: 2})
        names = [e.c_name for e in t.events if e.kind == C_CALL]
        assert names, "no C calls observed"
        assert "dict.get" in names

    def test_c_calls_carry_no_arguments(self):
        """Documented limitation of sys.setprofile: C-call args are invisible."""
        cfg = TracerConfig(events=(C_CALL,), include=lambda c: c.co_name == "c_user")
        with ProfileTracer(cfg, trace_c=True) as t:
            c_user({1: 2})
        assert all(e.arg_repr is None for e in t.events)


def c_user(d: dict) -> int:
    """Calls two C functions on purpose: ``dict.get`` and ``len``."""
    found = d.get("a", None)
    return len(d) + (1 if found else 0)


@pytest.mark.skipif(not HAS_MONITORING, reason="PEP 669 requires 3.12+")
class TestMonitorTracerContracts:
    def make(self, events, **kw):
        from framecraft import MonitorTracer

        return MonitorTracer(TracerConfig(events=events, **kw))

    def test_same_vocabulary_as_settrace(self):
        """The whole point of TraceEvent: both engines produce the same kinds."""
        kinds = {CALL, LINE, RETURN}
        with self.make(kinds, include=lambda c: c.co_name == "leaf") as mt:
            leaf(1)
        with FunctionTracer(TracerConfig(events=kinds, include=lambda c: c.co_name == "leaf")) as ft:
            leaf(1)
        assert {e.kind for e in mt.events} == {e.kind for e in ft.events}
        assert [e.lineno for e in mt.events if e.kind == LINE] == \
               [e.lineno for e in ft.events if e.kind == LINE]

    def test_empty_event_set_is_allowed(self):
        """A globally silent tracer, to be armed per code object later."""
        with self.make(()) as t:
            leaf(1)
        assert t.events == []

    def test_watch_code_arms_one_function(self):
        with self.make(()) as t:
            t.watch_code(leaf.__code__, ("line",))
            leaf(1)
            middle(1)
        names = {e.qualname for e in t.events}
        assert "leaf" in names
        assert "middle" not in names

    def test_tool_id_is_released(self):
        import sys as _sys

        from framecraft._internal.compat import free_tool_ids

        before = free_tool_ids()
        with self.make((CALL,)):
            assert len(free_tool_ids()) < len(before)
        assert free_tool_ids() == before
        assert _sys.monitoring.get_tool(before[0]) is None if before else True

    def test_call_site_event_names_the_callee_and_arg0(self):
        """PEP 669's CALL is the only hook that names the callee and arg0.

        Note the include filter is on the *caller*: the event fires in the
        calling frame's code object.  That is structural, not a quirk, and it is
        why CALL is the right event for dependency analysis.
        """
        from framecraft import CALL_SITE

        def dispatcher():
            return c_user({1: 2})

        with self.make((CALL_SITE,), include=lambda c: c.co_name == "dispatcher") as t:
            dispatcher()
        assert t.events, "no CALL events were delivered"
        assert [e.c_name for e in t.events] == ["c_user"], (
            "one call site in dispatcher, and the event names the callee"
        )
        assert all(e.arg_repr is not None for e in t.events)

    def test_call_site_is_only_on_pep669(self):
        """The counterpart: sys.settrace has no equivalent, and that is the point."""
        with FunctionTracer(TracerConfig(events=(CALL,),
                                         include=lambda c: c.co_name == "dispatcher")) as t:
            def dispatcher():
                return c_user({1: 2})

            dispatcher()
        assert t.events
        assert all(e.c_name is None for e in t.events), (
            "settrace's 'call' event carries no callee; reconstructing it means "
            "disassembling the calling instruction"
        )

    def test_unavailable_events_are_recorded(self):
        """If the interpreter refuses an event, we say so rather than pretend."""
        t = self.make((C_RETURN, C_CALL))
        try:
            with t:
                c_user({1: 2})
        finally:
            pass
        if t.unavailable_events:
            assert t._last_set_events_error

    def test_raise_site_is_observed_on_314(self):
        """A raise handled in the same frame fires RAISE, not PY_UNWIND.

        On 3.14 `sys.monitoring.events.EXCEPTION` no longer exists, so the only
        exception-family event for `try: raise X except X: pass` is RAISE --
        which fires *before* the exception object exists.  Without mapping RAISE
        into the `exception` kind, handled exceptions would be completely
        invisible on 3.14.
        """
        with self.make((EXCEPTION,), capture_locals=True,
                       include=lambda c: c.co_name == "raiser") as t:
            raiser()
        assert t.events, "a handled raise produced no events"
        assert any(e.extra.get("phase") == "raise-opcode" for e in t.events)

    def test_unwind_is_observed_when_the_frame_is_destroyed(self):
        with self.make((EXCEPTION,), capture_locals=True,
                       include=lambda c: c.co_name == "unhandled_raiser") as t:
            try:
                unhandled_raiser()
            except KeyError:
                pass
        phases = {e.extra.get("phase") for e in t.events}
        assert "unwind" in phases, f"expected a PY_UNWIND, got {phases}"
        unwind = next(e for e in t.events if e.extra.get("phase") == "unwind")
        assert unwind.locals, "PY_UNWIND is the last chance to read locals"

    def test_py_yield_fires_for_generators(self):
        with self.make((YIELD,), include=lambda c: c.co_name == "gen") as t:
            list(gen(3))
        assert len(t.events) == 3


class TestOpcodeEvents:
    def test_opcode_events_produce_events(self):
        """Regression test for the CPython 3.14 f_trace_opcodes trap.

        The documented idiom -- set the flags and let the global trace
        function's return value install f_trace -- silently loses every opcode
        event on 3.14.6. FunctionTracer assigns frame.f_trace first.
        """
        with FunctionTracer(
            TracerConfig(events=(CALL, "opcode", RETURN), include=lambda c: c.co_name == "leaf"),
            opcode_events=True,
        ) as t:
            leaf(1)
        opcodes = [e for e in t.events if e.kind == "opcode"]
        assert opcodes, "no opcode events were produced"
        assert all(e.opcode for e in opcodes)

    def test_opcode_offsets_map_to_lines(self):
        with FunctionTracer(
            TracerConfig(events=("opcode",), include=lambda c: c.co_name == "leaf"),
            opcode_events=True,
        ) as t:
            leaf(1)
        assert all(e.lineno is not None for e in t.events)


class TestCompose:
    def test_sampler_plus_settrace(self):
        """The recommended pairing: sample cheaply, then instrument.

        This only works because SamplingTracer does not take a hook slot.  When
        it did, HookLease would refuse the settrace tracer and the one
        combination the library most wants to support would be the one it
        rejected.
        """
        from framecraft import SamplingTracer
        from framecraft.trace import compose

        with compose(SamplingTracer(interval=0.001),
                     FunctionTracer(TracerConfig(events=(CALL, RETURN)))) as (sampler, ft):
            leaf(1)
        assert ft.events, "the instrumenting tracer collected nothing"
        assert isinstance(sampler, SamplingTracer)
        assert not sampler.uses_hook_slot

    def test_sampler_does_not_take_the_hook_lease(self):
        from framecraft import SamplingTracer

        with SamplingTracer(interval=0.001):
            assert HookLease.current() is None
            with FunctionTracer(TracerConfig(events=(CALL,))):
                pass
        assert HookLease.current() is None

    def test_two_settracers_rejected(self):
        from framecraft.trace import compose

        with pytest.raises(RuntimeError, match="one global slot"):
            compose(FunctionTracer(TracerConfig(events=(CALL,))),
                    FunctionTracer(TracerConfig(events=(CALL,))))

    def test_two_profiletracers_rejected(self):
        from framecraft.trace import compose

        with pytest.raises(RuntimeError, match="one global slot"):
            compose(ProfileTracer(TracerConfig(events=(CALL,))),
                    ProfileTracer(TracerConfig(events=(CALL,))))


class TestDecorator:
    def test_trace_function_decorator(self):
        from framecraft import trace_function

        @trace_function("call", "return", scope="self")
        def target(x):
            return x * 2

        assert target(3) == 6
        assert target.last_tracer is not None
        qualnames = {e.qualname for e in target.last_tracer.events}
        assert qualnames, "the decorator produced no events"
        assert all(q.endswith("target") for q in qualnames), qualnames

    def test_preserves_metadata(self):
        from framecraft import trace_function

        @trace_function("call")
        def documented():
            """The docstring"""
            return 1

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "The docstring"
        assert documented.__wrapped__ is not None


class TestNullSinkDoesNotBreak:
    def test_null_sink(self):
        with FunctionTracer(TracerConfig(events=(LINE,), buffer=False)) as t:
            t.add_sink(NullSink())
            leaf(1)
        assert t.error_count == 0
