"""Integration tests: the enrollment pipeline end to end.

Covers policy enforcement, phase-dependent behavior, batch allocation,
and event-driven waitlist promotion.

Seed world (see conftest): alice & bob passed CS101+MATH101; charlie
passed CS101 only; dave passed nothing. Sections: s1=CS201.01 (cap 2,
Mon), s2=EE210.01 (cap 1, Mon, overlaps s1), s3=MATH101.01 (cap 30,
Tue).
"""
from tests.conftest import advance_to, hdr


def enroll(env, who, section_id):
    return env["client"].post("/api/enrollments", headers=hdr(env, who),
                              json={"section_id": section_id})


def add_section(env, course_code, section_no, day, start, end, capacity=30):
    r = env["client"].post("/api/sections", headers=hdr(env, "registrar"),
                           json={"course_code": course_code, "semester": "2026FA",
                                 "section_no": section_no,
                                 "instructor_name": "Staff", "capacity": capacity,
                                 "venue": "GEN-1",
                                 "slots": [{"day": day, "start_min": start,
                                            "end_min": end}]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def set_max_credits(env, username: str, value: int) -> None:
    conn = env["app"].state.db_factory()
    try:
        conn.execute("UPDATE users SET max_credits=? WHERE username=?",
                     (value, username))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------- pre-enrollment wish ----
def test_pre_enrollment_collects_validated_wishes(env):
    r = enroll(env, "alice", env["s1"])
    assert r.status_code == 200
    assert r.json()["state"] == "VALIDATED"      # capacity-blind wish


def test_prerequisite_enforced(env):
    r = enroll(env, "dave", env["s1"])           # dave passed nothing
    assert r.status_code == 400
    body = r.json()["detail"]
    assert body["code"] == "PREREQ_MISSING"
    # the failed request is REJECTED in the FSM, not left dangling
    mine = env["client"].get("/api/enrollments/me", headers=hdr(env, "dave")).json()
    assert mine and mine[0]["state"] == "REJECTED"


def test_duplicate_and_retake_rejected(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    assert enroll(env, "alice", env["s1"]).json()["state"] == "ENROLLED"
    # same course, different section -> duplicate
    cs201b = add_section(env, "CS201", "02", 3, 510, 650)
    r = enroll(env, "alice", cs201b)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "DUPLICATE_ENROLLMENT"
    # retaking an already-passed course -> rejected
    cs101a = add_section(env, "CS101", "01", 4, 510, 650)
    r = enroll(env, "bob", cs101a)               # bob already passed CS101
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "ALREADY_PASSED"


def test_credit_limit_enforced(env):
    set_max_credits(env, "alice", 5)             # CS201(3) ok; +EE210(3) = 6 > 5
    advance_to(env, "add_drop", hdr(env, "registrar"))
    assert enroll(env, "alice", env["s1"]).json()["state"] == "ENROLLED"
    ee210b = add_section(env, "EE210", "02", 3, 510, 650)   # Wed, no overlap
    r = enroll(env, "alice", ee210b)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "CREDIT_LIMIT"


def test_timetable_conflict_enforced(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    assert enroll(env, "alice", env["s1"]).json()["state"] == "ENROLLED"
    r = enroll(env, "alice", env["s2"])          # EE210 overlaps CS201 on Monday
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "TIMETABLE_CONFLICT"
    # non-overlapping section of the same course still works
    ee210b = add_section(env, "EE210", "02", 3, 510, 650)
    assert enroll(env, "alice", ee210b).json()["state"] == "ENROLLED"


# ------------------------------------------------------- phase locking ----
def test_enrollment_locked_during_allocation(env):
    advance_to(env, "allocation", hdr(env, "registrar"))
    r = enroll(env, "alice", env["s1"])
    assert r.status_code == 400 and r.json()["detail"]["code"] == "PHASE_LOCKED"


# ------------------------------------------------------------ FCFS mode ----
def test_add_drop_is_first_come_first_served(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    assert enroll(env, "alice", env["s1"]).json()["state"] == "ENROLLED"
    assert enroll(env, "bob", env["s1"]).json()["state"] == "ENROLLED"
    r = enroll(env, "charlie", env["s1"])        # capacity 2 exhausted
    assert r.json()["state"] == "WAITLISTED"
    assert r.json()["waitlist_seq"] == 1


# ------------------------------------------------------- waitlist engine ----
def test_drop_triggers_fifo_promotion(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    a = enroll(env, "alice", env["s1"]).json()
    enroll(env, "bob", env["s1"])
    c = enroll(env, "charlie", env["s1"]).json()
    assert c["state"] == "WAITLISTED"

    # alice drops -> charlie (FIFO head) is auto-promoted
    r = env["client"].post(f"/api/enrollments/{a['id']}/drop",
                           headers=hdr(env, "alice"))
    assert r.json()["state"] == "DROPPED"
    promoted = env["client"].get("/api/enrollments/me",
                                 headers=hdr(env, "charlie")).json()
    assert promoted[0]["state"] == "ENROLLED"


def test_ineligible_head_is_skipped_and_expired(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    set_max_credits(env, "charlie", 3)           # CS201(3cr) just fits
    a = enroll(env, "alice", env["s1"]).json()
    enroll(env, "bob", env["s1"])
    c = enroll(env, "charlie", env["s1"]).json()
    assert c["state"] == "WAITLISTED"
    # charlie's credit ceiling drops while he waits -> promotion must fail
    set_max_credits(env, "charlie", 2)
    env["client"].post(f"/api/enrollments/{a['id']}/drop", headers=hdr(env, "alice"))

    charlie_rows = env["client"].get("/api/enrollments/me",
                                     headers=hdr(env, "charlie")).json()
    assert charlie_rows[0]["state"] == "EXPIRED"
    audit = env["client"].get("/api/admin/audit",
                              headers=hdr(env, "registrar")).json()
    expired = [x for x in audit if x["to_state"] == "EXPIRED"]
    assert expired and "CREDIT_LIMIT" in expired[0]["reason"]


# ---------------------------------------------------------- allocation ----
def test_batch_allocation_with_seeded_lottery(env):
    # three wishes, capacity 2 -> 2 enrolled, 1 waitlisted
    for who in ("alice", "bob", "charlie"):
        assert enroll(env, who, env["s1"]).json()["state"] == "VALIDATED"
    advance_to(env, "allocation", hdr(env, "registrar"))
    r = env["client"].post("/api/admin/allocation/run?seed=7",
                           headers=hdr(env, "registrar"))
    assert r.status_code == 200
    summary = r.json()["sections"]["CS201.01"]
    assert summary == {"capacity": 2, "requests": 3,
                       "enrolled": 2, "waitlisted": 1}
    states = sorted(
        e["state"] for e in env["client"].get(
            "/api/enrollments", headers=hdr(env, "registrar")).json())
    assert states == ["ENROLLED", "ENROLLED", "WAITLISTED"]


def test_allocation_is_reproducible_for_same_seed():
    def run_once(base: str):
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient

        from app.main import create_app
        from tests.conftest import login as _login

        with tempfile.TemporaryDirectory() as d:
            app = create_app(Path(d) / base)
            c = TestClient(app)
            h_reg = _login(c, "registrar", "reg123")
            h_admin = _login(c, "admin", "admin123")
            for name in ("stu1", "stu2", "stu3"):
                assert c.post("/api/users", headers=h_admin,
                              json={"username": name, "password": "pass123",
                                    "full_name": name, "role": "student"}
                              ).status_code == 200
            c.post("/api/courses", headers=h_reg,
                   json={"code": "AA100", "title": "Intro", "credits": 2,
                         "department": "General", "prerequisites": []})
            sec = c.post("/api/sections", headers=h_reg,
                         json={"course_code": "AA100", "semester": "2026FA",
                               "section_no": "01", "instructor_name": "T",
                               "capacity": 1, "venue": "V",
                               "slots": [{"day": 1, "start_min": 100,
                                          "end_min": 200}]}).json()
            for name in ("stu1", "stu2", "stu3"):
                h = _login(c, name, "pass123")
                assert c.post("/api/enrollments", headers=h,
                              json={"section_id": sec["id"]}).status_code == 200
            c.post("/api/admin/phase/advance", headers=h_reg)
            c.post("/api/admin/allocation/run?seed=99", headers=h_reg)
            rows = c.get("/api/enrollments", headers=h_reg).json()
            return sorted(e["student"] for e in rows if e["state"] == "ENROLLED")

    assert run_once("a.db") == run_once("b.db")


def test_audit_trail_records_full_lifecycle(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    a = enroll(env, "alice", env["s1"]).json()
    env["client"].post(f"/api/enrollments/{a['id']}/drop", headers=hdr(env, "alice"))
    audit = env["client"].get(
        f"/api/admin/audit?enrollment_id={a['id']}",
        headers=hdr(env, "registrar")).json()
    chain = [(x["from_state"], x["to_state"]) for x in audit]
    assert chain == [("SUBMITTED", "VALIDATED"), ("VALIDATED", "ENROLLED"),
                     ("ENROLLED", "DROPPED")]


def test_event_log_records_waitlist_promotion(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    a = enroll(env, "alice", env["s1"]).json()
    enroll(env, "bob", env["s1"])
    enroll(env, "charlie", env["s1"])
    env["client"].post(f"/api/enrollments/{a['id']}/drop", headers=hdr(env, "alice"))
    events = env["client"].get("/api/admin/events",
                               headers=hdr(env, "registrar")).json()
    types = {e["type"] for e in events}
    assert "section.seat_released" in types
    assert "waitlist.promoted" in types
