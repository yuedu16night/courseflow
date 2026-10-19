"""Shared fixtures: an isolated app per test with deterministic seed data.

Seed world (semester 2026FA, phase PRE_ENROLLMENT):
  users     alice (CS, passed CS101+MATH101), bob (CS, passed CS101),
            charlie (EE, passed nothing), carol (instructor "Carol Chen"),
            registrar / admin (bootstrapped by create_app)
  courses   CS101 (3cr), MATH101 (4cr), CS201 (3cr, prereq CS101),
            EE210 (3cr)
  sections  S1 CS201.01 cap=2  Mon 08:30-10:10
            S2 EE210.01  cap=1  Mon 10:00-11:40   (overlaps S1)
            S3 MATH101.01 cap=30 Tue 08:30-10:10  (no overlap)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import repositories as repo
from app.main import create_app
from app.models import ScheduleSlot, SectionIn
from app.runtime import CURRENT_SEMESTER
from app.services import CatalogService

SEM = CURRENT_SEMESTER


def login(client: TestClient, username: str, password: str) -> dict:
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(db)
    client = TestClient(app)

    conn = app.state.db_factory()
    try:
        repo.create_user(conn, "alice", "alice123", "Alice Wang", "student",
                         major="CS", completed={"CS101", "MATH101"})
        repo.create_user(conn, "bob", "bob123", "Bob Li", "student",
                         major="CS", completed={"CS101", "MATH101"})
        repo.create_user(conn, "charlie", "charlie123", "Charlie Zhao",
                         "student", major="EE", completed={"CS101"})
        repo.create_user(conn, "dave", "dave123", "Dave Qian", "student",
                         major="ME", completed=set())
        repo.create_user(conn, "carol", "carol123", "Carol Chen", "instructor")

        cat = CatalogService(conn, app.state.bus)
        for course in [
            {"code": "CS101", "title": "Intro to Programming", "credits": 3,
             "department": "Computer Science", "prerequisites": []},
            {"code": "MATH101", "title": "Calculus I", "credits": 4,
             "department": "Mathematics", "prerequisites": []},
            {"code": "CS201", "title": "Data Structures", "credits": 3,
             "department": "Computer Science", "prerequisites": ["CS101"]},
            {"code": "EE210", "title": "Circuit Theory", "credits": 3,
             "department": "Electrical Engineering", "prerequisites": ["MATH101"]},
        ]:
            cat.create_course(course)

        def slot(day, start, end):
            return ScheduleSlot(day=day, start_min=start, end_min=end).model_dump()

        s1 = cat.create_section(SectionIn(
            course_code="CS201", semester=SEM, section_no="01",
            instructor_name="Carol Chen", capacity=2, venue="SE-101",
            slots=[slot(1, 510, 650)]))
        s2 = cat.create_section(SectionIn(
            course_code="EE210", semester=SEM, section_no="01",
            instructor_name="Dan Wu", capacity=1, venue="EE-201",
            slots=[slot(1, 600, 700)]))
        s3 = cat.create_section(SectionIn(
            course_code="MATH101", semester=SEM, section_no="01",
            instructor_name="Erin Liu", capacity=30, venue="MA-1",
            slots=[slot(2, 510, 650)]))

        # 培养方案：一份 CS 版本，覆盖 all_of / choose_n / min_credits 三类规则
        repo.deactivate_degree_versions(conn, "CS")
        rid = repo.create_degree_requirement(conn, "CS", 120.0,
                                             "计算机科学培养方案 v1")
        repo.append_degree_rule(conn, rid, "专业必修", "all_of",
                                ["CS101", "CS201"], None, None, 0)
        repo.append_degree_rule(conn, rid, "数学基础", "min_credits",
                                ["MATH101"], 4.0, None, 1)
        repo.append_degree_rule(conn, rid, "专业选修", "choose_n",
                                ["CS201", "EE210"], None, 1, 2)
    finally:
        conn.close()

    return {
        "client": client,
        "app": app,
        "s1": s1["id"], "s2": s2["id"], "s3": s3["id"],
    }


def hdr(env, who: str, pw: str | None = None) -> dict:
    creds = {
        "alice": "alice123", "bob": "bob123", "charlie": "charlie123",
        "dave": "dave123", "carol": "carol123",
        "registrar": "reg123", "admin": "admin123",
    }
    return login(env["client"], who, pw or creds[who])


def advance_to(env, target: str, h_registrar: dict) -> None:
    order = ["pre_enrollment", "allocation", "add_drop", "closed"]
    while True:
        r = env["client"].get("/api/admin/phase", headers=h_registrar)
        current = r.json()["phase"]
        if current == target:
            return
        r = env["client"].post("/api/admin/phase/advance", headers=h_registrar)
        assert r.status_code == 200, r.text
