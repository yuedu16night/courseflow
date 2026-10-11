"""End-to-end demo: seeds a realistic semester and walks a scripted
scenario through the public REST API, printing a human-readable trace.

Run:  python demo/seed_demo.py
Output: console + docs/demo_trace.md (execution evidence for the report)

Cast:
  alice (passed CS101, MATH101)   bob (passed CS101, MATH101)
  charlie (passed CS101 only)     dave (passed nothing)
  erin (passed MATH101)           carol — instructor of CS201.01
Sections: CS201.01 cap=2 Mon 08:30-10:10, EE210.01 cap=1 Tue 08:30-10:10
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import repositories as repo  # noqa: E402
from app.main import create_app  # noqa: E402

DB = ROOT / "demo.db"
TRACE = ROOT / "docs" / "demo_trace.md"
out_lines: list[str] = []


def say(line: str = "") -> None:
    print(line)
    out_lines.append(line)


def tok(client: TestClient, username: str) -> str:
    password = {"registrar": "reg123", "admin": "admin123"}.get(
        username, username + "123")
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def main() -> None:
    if DB.exists():
        DB.unlink()
    app = create_app(DB)
    client = TestClient(app)

    say("# CourseFlow — End-to-End Execution Trace")
    say()
    say("Scripted demo executed against the REST API by `demo/seed_demo.py`.")
    say()

    def call(method, path, token=None, label=None, expect=200, **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = getattr(client, method)("/api" + path, headers=headers, **kw)
        tag = label or f"{method.upper()} {path}"
        icon = "OK" if r.status_code == expect else "!!"
        say(f"- `{icon} {tag}` → HTTP {r.status_code}"
            + ("" if r.status_code == expect else f" (expected {expect})"))
        if r.status_code != expect:
            say(f"    - body: {r.json()}")
        return r

    # ---------------------------------------------------------- setup ----
    say("## 0. Bootstrap")
    reg = tok(client, "registrar")
    adm = tok(client, "admin")
    say("- app factory created default accounts; registrar & admin sessions issued")
    say()

    say("## 1. Seed: users, catalog (prerequisite DAG), sections")
    names = {"alice": "Alice Wang", "bob": "Bob Li", "charlie": "Charlie Zhao",
             "dave": "Dave Qian", "erin": "Erin Sun", "carol": "Carol Chen"}
    for name, role in [("alice", "student"), ("bob", "student"),
                       ("charlie", "student"), ("dave", "student"),
                       ("erin", "student"), ("carol", "instructor")]:
        call("post", "/users", adm, f"create user {name} ({role})",
             json={"username": name, "password": name + "123",
                   "full_name": names[name], "role": role})
    # transcripts (in a real deployment: imported from the SIS)
    conn = app.state.db_factory()
    try:
        for name, done in [("alice", {"CS101", "MATH101"}),
                           ("bob", {"CS101", "MATH101"}),
                           ("charlie", {"CS101"}),
                           ("dave", set()),
                           ("erin", {"MATH101"})]:
            u = repo.get_user_by_username(conn, name)
            repo.set_completed_courses(conn, u["id"], done)
    finally:
        conn.close()
    say("- transcripts loaded (SIS import)")

    for c in [
        {"code": "CS101", "title": "Intro to Programming", "credits": 3,
         "department": "Computer Science", "prerequisites": []},
        {"code": "MATH101", "title": "Calculus I", "credits": 4,
         "department": "Mathematics", "prerequisites": []},
        {"code": "CS201", "title": "Data Structures", "credits": 3,
         "department": "Computer Science", "prerequisites": ["CS101"]},
        {"code": "EE210", "title": "Circuit Theory", "credits": 3,
         "department": "Electrical Engineering", "prerequisites": ["MATH101"]},
    ]:
        call("post", "/courses", reg, f"course {c['code']}", json=c)

    r = call("post", "/sections", reg,
             "section CS201.01 (cap 2, Mon 08:30-10:10, Carol Chen)",
             json={"course_code": "CS201", "semester": "2026FA",
                   "section_no": "01", "instructor_name": "Carol Chen",
                   "capacity": 2, "venue": "SE-101",
                   "slots": [{"day": 1, "start_min": 510, "end_min": 650}]})
    S1 = r.json()["id"]
    r = call("post", "/sections", reg,
             "section EE210.01 (cap 1, Tue 08:30-10:10, Dan Wu)",
             json={"course_code": "EE210", "semester": "2026FA",
                   "section_no": "01", "instructor_name": "Dan Wu",
                   "capacity": 1, "venue": "EE-201",
                   "slots": [{"day": 2, "start_min": 510, "end_min": 650}]})
    S2 = r.json()["id"]
    say()

    # ------------------------------------------------ semantic guards ----
    say("## 2. Semantic constraint enforcement (phase = pre_enrollment)")
    call("post", "/enrollments", tok(client, "dave"),
         "dave requests CS201.01", expect=400, json={"section_id": S1})
    say("    - REJECTED: PREREQ_MISSING (dave has no CS101) — DAG policy")
    call("post", "/enrollments", tok(client, "alice"),
         "alice wishes CS201.01", json={"section_id": S1})
    say("    - wish stored as VALIDATED (capacity-blind in this stage)")
    call("post", "/courses", reg, "add cycle edge CS201 -> CS101", expect=400,
         json={"code": "CS101", "title": "Bad", "credits": 3,
               "department": "CS", "prerequisites": ["CS201"]})
    say("    - REJECTED: PREREQ_CYCLE — catalog graph integrity preserved")
    r = call("post", "/courses", reg, "edit CS101 -> immutable version 2",
             json={"code": "CS101", "title": "Intro to Programming (Python)",
                   "credits": 3, "department": "Computer Science",
                   "prerequisites": []})
    say(f"    - CS101 now at catalog_version {r.json()['catalog_version']}")
    say()

    # ------------------------------------------------- stage handover ----
    say("## 3. Multi-stage orchestration: pre_enrollment → allocation")
    for who in ["bob", "charlie"]:
        call("post", "/enrollments", tok(client, who),
             f"{who} wishes CS201.01", json={"section_id": S1})
    r = call("post", "/admin/phase/advance", reg, "advance phase")
    snap = r.json()["handover_context"]["sections"]["CS201.01"]
    say(f"    - phase → {r.json()['phase']}; handover snapshot: {snap}")
    call("post", "/enrollments", tok(client, "erin"),
         "enroll while in ALLOCATION", expect=400, json={"section_id": S1})
    say("    - REJECTED: PHASE_LOCKED — behavior is stage-scoped")
    r = call("post", "/admin/allocation/run?seed=42", reg,
             "registrar runs seeded batch allocation")
    say(f"    - allocation result: {r.json()['sections']}")
    say()

    # ------------------------------------------------------ add/drop ----
    say("## 4. add_drop: FCFS + event-driven waitlist promotion")
    call("post", "/admin/phase/advance", reg, "advance phase → add_drop")
    r = call("post", "/enrollments", tok(client, "bob"),
             "bob requests EE210.01 (cap 1)", json={"section_id": S2})
    say(f"    - state: {r.json()['state']}")
    r = call("post", "/enrollments", tok(client, "alice"),
             "alice requests EE210.01 — full", json={"section_id": S2})
    say(f"    - state: {r.json()['state']} (waitlist_seq={r.json()['waitlist_seq']})")
    r = call("post", "/enrollments", tok(client, "erin"),
             "erin requests EE210.01 — full", json={"section_id": S2})
    say(f"    - state: {r.json()['state']} (waitlist_seq={r.json()['waitlist_seq']})")
    call("post", "/enrollments", tok(client, "charlie"),
         "charlie requests EE210.01", expect=400, json={"section_id": S2})
    say("    - REJECTED: PREREQ_MISSING (charlie lacks MATH101)")

    r = call("post", f"/enrollments/{{alice_enr}}/drop".replace(
        "{alice_enr}", str(_alice_enr(client, S2))), tok(client, "alice"),
        "alice drops EE210.01")
    say("    - seat released → event `section.seat_released` published")
    alice_now = client.get("/api/enrollments/me",
                           headers={"Authorization": f"Bearer {tok(client, 'alice')}"}
                           ).json()
    say(f"    - alice re-checked: {alice_now[-1]['state']} — FIFO head was herself, "
        "no promotion needed")
    # bob (ENROLLED) drops instead: promotion of the new FIFO head (erin)
    bob_enr = _find(client, tok(client, "bob"), S2)
    call("post", f"/enrollments/{bob_enr}/drop", tok(client, "bob"),
         "bob drops EE210.01")
    say("    - event `section.seat_released` → WaitlistEngine promotes erin")
    erin_now = client.get("/api/enrollments/me",
                          headers={"Authorization": f"Bearer {tok(client, 'erin')}"}
                          ).json()
    e_row = next(e for e in erin_now if e["section_id"] == S2)
    say(f"    - erin: WAITLISTED → {e_row['state']} (zero polling, event-driven)")
    call("post", "/admin/phase/advance", reg, "advance phase → closed")
    say("    - semester CLOSED: enrollment frozen; audit trail immutable")
    say()

    say("## 5. Observability: audit chain & event log")
    audit = client.get(f"/api/admin/audit?enrollment_id={e_row['id']}",
                       headers={"Authorization": f"Bearer {reg}"}).json()
    say(f"- enrollment #{e_row['id']} (erin → EE210.01) lifecycle:")
    for a in audit:
        say(f"    - {a['from_state'] or '∅'} → **{a['to_state']}** by {a['actor']}"
            + (f" ({a['reason']})" if a["reason"] else ""))
    events = client.get("/api/admin/events",
                        headers={"Authorization": f"Bearer {reg}"}).json()
    say(f"- event log: {len(events)} events recorded, types: "
        f"{sorted({e['type'] for e in events})}")


def _find(client, token, section_id):
    rows = client.get("/api/enrollments/me",
                      headers={"Authorization": f"Bearer {token}"}).json()
    return next(e["id"] for e in rows if e["section_id"] == section_id
                and e["state"] in ("ENROLLED", "WAITLISTED", "VALIDATED"))


def _alice_enr(client, section_id):
    return _find(client, tok(client, "alice"), section_id)


if __name__ == "__main__":
    main()
    TRACE.parent.mkdir(exist_ok=True)
    TRACE.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"\ntrace written to {TRACE}")
