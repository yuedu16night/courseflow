"""Integration tests: RBAC, catalog versioning, and phase orchestration."""
from tests.conftest import advance_to, hdr


# ---------------------------------------------------------------- RBAC ----
def test_login_and_me(env):
    h = hdr(env, "alice")
    r = env["client"].get("/api/auth/me", headers=h)
    assert r.status_code == 200
    assert r.json()["role"] == "student"


def test_bad_credentials(env):
    r = env["client"].post("/api/auth/login",
                           json={"username": "alice", "password": "wrong!"})
    assert r.status_code == 401


def test_missing_token(env):
    assert env["client"].get("/api/courses").status_code == 401


def test_student_cannot_write_catalog(env):
    r = env["client"].post("/api/courses", headers=hdr(env, "alice"),
                           json={"code": "XX999", "title": "Hack", "credits": 1,
                                 "department": "X", "prerequisites": []})
    assert r.status_code == 403
    assert "catalog:write" in r.json()["detail"]


def test_registrar_cannot_create_users(env):
    r = env["client"].post("/api/users", headers=hdr(env, "registrar"),
                           json={"username": "eve", "password": "eve12345",
                                 "full_name": "Eve", "role": "admin"})
    assert r.status_code == 403


def test_admin_can_create_users(env):
    r = env["client"].post("/api/users", headers=hdr(env, "admin"),
                           json={"username": "frank", "password": "frank123",
                                 "full_name": "Frank", "role": "student"})
    assert r.status_code == 200


def test_instructor_roster_scoped_to_own_sections(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    # alice (eligible for CS201) takes Carol's section; bob takes EE210 (Dan's)
    r1 = env["client"].post("/api/enrollments", headers=hdr(env, "alice"),
                            json={"section_id": env["s1"]})
    assert r1.status_code == 200, r1.text
    r2 = env["client"].post("/api/enrollments", headers=hdr(env, "bob"),
                            json={"section_id": env["s2"]})
    assert r2.status_code == 200, r2.text
    h_carol = hdr(env, "carol")
    r = env["client"].get(f"/api/sections/{env['s1']}/roster", headers=h_carol)
    assert r.status_code == 200 and len(r.json()["enrolled"]) == 1
    r = env["client"].get(f"/api/sections/{env['s2']}/roster", headers=h_carol)
    assert r.status_code == 403
    # students have no roster access at all
    r = env["client"].get(f"/api/sections/{env['s1']}/roster", headers=hdr(env, "alice"))
    assert r.status_code == 403
    # registrar retains access via roster:read_any
    h_reg = hdr(env, "registrar")
    r = env["client"].get(f"/api/sections/{env['s1']}/roster", headers=h_reg)
    assert r.status_code == 200


# -------------------------------------------------------- catalog DAG ----
def test_course_versioning(env):
    h = hdr(env, "registrar")
    r1 = env["client"].get("/api/courses", headers=h).json()
    v1 = next(c for c in r1 if c["code"] == "CS201")
    assert v1["catalog_version"] == 1
    # editing = new immutable version
    r = env["client"].post("/api/courses", headers=h,
                           json={"code": "CS201", "title": "Data Structures (v2)",
                                 "credits": 4, "department": "Computer Science",
                                 "prerequisites": ["CS101", "MATH101"]})
    assert r.status_code == 200 and r.json()["catalog_version"] == 2
    active = {c["code"]: c for c in
              env["client"].get("/api/courses", headers=h).json()}
    assert active["CS201"]["credits"] == 4
    assert active["CS201"]["prerequisites"] == ["CS101", "MATH101"]


def test_prerequisite_cycle_rejected(env):
    r = env["client"].post("/api/courses", headers=hdr(env, "registrar"),
                           json={"code": "CS101", "title": "Intro v2", "credits": 3,
                                 "department": "Computer Science",
                                 "prerequisites": ["CS201"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "PREREQ_CYCLE"


def test_unknown_prerequisite_rejected(env):
    r = env["client"].post("/api/courses", headers=hdr(env, "registrar"),
                           json={"code": "CS999", "title": "Ghost", "credits": 1,
                                 "department": "CS", "prerequisites": ["NO101"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "PREREQ_UNKNOWN"


def test_curriculum_topological_order(env):
    r = env["client"].get("/api/courses/curriculum-order", headers=hdr(env, "alice"))
    order = r.json()["topological_order"]
    assert order.index("CS101") < order.index("CS201")
    assert order.index("MATH101") < order.index("EE210")


# ------------------------------------------------------------- phases ----
def test_phase_transitions_are_linear(env):
    h = hdr(env, "registrar")
    for expected in ("allocation", "add_drop", "closed"):
        r = env["client"].post("/api/admin/phase/advance", headers=h)
        assert r.json()["phase"] == expected
    # closed is terminal
    r = env["client"].post("/api/admin/phase/advance", headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "PHASE_TERMINAL"


def test_student_cannot_advance_phase(env):
    r = env["client"].post("/api/admin/phase/advance", headers=hdr(env, "alice"))
    assert r.status_code == 403


def test_stage_handover_snapshot(env):
    h = hdr(env, "registrar")
    # alice submits a wish in PRE_ENROLLMENT
    r = env["client"].post("/api/enrollments", headers=hdr(env, "alice"),
                           json={"section_id": env["s1"]})
    assert r.json()["state"] == "VALIDATED"
    r = env["client"].post("/api/admin/phase/advance", headers=h)
    ctx = r.json()["handover_context"]
    section_key = "CS201.01"
    assert ctx["sections"][section_key]["validated_pending"] == 1
    # the snapshot is readable afterwards via the phase status endpoint
    r = env["client"].get("/api/admin/phase", headers=h)
    assert r.json()["handover_context"]["sections"][section_key]["validated_pending"] == 1
