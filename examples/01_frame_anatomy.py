#!/usr/bin/env python3
"""Example 01 -- Anatomy of a live frame.

Run me::

    python examples/01_frame_anatomy.py

There is no tracer in this file.  Everything here is *read-only reflection*: the
skill that a tracer is built on top of, and the one you need before the first
hook makes sense.

The lesson
----------

A frame is a mutable record of one activation of a code object.  It has four
groups of attributes, and mixing them up is the most common source of confusion:

======================  ==========================================  ==========
group                   attributes                                 mutability
======================  ==========================================  ==========
identity                ``f_code``                                 immutable
program counter         ``f_lasti``, ``f_lineno``                  mutable
namespaces              ``f_locals``, ``f_globals``,
                        ``f_builtins``                             proxy/snapshot
link                    ``f_back``                                 read-only
tracing controls        ``f_trace``, ``f_trace_lines``,
                        ``f_trace_opcodes``                        writable
======================  ==========================================  ==========

Two things to internalise from the output:

1.  ``f_code`` is *not* the function.  ``f_code.co_name`` is a string; the
    function object is what you got the code object *from*.  Ten thousand calls
    to one function produce ten thousand frames sharing one code object.
2.  ``f_locals`` is expensive on < 3.13 because every read materialises a fresh
    dict.  ``describe()`` reads it exactly once.
"""

from __future__ import annotations

import dis
import json
import sys

from framecraft import describe, resolve_name, walk

# ----------------------------------------------------------------------
# A little program with something worth looking at.
# ----------------------------------------------------------------------


def inner(a, b=2, *rest, kw_only=None, **extra):
    """Three names in three different scopes, plus a closure-free frame."""
    total = a + b
    label = f"sum={total}"
    return label


def outer(n):
    """A closure, so `outer` has a cell variable we can inspect."""
    factor = n * 2

    def helper(x):
        return inner(x, 3, 9, kw_only=factor)  # type: ignore[arg-type]

    return helper(1)


def show_basics() -> None:
    print("=" * 78)
    print("1. IDENTITY: code object vs frame vs function")
    print("=" * 78)
    f = inner.__code__
    print(f"  function object   {inner!r}")
    print(f"  code object       {f}")
    print(f"  code.co_name      {f.co_name!r}          <- a string, not the function")
    print(f"  code.co_qualname  {f.co_qualname!r}")
    print(f"  code.co_filename  {f.co_filename!r}")
    print(f"  code.co_firstline {f.co_firstlineno}")
    print(f"  code.co_nlocals   {f.co_nlocals}   co_varnames={f.co_varnames}")
    print(f"  code.co_flags     {f.co_flags:#06x}")
    if hasattr(f, "co_nlocalsplus"):
        print(f"  co_nlocalsplus   {f.co_nlocalsplus}  (fast-locals + cells + stack)")
    else:
        print("  co_nlocalsplus   n/a on this build (removed after 3.12)")
    print()
    print("  Every activation makes a NEW frame but reuses the SAME code object:")
    frames = [capture_one_frame() for _ in range(3)]
    print(f"    three calls to inner() -> {len({id(f) for f in frames})} distinct frames")
    print(f"    ...sharing {len({f.f_code for f in frames})} code object: {f.co_name!r}")
    print("    THIS is why a tracer must never key state on the code object alone:")
    print("    two concurrent calls of one function share it and differ only in frame.")
    for fr in frames:
        fr.clear()  # PEP 667: explicitly drop the locals so we do not retain them


def show_namespaces() -> None:
    print()
    print("=" * 78)
    print("2. NAMESPACES: where does each name actually live?")
    print("=" * 78)
    me = sys._getframe()
    print(f"  (we are inside {me.f_code.co_name})")
    print(f"  co_varnames  {me.f_code.co_varnames}")
    print(f"  co_names     {me.f_code.co_names}   <- global names this code touches")
    print(f"  co_freevars  {me.f_code.co_freevars}")
    print(f"  co_cellvars  {me.f_code.co_cellvars}")
    print()
    print(f"  len(f_locals)  = {len(me.f_locals)}")
    print(f"  len(f_globals) = {len(me.f_globals)}")
    print(f"  len(f_builtins)= {len(me.f_builtins)}")
    print()
    for name in ("me", "print", "sys", "definitely_not_here"):
        scope, value = resolve_name(me, name)
        shown = repr(value)
        print(f"  resolve_name({name!r:22}) -> scope={scope:<9} value={shown[:60]}")
    print()
    print("  NOTE: 'print' resolves to scope='builtin', NOT 'global'.")
    print("        A debugger that only looks in f_globals will report it missing.")


