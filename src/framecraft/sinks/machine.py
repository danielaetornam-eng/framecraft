"""Machine-readable sinks: JSONL, SQLite, and the flame graph SVG.

:JSONL:`
    Append-only, one JSON object per line.  The default interchange format for
    everything in this library, because it is the only one that is
    (a) streamable, (b) greppable, (c) diffable line-by-line in git, and
    (d) forgiving of schema additions.  ``TraceEvent.to_dict`` is the schema and
    it is append-only by policy: add keys, never rename them.

:SQLiteSink:`
    The same data with an index, for when a trace is too big to load into memory
    and you want SQL.  Uses the stdlib ``sqlite3`` -- no dependency -- and
    commits in batches because committing per event would dominate the cost.

:SVGFlameSink:`
    Renders a flame graph directly from the event stream, without an
    intermediate tree, by maintaining the current stack prefix incrementally.
    See :mod:`framecraft.sinks.flame` for the layout algorithm.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from typing import Any, TextIO

from framecraft.core.events import TraceEvent

__all__ = ["JSONLSink", "SQLiteSink"]


class JSONLSink:
    """One JSON object per line.

    Parameters
    ----------
    stream:
        Destination text stream, or ``None`` with ``path`` given.
    path:
        Destination path.  Opened in append mode so successive runs accumulate.
    include_locals:
        Emit the ``locals`` array.  This is usually 90%+ of the file size, which
        is why it is opt-in rather than part of the schema.
    include_stack:
        Emit the captured ``stack`` array.  Same reasoning.
    flush_every:
        Flush after N events.  ``1`` is safest but slow; ``1000`` is 1000
        events of loss on a crash and 1000x fewer syscalls.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        path: str | None = None,
        include_locals: bool = False,
        include_stack: bool = False,
        flush_every: int = 512,
        sort_keys: bool = False,
    ) -> None:
        self._own = stream is None
        if stream is None:
            path = path or "trace.jsonl"
            stream = open(path, "a", encoding="utf-8")
        self.stream = stream
        self.include_locals = include_locals
        self.include_stack = include_stack
        self.flush_every = max(1, flush_every)
        self.sort_keys = sort_keys
        self.count = 0
        self._path = path

    def write(self, event: TraceEvent) -> None:
        d = event.to_dict(
            include_locals=self.include_locals,
        )
        if self.include_stack and event.stack:
            d["stack"] = [f.to_dict() for f in event.stack]
        self.stream.write(json.dumps(d, sort_keys=self.sort_keys, default=_json_default) + "\n")
        self.count += 1
        if self.count % self.flush_every == 0:
            self.stream.flush()

    def close(self) -> None:
        self.stream.flush()
        if self._own:
            self.stream.close()

    def __repr__(self) -> str:
        return f"<JSONLSink rows={self.count} path={self._path!r}>"


def _json_default(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)


