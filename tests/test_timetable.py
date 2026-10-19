"""无冲突课表规划测试：纯函数 + 端点集成。

覆盖范围：
- 无冲突分配成功、周课表网格
- 必然冲突时的可读诊断（两门课全部教学班两两冲突）
- 缺少教学班的诊断
- 余席偏好（优先余席多的教学班）
- 确定性（同输入 ⇒ 同输出）
- 端点：/api/timetable/plan 的成功、冲突、未知课程、权限
- 智能体离线意图路由（排课表）
"""
from __future__ import annotations

import pytest

from app.timetable import plan_timetable
from tests.conftest import hdr


@pytest.fixture(autouse=True)
def _force_offline_mode(monkeypatch):
    """智能体用例走离线规则模式：不依赖网络与 LLM。"""
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)


def mk(sec_id, no, day, start, end, cap=30, venue="V"):
    return {"id": sec_id, "section_no": no, "venue": venue, "capacity": cap,
            "slots": [{"day": day, "start_min": start, "end_min": end}]}


# --------------------------------------------------------------- 纯函数
def test_plan_timetable_conflict_free_success():
    sections = {
        "CS201": [mk(1, "01", 1, 510, 650)],
        "EE210": [mk(2, "01", 3, 510, 650)],
    }
    out = plan_timetable(["CS201", "EE210"], sections, {})
    assert out["feasible"]
    assign = {a["course"]: a for a in out["assignments"]}
    assert assign["CS201"]["section_id"] == 1
    assert assign["EE210"]["section_id"] == 2
    assert len(out["weekly_grid"]) == 2
    assert out["diagnostics"] == []


def test_plan_timetable_reports_guaranteed_conflict():
    sections = {
        "A": [mk(1, "01", 1, 510, 650)],
        "B": [mk(2, "01", 1, 600, 700)],       # 与 A 重叠
    }
    out = plan_timetable(["A", "B"], sections, {})
    assert not out["feasible"]
    assert out["assignments"] == []
    conflict = [d for d in out["diagnostics"] if d["code"] == "TIMETABLE_CONFLICT"]
    assert conflict
    assert "A" in conflict[0]["message"] and "B" in conflict[0]["message"]


def test_plan_timetable_reports_missing_section():
    out = plan_timetable(["A", "B"], {"A": [], "B": [mk(2, "01", 1, 510, 650)]}, {})
    assert not out["feasible"]
    assert any(d["code"] == "SECTION_UNKNOWN" for d in out["diagnostics"])


def test_plan_timetable_prefers_more_free_seats():
    sections = {"A": [mk(1, "01", 1, 510, 650, cap=10),
                      mk(2, "02", 1, 510, 650, cap=30)]}
    out = plan_timetable(["A"], sections, {1: 9, 2: 0})
    assert out["assignments"][0]["section_id"] == 2
    assert out["assignments"][0]["free_seats"] == 30


def test_plan_timetable_is_deterministic():
    sections = {
        "A": [mk(1, "01", 1, 510, 650), mk(2, "02", 3, 510, 650)],
        "B": [mk(3, "01", 1, 510, 650), mk(4, "02", 5, 510, 650)],
        "C": [mk(5, "01", 2, 510, 650)],
    }
    first = plan_timetable(["A", "B", "C"], sections, {})
    second = plan_timetable(["A", "B", "C"], sections, {})
    assert first == second


# ------------------------------------------------------------------ 端点
def test_timetable_plan_endpoint_success(env):
    r = env["client"].post("/api/timetable/plan", headers=hdr(env, "alice"),
                           json={"course_codes": ["CS201", "MATH101"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"]
    assert {a["course"] for a in body["assignments"]} == {"CS201", "MATH101"}


def test_timetable_plan_endpoint_reports_conflict(env):
    r = env["client"].post("/api/timetable/plan", headers=hdr(env, "alice"),
                           json={"course_codes": ["CS201", "EE210"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["feasible"]
    assert body["diagnostics"]


def test_timetable_plan_unknown_course(env):
    r = env["client"].post("/api/timetable/plan", headers=hdr(env, "alice"),
                           json={"course_codes": ["NOPE101"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "COURSE_NOT_FOUND"


def test_timetable_plan_requires_student_capability(env):
    r = env["client"].post("/api/timetable/plan", headers=hdr(env, "registrar"),
                           json={"course_codes": ["CS201"]})
    assert r.status_code == 403


# --------------------------------------------------------------- 智能体路由
def test_chat_plan_timetable_intent(env):
    body = env["client"].post(
        "/api/agent/chat", headers=hdr(env, "alice"),
        json={"message": "帮我排课表 CS201 MATH101", "history": []}).json()
    assert [t["tool"] for t in body["tool_trace"]] == ["plan_timetable"]
    assert "CS201" in body["reply"]
