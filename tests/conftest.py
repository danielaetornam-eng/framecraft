"""Shared fixtures.

Two global rules, because tracing tests break each other in confusing ways:

1.  **One tracer at a time.**  ``sys.settrace`` is one slot per thread, and
    ``HookLease`` enforces it.  If a test leaks an active tracer, every
    subsequent test sees a trace it did not ask for.  The ``clean_hooks``
    autouse fixture below is the safety net for exactly that.

2.  **Never let a test fail because of a leftover hook.**  A test that asserts
    ``sys.gettrace() is None`` is testing the *previous* test if the previous
    test crashed.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def clean_hooks():
    """Guarantee no tracing hook survives a test, whatever happened."""
    saved_trace = sys.gettrace()
    saved_profile = sys.getprofile()
    yield
    if sys.gettrace() is not saved_trace:
        sys.settrace(saved_trace)
    if sys.getprofile() is not saved_profile:
        sys.setprofile(saved_profile)


@pytest.fixture
def no_gil_debug():
    """Fail loudly if a test somehow leaves an instrumented frame behind."""
    frames = sys._current_frames()
    for frame in frames.values():
        f = frame
        while f is not None:
            assert f.f_trace is None, (
                f"frame {f.f_code.co_qualname} still has f_trace={f.f_trace!r}; "
                "a tracer was not uninstalled"
            )
            f = f.f_back