class SQLiteSink:
    """Write events into a SQLite database with an index on ``(kind, code_hash)``.

    Schema
    ------
    ``meta``
        One row: schema version, framecraft version, python version, start wall
        clock.  Without it a ``trace.sqlite`` file found in your temp directory
        six months later is uninterpretable.
    ``codes``
        ``code_hash -> (name, filename, firstlineno)``.  Interned here rather
        than repeated on every event, which is roughly a 5x size reduction on a
        line trace.
    ``events``
        The event rows, referencing ``codes`` by id.

    This is a deliberate schema, not a "one table with everything" dump: the
    whole point of choosing SQL is to be able to say
    ``SELECT code_id, count(*) FROM events JOIN codes USING(code_id) GROUP BY 1``
    without loading anything into Python.
    """

    SCHEMA_VERSION = 1

    COLUMNS = (
        "seq INTEGER PRIMARY KEY",
        "kind TEXT NOT NULL",
        "perf_ns INTEGER NOT NULL",
        "wall REAL",
        "cpu REAL",
        "thread_id INTEGER",
        "thread_name TEXT",
        "task TEXT",
        "depth INTEGER",
        "call_id INTEGER",
        "parent_call_id INTEGER",
        "code_id INTEGER REFERENCES codes(id)",
        "lineno INTEGER",
        "offset INTEGER",
        "opcode TEXT",
        "c_name TEXT",
        "arg TEXT",
        "retval TEXT",
        "exception_type TEXT",
        "exception TEXT",
    )

    def __init__(
        self,
        path: str = "trace.sqlite",
        *,
        include_locals: bool = False,
        commit_every: int = 2000,
        reset: bool = False,
    ) -> None:
        if reset and os.path.exists(path):
            os.remove(path)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")  # survives a crash mid-write
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS codes (
                id INTEGER PRIMARY KEY,
                code_hash TEXT UNIQUE,
                name TEXT,
                qualname TEXT,
                filename TEXT,
                firstlineno INTEGER
            );
        """
        )
        exists = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if not exists:
            self.conn.execute(
                "CREATE TABLE events (\n  "
                + ",\n  ".join(self.COLUMNS)
                + "\n)"
            )
            self.conn.execute("CREATE INDEX ix_events_kind ON events(kind)")
            self.conn.execute("CREATE INDEX ix_events_code ON events(code_id)")
            self.conn.execute("CREATE INDEX ix_events_thread ON events(thread_id, perf_ns)")
        self._code_ids: dict[str, int] = {}
        for cid, ch in self.conn.execute("SELECT id, code_hash FROM codes"):
            self._code_ids[ch] = cid
        self.include_locals = include_locals
        self.commit_every = max(1, commit_every)
        self.count = 0
        self._write_meta()

    def _write_meta(self) -> None:
        import sys

        from framecraft import __version__

        for k, v in (
            ("schema_version", str(self.SCHEMA_VERSION)),
            ("framecraft_version", __version__),
            ("python_version", sys.version.split()[0]),
            ("platform", sys.platform),
        ):
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )
        self.conn.commit()

    def _code_id(self, event: TraceEvent) -> int | None:
        h = event.code_hash
        if not h:
            return None
        cid = self._code_ids.get(h)
        if cid is None:
            cur = self.conn.execute(
                "INSERT INTO codes(code_hash, name, qualname, filename, firstlineno) VALUES(?,?,?,?,?)",
                (h, event.name, event.qualname, event.filename, event.lineno or 0),
            )
            cid = int(cur.lastrowid)
            self._code_ids[h] = cid
        return cid

    def write(self, event: TraceEvent) -> None:
        self.conn.execute(
            "INSERT INTO events (seq, kind, perf_ns, wall, cpu, thread_id, thread_name,"
            " task, depth, call_id, parent_call_id, code_id, lineno, offset, opcode,"
            " c_name, arg, retval, exception_type, exception)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.seq,
                event.kind,
                event.perf_ns,
                event.wall,
                event.cpu,
                event.thread_id,
                event.thread_name,
                event.task,
                event.depth,
                event.call_id,
                event.parent_call_id,
                self._code_id(event),
                event.lineno,
                event.offset,
                event.opcode,
                event.c_name,
                event.arg_repr,
                event.retval_repr,
                event.exception_type,
                event.exception_repr,
            ),
        )
        if self.include_locals and event.locals:
            for b in event.locals:
                self.conn.execute(
                    "INSERT INTO locals (seq, name, scope, value) VALUES (?,?,?,?)",
                    (event.seq, b.name, b.scope, b.value),
                )
        self.count += 1
        if self.count % self.commit_every == 0:
            self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.execute("ANALYZE")  # free win: lets the planner use the indexes
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> SQLiteSink:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<SQLiteSink rows={self.count} path={self.path!r}>"


def query_sqlite(path: str, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
    """Run a query against a trace database.  Convenience for the CLI."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()
