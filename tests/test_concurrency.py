"""并发行为测试：原子占座、同学生互斥、退课幂等。

这些用例直接并发调用服务层（每个线程一条独立 SQLite 连接），
验证的是数据库层条件更新（CAS）与进程内学生锁共同保证的不变量：
- 抢最后一个席位时不会超卖，其余请求全部进入候补；
- 同一学生并发提交时间冲突的课程时，只有一个能通过策略链；
- 同一选课记录被并发退课时，只释放一次席位。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import repositories as repo
from app.main import create_app
from app.models import ScheduleSlot, SectionIn
from app.runtime import CURRENT_SEMESTER
from app.services import CatalogService, DomainError, EnrollmentService

SEM = CURRENT_SEMESTER


def _slot(day: int, start: int, end: int) -> dict:
    return ScheduleSlot(day=day, start_min=start, end_min=end).model_dump()


def _build_world(app, n_students: int, cap: int):
    conn = app.state.db_factory()
    try:
        for i in range(n_students):
            repo.create_user(conn, f"stu{i}", "pw", f"Student {i}", "student",
                             major="CS", completed={"CS101", "MATH101"})
        cat = CatalogService(conn, app.state.bus)
        cat.create_course({"code": "CS101", "title": "程序设计基础",
                           "credits": 3, "department": "CS",
                           "prerequisites": []})
        cat.create_course({"code": "MATH101", "title": "高等数学",
                           "credits": 4, "department": "MATH",
                           "prerequisites": []})
        cat.create_course({"code": "CS201", "title": "数据结构",
                           "credits": 3, "department": "CS",
                           "prerequisites": ["CS101"]})
        cat.create_course({"code": "EE210", "title": "电路理论",
                           "credits": 3, "department": "EE",
                           "prerequisites": ["MATH101"]})
        # s1 与 s2 在周一 10:00–11:40 重叠，用于构造同学生的时间冲突
        s1 = cat.create_section(SectionIn(
            course_code="CS201", semester=SEM, section_no="01",
            instructor_name="A", capacity=cap, venue="A-101",
            slots=[_slot(1, 510, 650)]))
        s2 = cat.create_section(SectionIn(
            course_code="EE210", semester=SEM, section_no="01",
            instructor_name="B", capacity=5, venue="B-201",
            slots=[_slot(1, 600, 700)]))
        return s1["id"], s2["id"]
    finally:
        conn.close()


def _advance_to_add_drop(app) -> None:
    client = TestClient(app)
    r = client.post("/api/auth/login",
                    json={"username": "registrar", "password": "reg123"})
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    for _ in range(2):   # pre_enrollment -> allocation -> add_drop
        assert client.post("/api/admin/phase/advance",
                           headers=headers).status_code == 200


def _enroll(app, username: str, section_id: int) -> str:
    conn = app.state.db_factory()
    try:
        user = repo.get_user_by_username(conn, username)
        view = EnrollmentService(conn, app.state.bus).enroll(user, section_id)
        return view["state"]
    except DomainError as exc:
        return exc.code
    finally:
        conn.close()


def _drop(app, username: str, enrollment_id: int) -> str:
    conn = app.state.db_factory()
    try:
        user = repo.get_user_by_username(conn, username)
        view = EnrollmentService(conn, app.state.bus).drop(user, enrollment_id)
        return view["state"]
    except DomainError as exc:
        return exc.code
    finally:
        conn.close()


@pytest.fixture()
def world(tmp_path):
    app = create_app(tmp_path / "conc.db")
    return app


def test_concurrent_enroll_never_oversells(world):
    """8 名学生同时抢 1 个席位：恰好 1 人 ENROLLED，其余候补且序号不重号。"""
    s1, _ = _build_world(world, n_students=8, cap=1)
    _advance_to_add_drop(world)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda i: _enroll(world, f"stu{i}", s1), range(8)))

    assert all(r in ("ENROLLED", "WAITLISTED") for r in results), results
    assert results.count("ENROLLED") == 1, results
    assert results.count("WAITLISTED") == 7, results

    conn = world.state.db_factory()
    try:
        enrolled = repo.list_enrollments(conn, section_id=s1, state="ENROLLED")
        waitlisted = repo.list_enrollments(conn, section_id=s1,
                                           state="WAITLISTED")
        assert len(enrolled) == 1
        assert len(waitlisted) == 7
        # FIFO 序号在数据库内分配，并发下不会出现重号
        assert sorted(e["waitlist_seq"] for e in waitlisted) == list(range(1, 8))
        assert repo.count_enrolled(conn, s1) == 1
    finally:
        conn.close()


def test_same_student_conflicting_requests_are_serialized(world):
    """同一学生并发提交两门时间重叠的课：只有一门通过，另一门被冲突策略拒绝。"""
    s1, s2 = _build_world(world, n_students=1, cap=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda sid: _enroll(world, "stu0", sid), [s1, s2]))

    assert sorted(results) == ["TIMETABLE_CONFLICT", "VALIDATED"], results

    conn = world.state.db_factory()
    try:
        user = repo.get_user_by_username(conn, "stu0")
        active = [e for e in repo.list_enrollments(conn, student_id=user["id"])
                  if e["state"] in ("SUBMITTED", "VALIDATED", "ENROLLED",
                                    "WAITLISTED")]
        assert len(active) == 1
    finally:
        conn.close()


def test_concurrent_drop_releases_seat_once(world):
    """同一记录被并发退课：状态只迁移一次，席位释放事件只发一次。"""
    s1, _ = _build_world(world, n_students=1, cap=1)
    _advance_to_add_drop(world)
    assert _enroll(world, "stu0", s1) == "ENROLLED"

    conn = world.state.db_factory()
    try:
        user = repo.get_user_by_username(conn, "stu0")
        enr = repo.get_enrollment_by_student_section(conn, user["id"], s1)
    finally:
        conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: _drop(world, "stu0", enr["id"]), range(4)))

    assert all(r == "DROPPED" for r in results), results

    conn = world.state.db_factory()
    try:
        assert repo.get_enrollment(conn, enr["id"])["state"] == "DROPPED"
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM event_log WHERE type='section.seat_released'"
        ).fetchone()
        assert row["n"] == 1
    finally:
        conn.close()
