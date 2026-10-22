"""framecraft internals.

Nothing here is public API.  The two rules that keep it private:

1.  Every module in here is documented as *mechanism*, not *policy*.  If a
    function here has to make a choice you might disagree with, it belongs in
    ``framecraft.core`` or ``framecraft.trace``.
2.  Nothing in here imports from ``framecraft.core`` at module scope.  The
    dependency arrow points one way: ``core`` -> ``_internal``.  (There is one
    deliberate local import inside :func:`framecraft.core.frames.capture_locals`
    to break what would otherwise be a cycle; it is commented at the call site.)
"""

__all__: list[str] = []
