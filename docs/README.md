# framecraft documentation

Read these in order. Each chapter is written to be read *before* you touch the
API it describes, because each one contains something you will get wrong
otherwise.

| # | Chapter | What it settles |
|---|---------|-----------------|
| 00 | [The map](00-the-map.md) | Which hook for which question. Start here. |
| 01 | [Frame anatomy](01-frame-anatomy.md) | Code objects, frames, namespaces, the instruction pointer, suspension. |
| 02 | [The hooks](02-the-hooks.md) | `sys.settrace`, `sys.setprofile`, `sys.monitoring`, sampling — what each can and cannot see. |
| 03 | [Gotchas catalogue](03-gotchas.md) | Every trap in this library, with a minimal reproducer and a fix. |
| 04 | [Building a tracer](04-building-a-tracer.md) | Hook design, the two-level protocol, error isolation, scoping. |
| 05 | [Capturing state safely](05-capturing-state.md) | `safe_repr`, redaction, budgets, why `f_locals` costs what it costs. |
| 06 | [Performance](06-performance.md) | Measured overhead, where it goes, and how to make it smaller. |
| 07 | [Graphs and reports](07-graphs-and-reports.md) | Call graphs, stack trees, timelines, flame graphs, the HTML report. |
| 08 | [Threads and asyncio](08-threads-and-asyncio.md) | Per-thread hooks, task identity, and what "blocked" looks like in a trace. |
| 09 | [Version differences](09-versions.md) | Every behaviour that changed between 3.10 and 3.14, in one table. |
| 10 | [API reference](10-api-reference.md) | Every public name, grouped. |
| 11 | [Cookbook](11-cookbook.md) | Task-shaped recipes. |

## How this documentation is written

Three rules, applied throughout:

1. **Claims are measured, not remembered.** Where a number appears, it came from
   running the code on CPython 3.14.6 / Windows, and the measurement is
   reproducible from `bench/overhead.py` or the example that quotes it.
2. **Limits are stated with the limits they are.** A tracer that cannot see
   blocking says so, rather than presenting its timings as complete.
3. **Failure modes are shown as failures.** Every "gotcha" chapter entry has a
   reproducer you can paste into a REPL, because a trap you have seen fire is a
   trap you will not hit again.

## Two things to know before you start

**A tracer is a distortion.** Any hook that fires per bytecode adds cost per
bytecode, which is not proportional across your program. That does not make
tracing wrong; it makes it wrong *for timing*. For control flow and state it is
exact, and for timing you want a sampler (`examples/07`).

**A tracer runs inside your program.** It re-enters arbitrary user code (every
`repr` it calls), it competes for the GIL, and a bug in it can take down the
process you were trying to debug. Everything in chapter 04 exists because of
this.