"""Repository layer: the ONLY place that touches SQL.

Keeps the service layer persistence-agnostic (state/logic separation)
and gives tests a single seam to observe.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import defaultdict

from app.database import connect


# -- helpers ---------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), 100_000
    )
    return digest.hex() == digest_hex


def _row_to_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "role": row["role"],
        "major": row["major"],
        "max_credits": row["max_credits"],
        "completed_courses": set(json.loads(row["completed_courses"])),
    }


# -- users -----------------------------------------------------------------
def create_user(conn, username, password, full_name, role,
                major=None, max_credits=35, completed=None) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, full_name, role, major,"
        " max_credits, completed_courses) VALUES (?,?,?,?,?,?,?)",
        (username, hash_password(password), full_name, role, major,
         max_credits, json.dumps(sorted(completed or []))),
    )
    conn.commit()
    return cur.lastrowid


def get_user_by_username(conn, username):
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return _row_to_user(row) if row else None


def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def set_completed_courses(conn, user_id, completed: set[str]) -> None:
    conn.execute(
        "UPDATE users SET completed_courses=? WHERE id=?",
        (json.dumps(sorted(completed)), user_id),
    )
    conn.commit()


def list_users(conn, role: str | None = None) -> list[dict]:
    if role:
        rows = conn.execute("SELECT * FROM users WHERE role=? ORDER BY id", (role,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_row_to_user(r) for r in rows]


# -- sessions --------------------------------------------------------------
def create_session(conn, user_id) -> str:
    token = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
        (token, user_id, time.time()),
    )
    conn.commit()
    return token


def get_session_user(conn, token: str):
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?",
        (token,),
    ).fetchone()
    return _row_to_user(row) if row else None


# -- semesters / phases ----------------------------------------------------
def upsert_semester(conn, code: str, phase: str, handover: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO semesters (code, phase, handover_context) VALUES (?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET phase=excluded.phase,"
        " handover_context=excluded.handover_context",
        (code, phase, json.dumps(handover or {})),
    )
    conn.commit()


def get_semester(conn, code: str):
    row = conn.execute("SELECT * FROM semesters WHERE code=?", (code,)).fetchone()
    if not row:
        return None
    return {
        "code": row["code"],
        "phase": row["phase"],
        "handover_context": json.loads(row["handover_context"]),
    }


# -- catalog (courses) -----------------------------------------------------
def create_course_version(conn, data: dict) -> tuple[int, int]:
    """Insert a new catalog version for a course code and deactivate the
    previous one. Returns (row_id, new_version)."""
    row = conn.execute(
        "SELECT MAX(catalog_version) AS v FROM courses WHERE code=?",
        (data["code"],),
    ).fetchone()
    new_version = (row["v"] or 0) + 1
    if new_version > 1:
        conn.execute(
            "UPDATE courses SET active=0 WHERE code=? AND catalog_version=?",
            (data["code"], new_version - 1),
        )
    cur = conn.execute(
        "INSERT INTO courses (code, title, credits, department, prerequisites,"
        " description, catalog_version, active) VALUES (?,?,?,?,?,?,?,1)",
        (data["code"], data["title"], data["credits"], data["department"],
         json.dumps(data.get("prerequisites", [])), data.get("description", ""),
         new_version),
    )
    conn.commit()
    return cur.lastrowid, new_version


def get_active_course(conn, code: str):
    row = conn.execute(
        "SELECT * FROM courses WHERE code=? AND active=1", (code,)
    ).fetchone()
    if not row:
        return None
    return _row_to_course(row)


def list_active_courses(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM courses WHERE active=1 ORDER BY code"
    ).fetchall()
    return [_row_to_course(r) for r in rows]


def course_graph(conn) -> dict[str, set[str]]:
    """Prerequisite DAG over all active courses: prereq -> course."""
    graph: dict[str, set[str]] = defaultdict(set)
    for c in list_active_courses(conn):
        for pre in c["prerequisites"]:
            graph[pre].add(c["code"])
    return dict(graph)


def _row_to_course(row) -> dict:
    return {
        "id": row["id"],
        "code": row["code"],
        "title": row["title"],
        "credits": row["credits"],
        "department": row["department"],
        "prerequisites": json.loads(row["prerequisites"]),
        "description": row["description"],
        "catalog_version": row["catalog_version"],
    }


# -- sections --------------------------------------------------------------
def create_section(conn, data: dict) -> int:
    slots = data["slots"]
    if slots and hasattr(slots[0], "model_dump"):
        slots = [s.model_dump() for s in slots]
    cur = conn.execute(
        "INSERT INTO sections (course_code, semester, section_no,"
        " instructor_name, capacity, venue, slots) VALUES (?,?,?,?,?,?,?)",
        (data["course_code"], data["semester"], data["section_no"],
         data["instructor_name"], data["capacity"], data["venue"],
         json.dumps(slots)),
    )
    conn.commit()
    return cur.lastrowid


def _row_to_section(row) -> dict:
    return {
        "id": row["id"],
        "course_code": row["course_code"],
        "semester": row["semester"],
        "section_no": row["section_no"],
        "instructor_name": row["instructor_name"],
        "capacity": row["capacity"],
        "venue": row["venue"],
        "slots": json.loads(row["slots"]),
    }


def get_section(conn, section_id: int):
    row = conn.execute("SELECT * FROM sections WHERE id=?", (section_id,)).fetchone()
    return _row_to_section(row) if row else None


def list_sections(conn, semester: str | None = None,
                  course_code: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM sections WHERE 1=1", []
    if semester:
        sql += " AND semester=?"; params.append(semester)
    if course_code:
        sql += " AND course_code=?"; params.append(course_code)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [_row_to_section(r) for r in rows]


# -- enrollments -----------------------------------------------------------
def create_enrollment(conn, student_id, section_id, semester, state,
                      waitlist_seq=None, reason="") -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO enrollments (student_id, section_id, semester, state,"
        " waitlist_seq, reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (student_id, section_id, semester, state, waitlist_seq, reason, now, now),
    )
    conn.commit()
    return cur.lastrowid


def get_enrollment(conn, enrollment_id: int):
    row = conn.execute(
        "SELECT * FROM enrollments WHERE id=?", (enrollment_id,)
    ).fetchone()
    return dict(row) if row else None


def get_enrollment_by_student_section(conn, student_id, section_id):
    row = conn.execute(
        "SELECT * FROM enrollments WHERE student_id=? AND section_id=?",
        (student_id, section_id),
    ).fetchone()
    return dict(row) if row else None


def update_enrollment_state(conn, enrollment_id: int, state: str,
                            waitlist_seq=None, reason="") -> None:
    conn.execute(
        "UPDATE enrollments SET state=?, waitlist_seq=?, reason=?, updated_at=?"
        " WHERE id=?",
        (state, waitlist_seq, reason, time.time(), enrollment_id),
    )
    conn.commit()


def list_enrollments(conn, student_id=None, section_id=None, semester=None,
                     state=None) -> list[dict]:
    sql, params = "SELECT * FROM enrollments WHERE 1=1", []
    if student_id is not None:
        sql += " AND student_id=?"; params.append(student_id)
    if section_id is not None:
        sql += " AND section_id=?"; params.append(section_id)
    if semester is not None:
        sql += " AND semester=?"; params.append(semester)
    if state is not None:
        sql += " AND state=?"; params.append(state)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [dict(r) for r in rows]


def count_enrolled(conn, section_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM enrollments"
        " WHERE section_id=? AND state='ENROLLED'", (section_id,),
    ).fetchone()
    return row["n"]


def next_waitlist_seq(conn, section_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(waitlist_seq),0)+1 AS seq FROM enrollments"
        " WHERE section_id=? AND state='WAITLISTED'", (section_id,),
    ).fetchone()
    return row["seq"]


def waitlist_head(conn, section_id: int):
    """FIFO: lowest waitlist_seq among WAITLISTED records."""
    row = conn.execute(
        "SELECT * FROM enrollments WHERE section_id=? AND state='WAITLISTED'"
        " ORDER BY waitlist_seq LIMIT 1", (section_id,),
    ).fetchone()
    return dict(row) if row else None


# -- audit & event log (append-only) ---------------------------------------
def append_audit(conn, enrollment_id, from_state, to_state, actor, reason="") -> None:
    conn.execute(
        "INSERT INTO enrollment_audit (enrollment_id, from_state, to_state,"
        " actor, reason, ts) VALUES (?,?,?,?,?,?)",
        (enrollment_id, from_state, to_state, actor, reason, time.time()),
    )
    conn.commit()


def list_audit(conn, enrollment_id=None) -> list[dict]:
    if enrollment_id is not None:
        rows = conn.execute(
            "SELECT * FROM enrollment_audit WHERE enrollment_id=? ORDER BY id",
            (enrollment_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM enrollment_audit ORDER BY id DESC LIMIT 200"
        ).fetchall()
    return [dict(r) for r in rows]


def append_event(conn, event_type: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO event_log (type, payload, ts) VALUES (?,?,?)",
        (event_type, json.dumps(payload, default=str), time.time()),
    )
    conn.commit()


def list_events(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
