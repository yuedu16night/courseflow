"""培养方案审核与毕业规划测试：纯函数 + 端点集成。

覆盖范围：
- 审核三态（satisfied / partial / unmet）、N 选一、分类学分下限、总学分
- 毕业路线图的拓扑排序、学分装箱、不可行诊断、先修环
- 学期码 SP/FA 交替生成
- 端点：本人审核 / 路线图 / 方案列表 / 教务新建版本（版本化使旧版本失效）
- 权限：degree:read / degree:write 能力矩阵
- 智能体离线意图路由（毕业审核）
"""
from __future__ import annotations

import pytest

from app.degree import audit_requirements, next_semester_codes, plan_graduation
from tests.conftest import hdr


@pytest.fixture(autouse=True)
def _force_offline_mode(monkeypatch):
    """本模块的智能体用例走离线规则模式：不依赖网络与 LLM。"""
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)


# ----------------------------------------------------------- 纯函数：学期码
def test_next_semester_codes_alternates_sp_fa():
    assert next_semester_codes("2026FA", 4) == [
        "2026FA", "2027SP", "2027FA", "2028SP"]
    assert next_semester_codes("2026SP", 3) == [
        "2026SP", "2026FA", "2027SP"]
    assert next_semester_codes("2026FA", 0) == []


# --------------------------------------------------------------- 纯函数：审核
def _rule(category, rule_type, courses, min_credits=None, min_courses=None,
          sort_order=0):
    return {"category": category, "rule_type": rule_type, "courses": courses,
            "min_credits": min_credits, "min_courses": min_courses,
            "sort_order": sort_order}


def test_audit_reports_partial_unmet_and_credits():
    credits = {"CS101": 3, "CS201": 3, "MATH101": 4, "EE210": 3}
    rules = [
        _rule("专业必修", "all_of", ["CS101", "CS201"]),
        _rule("数学基础", "min_credits", ["MATH101"], min_credits=4.0),
        _rule("专业选修", "choose_n", ["CS201", "EE210"], min_courses=1),
    ]
    out = audit_requirements({"CS101", "MATH101"}, credits, rules, 20.0)
    by_cat = {r["category"]: r for r in out["rules"]}

    assert by_cat["专业必修"]["status"] == "partial"
    assert by_cat["专业必修"]["missing_courses"] == ["CS201"]
    assert by_cat["专业必修"]["reason_code"] == "DEGREE_REQ_PARTIAL"

    assert by_cat["数学基础"]["status"] == "satisfied"
    assert by_cat["数学基础"]["credits_earned"] == 4.0

    assert by_cat["专业选修"]["status"] == "unmet"
    assert by_cat["专业选修"]["required_courses"] == ["CS201"]   # N 选一取学分最低

    assert out["total_credits_earned"] == 7.0
    assert out["total_credits_required"] == 20.0
    assert out["status"] == "partial"
    assert {r["category"] for r in out["unmet_rules"]} == {"专业必修", "专业选修"}


def test_audit_all_satisfied():
    credits = {"A": 3, "B": 4, "C": 2}
    rules = [
        _rule("必修", "all_of", ["A", "B"]),
        _rule("选修", "choose_n", ["C"], min_courses=1),
        _rule("学分", "min_credits", ["A", "B", "C"], min_credits=9.0),
    ]
    out = audit_requirements({"A", "B", "C"}, credits, rules, 9.0)
    assert out["status"] == "satisfied"
    assert out["unmet_rules"] == []
    assert out["missing_courses"] == []
    assert out["total_credits_earned"] == 9.0
    assert out["total_credits_gap"] == 0.0


def test_audit_all_unmet_when_nothing_passed():
    out = audit_requirements(set(), {"A": 3}, [_rule("必修", "all_of", ["A"])], 3.0)
    assert out["status"] == "unmet"
    assert out["rules"][0]["status"] == "unmet"
    assert out["rules"][0]["reason_code"] == "DEGREE_REQ_UNMET"
    assert out["total_credits_earned"] == 0.0
    assert out["missing_courses"] == ["A"]


def test_audit_choose_n_partial_when_some_passed():
    credits = {"A": 3, "B": 3, "C": 3}
    rules = [_rule("选修", "choose_n", ["A", "B", "C"], min_courses=2)]
    out = audit_requirements({"A"}, credits, rules, 0.0)
    rule = out["rules"][0]
    assert rule["status"] == "partial"
    assert rule["met_count"] == 1
    assert rule["required_courses"] == ["B"]     # 还需 1 门，取字典序最靠前


# --------------------------------------------------------------- 纯函数：规划
def test_plan_graduation_topological_and_credit_packing():
    missing = [
        {"code": "CS101", "title": "Intro", "credits": 3},
        {"code": "CS201", "title": "DS", "credits": 3},
        {"code": "CS301", "title": "Algo", "credits": 3},
    ]
    graph = {"CS101": {"CS201"}, "CS201": {"CS301"}}
    out = plan_graduation(missing, graph, 6, "2026FA", 4)

    assert out["feasible"]
    pos = {c["code"]: i for i, sem in enumerate(out["semesters"])
           for c in sem["courses"]}
    assert pos["CS101"] < pos["CS201"] < pos["CS301"]
    assert all(sem["total_credits"] <= 6 for sem in out["semesters"])


