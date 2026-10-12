"""SQLite persistence: schema DDL and connection management.

A single relational store backs the whole system. The schema separates
registry tables (users, courses, sections), transactional tables
(enrollments), and immutable audit trails (enrollment_audit, event_log)
— the latter two are append-only to guarantee traceability.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name   TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('student','instructor','registrar','admin')),
    major       TEXT,
    max_credits INTEGER NOT NULL DEFAULT 35,
    completed_courses TEXT NOT NULL DEFAULT '[]'   -- JSON list of course codes
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS semesters (
    code              TEXT PRIMARY KEY,          -- e.g. 2026FA
    phase             TEXT NOT NULL,
    handover_context  TEXT NOT NULL DEFAULT '{}' -- snapshot passed between stages
);

CREATE TABLE IF NOT EXISTS courses (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    code      TEXT NOT NULL,
    title     TEXT NOT NULL,
    credits   REAL NOT NULL,
    department TEXT NOT NULL,
    prerequisites TEXT NOT NULL DEFAULT '[]',    -- JSON list of course codes
    description TEXT NOT NULL DEFAULT '',
    catalog_version INTEGER NOT NULL,           -- 1,2,3... per code
    active    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (code, catalog_version)
);

CREATE TABLE IF NOT EXISTS sections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_code TEXT NOT NULL,
    semester    TEXT NOT NULL,
    section_no  TEXT NOT NULL,
    instructor_name TEXT NOT NULL,
    capacity    INTEGER NOT NULL,
    venue       TEXT NOT NULL,
    slots       TEXT NOT NULL,                  -- JSON list of ScheduleSlot
    UNIQUE (semester, course_code, section_no)
);

CREATE TABLE IF NOT EXISTS enrollments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES users(id),
    section_id  INTEGER NOT NULL REFERENCES sections(id),
    semester    TEXT NOT NULL,
    state       TEXT NOT NULL,
    waitlist_seq INTEGER,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE (student_id, section_id)
);

CREATE INDEX IF NOT EXISTS idx_enr_section_state ON enrollments(section_id, state);
CREATE INDEX IF NOT EXISTS idx_enr_student ON enrollments(student_id);

CREATE TABLE IF NOT EXISTS enrollment_audit (   -- append-only
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (          -- append-only
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts      REAL NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync dependencies and endpoints in
    # different threadpool threads. Every request creates and closes its own
    # connection (no cross-request sharing), so cross-thread *creation vs.
    # use* is safe; concurrent use of one connection never happens.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(DDL)
        conn.commit()
    finally:
        conn.close()