def capture_one_frame():
    """Call `inner` and return a retained reference to its frame.

    A frame reference is the only way to look inside a call after it has
    finished -- there is no other supported API for it.
    """
    holder = []
    def peek(*args, **kwargs):
        holder.append(sys._getframe())
        return inner(*args, **kwargs)

    peek(1)
    return holder[0]


def show_closure() -> None:
    print()
    print("=" * 78)
    print("3. CLOSURES: cell variables live in the frame too")
    print("=" * 78)

    def make_adder(n):
        def add(x):
            return x + n

        return add

    adder = make_adder(10)
    holder = {}

    def call_it():
        return adder(5)

    def peek():
        # `_getframe()` inside the traced function, not here. Reuse the same
        # trick: install a temporary trace hook that grabs the frame of
        # `call_it` on entry.  That is exactly what a debugger does, and it is
        # the subject of the next example.
        frame = sys._getframe()
        holder["frame"] = frame
        return call_it()

    result = peek()
    fr = holder["frame"]
    print(f"  adder(5) returned {result}")
    print(f"  peek()'s code.co_freevars = {fr.f_code.co_freevars}  (names it read from enclosing scopes)")
    print(f"  peek()'s code.co_cellvars = {fr.f_code.co_cellvars}  (names it creates cells for)")
    print(f"  peek()'s f_locals         = {sorted(fr.f_locals)}")
    print()
    print("  The `adder` closure's own frame was entered and left without any")
    print("  reference to it surviving -- that is the normal case, and the reason")
    print("  no debugger can show you a post-mortem view of it.")
    print()
    print("  'peek' DID exit, and its locals are still readable:")
    print("    f_locals after return:", sorted(fr.f_locals))
    print("  PEP 667 (3.13+) gives frames a .clear() method for exactly this case;")
    print("  on 3.12 and earlier the only way to release them is to drop the frame.")
    del fr
    holder.clear()


def show_bytecode() -> None:
    print()
    print("=" * 78)
    print("4. BYTECODE: f_lasti is a BYTE offset, and 3.11+ has CACHE entries")
    print("=" * 78)
    print(dis.Bytecode(inner.__code__).dis())
    print("  Observations:")
    print("   * offsets 0, 2, 4 ... are not consecutive instructions. Since 3.11")
    print("     each instruction is followed by CACHE slots, so instruction index")
    print("     != byte offset. Anything that maps offset->line MUST account for it.")
    print("   * `co_lines()` is the authority: it yields (start, end, line) spans.")
    print()
    spans = [s for s in inner.__code__.co_lines() if s[2] is not None]
    print(f"   co_lines() spans: {[(s, e, ln) for s, e, ln in spans]}")
    print(f"   co_exceptiontable: {len(inner.__code__.co_exceptiontable)} bytes "
          f"(3.11+ zero-cost exception handling)")
    print()
    print("  Disassemble a specific line instead of the whole function:")
    for ins in dis.get_instructions(inner.__code__):
        if getattr(ins, "positions", None) and ins.positions.lineno == 42:
            print(f"     line 42: {ins.opname:<16} offset={ins.offset} argrepr={ins.argrepr}")


def show_full_describe() -> None:
    print()
    print("=" * 78)
    print("5. describe(): the reference dump")
    print("=" * 78)
    info = describe(sys._getframe())
    print(json.dumps(
        {
            k: v
            for k, v in info.items()
            if k in ("instruction_pointer", "namespace_sizes", "trace_hooks", "links", "closure", "exceptions")
        },
        indent=2,
        default=str,
    ))
    print()
    print("  locals, in compiler slot order (slot index == index in co_varnames):")
    for row in info.get("locals", []):
        print(f"    slot {row['slot']:<3} {row['name']:<22} scope={row['scope']:<8} {row['value'][:50]}")


def show_walk() -> None:
    print()
    print("=" * 78)
    print("6. WALKING: f_back, depth, and the bottom of the stack")
    print("=" * 78)

    def level3():
        for i, fr in enumerate(walk()):
            print(f"    [{i}] {fr.f_code.co_qualname:<28} {fr.f_code.co_filename.split(chr(92))[-1]}:{fr.f_lineno}")

    def level2():
        level3()

    def level1():
        level2()

    level1()
    print()
    print("  walk() stops cleanly when f_back is None. A naive `while frame:`")
    print("  loop without the None check raises AttributeError at the bottom.")
    print()
    deepest = None
    for fr in walk():
        deepest = fr
    print(f"  outermost frame: {deepest.f_code.co_name}, f_back is {deepest.f_back!r}")


def main() -> None:
    show_basics()
    show_namespaces()
    show_closure()
    show_bytecode()
    show_full_describe()
    show_walk()
    print()
    print("=" * 78)
    print("Next: 02 -- walk the stack from inside a hook (the two-level protocol)")
    print("=" * 78)


if __name__ == "__main__":
    main()
