"""SQLite storage: tasks, the event log the bot learns from, rules, missions.

All timestamps are stored as UTC ISO strings. Due dates and times are the
Mayor's local wall-clock values ("2026-11-03", "12:30").
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    building TEXT NOT NULL,
    board TEXT NOT NULL DEFAULT 'today',        -- today | waiting | construction
    status TEXT NOT NULL DEFAULT 'open',        -- open | done | dropped
    priority TEXT NOT NULL DEFAULT 'normal',    -- normal | high
    kind TEXT,                                  -- short category used for learning
    due_date TEXT,
    due_time TEXT,
    next_nudge_at TEXT,
    nudge_count INTEGER NOT NULL DEFAULT 0,
    postpone_count INTEGER NOT NULL DEFAULT 0,
    estimate_min INTEGER,
    actual_min INTEGER,
    delegated INTEGER NOT NULL DEFAULT 0,
    mission_id INTEGER,
    step_order INTEGER,
    notes TEXT,                                 -- e.g. the script for a content task
    link_id INTEGER,                            -- filming task -> its publish task
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    task_id INTEGER,
    type TEXT NOT NULL,
    at TEXT NOT NULL,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Task:
    id: int
    title: str
    building: str
    board: str
    status: str
    priority: str
    kind: str | None
    due_date: str | None
    due_time: str | None
    next_nudge_at: str | None
    nudge_count: int
    postpone_count: int
    estimate_min: int | None
    actual_min: int | None
    delegated: int
    mission_id: int | None
    step_order: int | None
    notes: str | None
    link_id: int | None
    created_at: str
    done_at: str | None
    brand: str | None = None  # the business a content or marketing task is for (a building key)


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        for column, kind in (("notes", "TEXT"), ("link_id", "INTEGER"), ("brand", "TEXT")):
            if column not in columns:  # databases created by an older version
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {kind}")
        self.conn.commit()

    # --- state -----------------------------------------------------------
    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # --- tasks -----------------------------------------------------------
    def add_task(
        self,
        title: str,
        building: str,
        board: str = "today",
        *,
        priority: str = "normal",
        kind: str | None = None,
        due_date: str | None = None,
        due_time: str | None = None,
        next_nudge_at: datetime | None = None,
        estimate_min: int | None = None,
        delegated: bool = False,
        mission_id: int | None = None,
        step_order: int | None = None,
        notes: str | None = None,
        link_id: int | None = None,
        brand: str | None = None,
        now: datetime | None = None,
    ) -> Task:
        now = now or utcnow()
        cur = self.conn.execute(
            "INSERT INTO tasks(title, building, board, priority, kind, due_date, due_time, "
            "next_nudge_at, estimate_min, delegated, mission_id, step_order, notes, link_id, brand, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                title, building, board, priority, kind, due_date, due_time,
                iso(next_nudge_at), estimate_min, int(delegated), mission_id,
                step_order, notes, link_id, brand, iso(now),
            ),
        )
        self.conn.commit()
        self.log(cur.lastrowid, "created", now=now)
        return self.get_task(cur.lastrowid)

    def get_task(self, task_id: int) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task(**dict(row)) if row else None

    def update_task(self, task_id: int, **fields) -> None:
        if not fields:
            return
        for k, v in fields.items():
            if isinstance(v, datetime):
                fields[k] = iso(v)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
        self.conn.commit()

    def open_tasks(self) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status='open' ORDER BY "
            "CASE priority WHEN 'high' THEN 0 ELSE 1 END, due_date IS NULL, due_date, due_time, id"
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def tasks_due_for_nudge(self, now: datetime) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status='open' AND next_nudge_at IS NOT NULL "
            "AND next_nudge_at <= ? ORDER BY next_nudge_at",
            (iso(now),),
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def done_between(self, start: datetime, end: datetime) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status='done' AND done_at >= ? AND done_at < ? ORDER BY done_at",
            (iso(start), iso(end)),
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def done_tasks(self) -> list[Task]:
        rows = self.conn.execute("SELECT * FROM tasks WHERE status='done'").fetchall()
        return [Task(**dict(r)) for r in rows]

    def floors_by_building(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT building, COUNT(*) AS n FROM tasks WHERE status='done' GROUP BY building"
        ).fetchall()
        return {r["building"]: r["n"] for r in rows}

    # --- events ----------------------------------------------------------
    def log(self, task_id: int | None, type_: str, *, now: datetime | None = None, **meta) -> None:
        self.conn.execute(
            "INSERT INTO events(task_id, type, at, meta) VALUES(?,?,?,?)",
            (task_id, type_, iso(now or utcnow()), json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        self.conn.commit()

    def events_for(self, task_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY at", (task_id,)
        ).fetchall()

    # --- rules -----------------------------------------------------------
    def add_rule(self, text: str) -> None:
        self.conn.execute("INSERT INTO rules(text, created_at) VALUES(?, ?)", (text, iso(utcnow())))
        self.conn.commit()

    def rules(self) -> list[str]:
        return [r["text"] for r in self.conn.execute("SELECT text FROM rules ORDER BY id")]

    # --- missions --------------------------------------------------------
    def add_mission(self, name: str, started_at: datetime, ends_at: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO missions(name, started_at, ends_at) VALUES(?,?,?)",
            (name, iso(started_at), iso(ends_at)),
        )
        self.conn.commit()
        return cur.lastrowid

    def active_missions(self, now: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM missions WHERE active=1 AND ends_at > ?", (iso(now),)
        ).fetchall()

    def mission_steps(self, mission_id: int) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE mission_id=? ORDER BY step_order", (mission_id,)
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def close_mission(self, mission_id: int) -> None:
        self.conn.execute("UPDATE missions SET active=0 WHERE id=?", (mission_id,))
        self.conn.commit()