def test_plan_graduation_packs_independent_courses():
    missing = [
        {"code": "A", "title": "A", "credits": 3},
        {"code": "B", "title": "B", "credits": 3},
        {"code": "C", "title": "C", "credits": 2},
    ]
    out = plan_graduation(missing, {}, 6, "2026FA", 4)
    assert out["feasible"]
    assert out["semesters"][0]["total_credits"] == 6.0      # A + B 用满
    assert out["semesters"][1]["courses"][0]["code"] == "C"


def test_plan_graduation_reports_infeasible_diagnostic():
    missing = [
        {"code": "A", "title": "A", "credits": 5},
        {"code": "B", "title": "B", "credits": 5},
    ]
    out = plan_graduation(missing, {}, 4, "2026FA", 1)
    assert not out["feasible"]
    assert out["semesters"] == []
    assert out["diagnostics"]
    assert all(d["code"] == "SEMESTER_LIMIT" for d in out["diagnostics"])


def test_plan_graduation_detects_prerequisite_cycle():
    missing = [{"code": "A", "title": "A", "credits": 3},
               {"code": "B", "title": "B", "credits": 3}]
    out = plan_graduation(missing, {"A": {"B"}, "B": {"A"}}, 10, "2026FA", 4)
    assert not out["feasible"]
    assert any(d["code"] == "PREREQ_CYCLE" for d in out["diagnostics"])


# ------------------------------------------------------------------ 端点集成
def test_degree_audit_endpoint(env):
    r = env["client"].get("/api/degree/me", headers=hdr(env, "alice"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["major"] == "CS"
    assert body["status"] == "partial"
    assert body["total_credits_earned"] == 7.0
    assert body["total_credits_required"] == 120.0
    assert "CS201" in body["missing_courses"]
    statuses = {r["category"]: r["status"] for r in body["rules"]}
    assert statuses["专业必修"] == "partial"
    assert statuses["数学基础"] == "satisfied"
    assert statuses["专业选修"] == "unmet"


def test_degree_plan_endpoint(env):
    r = env["client"].get("/api/degree/me/plan", headers=hdr(env, "alice"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["missing_courses"] == ["CS201"]
    assert body["feasible"]
    assert body["start_semester"] == "2026FA"
    assert body["semesters"][0]["courses"][0]["code"] == "CS201"


def test_degree_audit_requires_active_program(env):
    r = env["client"].get("/api/degree/me", headers=hdr(env, "dave"))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "DEGREE_PROGRAM_NOT_FOUND"


def test_student_can_list_requirements(env):
    r = env["client"].get("/api/degree/requirements", headers=hdr(env, "alice"))
    assert r.status_code == 200
    assert any(p["major"] == "CS" and p["active"] for p in r.json())


def test_registrar_creates_new_requirement_version(env):
    payload = {
        "major": "CS", "total_credits_min": 130.0, "label": "计算机科学 v2",
        "rules": [{"category": "专业必修", "rule_type": "all_of",
                   "courses": ["CS101", "CS201", "MATH101"],
                   "min_credits": None, "min_courses": None}],
    }
    r = env["client"].post("/api/degree/requirements",
                           headers=hdr(env, "registrar"), json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] >= 2
    assert body["total_credits_min"] == 130.0

    listing = env["client"].get("/api/degree/requirements",
                                headers=hdr(env, "alice")).json()
    active = [p for p in listing if p["major"] == "CS" and p["active"]]
    assert len(active) == 1 and active[0]["version"] == body["version"]


def test_requirement_rejects_unknown_course(env):
    payload = {
        "major": "CS", "total_credits_min": 100.0, "label": "bad",
        "rules": [{"category": "必修", "rule_type": "all_of",
                   "courses": ["NOPE101"], "min_credits": None,
                   "min_courses": None}],
    }
    r = env["client"].post("/api/degree/requirements",
                           headers=hdr(env, "registrar"), json=payload)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "COURSE_NOT_FOUND"


def test_student_cannot_write_requirement(env):
    payload = {
        "major": "CS", "total_credits_min": 100.0, "label": "hack",
        "rules": [{"category": "必修", "rule_type": "all_of",
                   "courses": ["CS101"], "min_credits": None,
                   "min_courses": None}],
    }
    r = env["client"].post("/api/degree/requirements",
                           headers=hdr(env, "alice"), json=payload)
    assert r.status_code == 403


def test_instructor_cannot_read_degree(env):
    r = env["client"].get("/api/degree/me", headers=hdr(env, "carol"))
    assert r.status_code == 403


# --------------------------------------------------------------- 智能体路由
def test_chat_degree_intent_uses_audit_tool(env):
    body = env["client"].post(
        "/api/agent/chat", headers=hdr(env, "alice"),
        json={"message": "我还差什么课才能毕业", "history": []}).json()
    assert [t["tool"] for t in body["tool_trace"]] == ["audit_degree"]
    assert "毕业学分审核" in body["reply"]
