"""智能体层测试：方案 A（确定性规划）+ 方案 B（编排与工具面）。

覆盖范围：
- 规划智能体的约束正确性（先修 DAG / 课表冲突 / 学分上限 / 重复与重修）
- 规划的确定性（同输入 ⇒ 同输出，可复现、可审计）
- 工具层的只读语义与写操作「待确认」语义
- 确认后写操作仍然走正常服务层，约束链与 RBAC 全量生效
- 智能体交互进入只追加事件日志

Seed world 见 tests/conftest.py。所有用例都不依赖网络与 LLM：
未配置 AGENT_LLM_API_KEY 时编排器走离线规则模式。
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import advance_to, hdr


@pytest.fixture(autouse=True)
def _force_offline_mode(monkeypatch):
    """本模块全部用例都在离线规则模式下运行：不依赖网络，也不依赖 LLM。"""
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)


# ---------------------------------------------------------------- 工具函数
def chat(env, who, message, history=None):
    return env["client"].post("/api/agent/chat", headers=hdr(env, who),
                              json={"message": message, "history": history or []})


def plan(env, who, **payload):
    return env["client"].post("/api/agent/plan", headers=hdr(env, who),
                              json=payload)


def act(env, who, tool, **args):
    return env["client"].post("/api/agent/actions", headers=hdr(env, who),
                              json={"tool": tool, "args": args})


def add_course(env, code, credits, prerequisites=()):
    conn = env["app"].state.db_factory()
    try:
        conn.execute(
            "INSERT INTO courses (code, title, credits, department,"
            " prerequisites, description, catalog_version, active)"
            " VALUES (?,?,?,?,?,'',1,1)",
            (code, f"{code} Title", credits, "Test", json.dumps(list(prerequisites))),
        )
        conn.commit()
    finally:
        conn.close()


def add_section(env, code, day, start, end, capacity=30, section_no="01"):
    r = env["client"].post("/api/sections", headers=hdr(env, "registrar"),
                           json={"course_code": code, "semester": "2026FA",
                                 "section_no": section_no, "instructor_name": "Staff",
                                 "capacity": capacity, "venue": "GEN-1",
                                 "slots": [{"day": day, "start_min": start,
                                            "end_min": end}]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ------------------------------------------------------- 方案 A：规划智能体
def test_planner_only_offers_eligible_courses(env):
    """alice 已通过 CS101+MATH101 ⇒ 可选 CS201 与 EE210；已通过课程不再出现。"""
    body = plan(env, "alice").json()
    offered = {i["course"] for p in body["plans"] for i in p["picks"]}
    assert offered <= {"CS201", "EE210"}
    assert "CS101" not in offered and "MATH101" not in offered
    assert body["completed_courses"] == ["CS101", "MATH101"]


def test_planner_never_produces_timetable_conflicts(env):
    """s1(周一) 与 s2(周一) 区间重叠 ⇒ 任何方案都不会同时包含 CS201 与 EE210。"""
    body = plan(env, "alice", top_k=10).json()
    assert body["plans"]
    for p in body["plans"]:
        codes = {i["course"] for i in p["picks"]}
        assert not {"CS201", "EE210"} <= codes


def test_planner_respects_credit_ceiling(env):
    """插入 4 学分的 PHY101（周三，不冲突），max_credits=6 时方案不得超过上限。"""
    add_course(env, "PHY101", 4)
    add_section(env, "PHY101", day=3, start=510, end=650)

    body = plan(env, "alice", max_credits=6, top_k=10).json()
    assert body["max_credits"] == 6
    assert body["plans"]
    for p in body["plans"]:
        assert p["total_credits"] <= 6
    # 4 学分的 PHY101 是学分利用率最高的单门选择
    assert body["plans"][0]["picks"][0]["course"] == "PHY101"


def test_planner_is_deterministic(env):
    """同输入 ⇒ 完全一致的输出（可复现、可写进报告作为证据）。"""
    first = plan(env, "alice", top_k=5).json()
    second = plan(env, "alice", top_k=5).json()
    assert first == second


def test_planner_reports_unmet_prerequisites(env):
    """CS301 要求先修 CS201 ⇒ 进入 blocked_by_prerequisites 而非方案。"""
    add_course(env, "CS301", 3, prerequisites=["CS201"])
    body = plan(env, "alice").json()
    blocked = {b["course"]: b for b in body["blocked_by_prerequisites"]}
    assert "CS301" in blocked
    assert blocked["CS301"]["missing_prerequisites"] == ["CS201"]
    assert blocked["CS301"]["reason_code"] == "PREREQ_MISSING"


def test_planner_explains_why_courses_were_excluded(env):
    """第 1 方案之外的课程必须给出原因：时间冲突或排序更低。"""
    body = plan(env, "alice", top_k=1).json()
    excluded = {e["course"]: e["reason_code"] for e in body["excluded_from_best_plan"]}
    assert excluded.get("EE210") in {"TIMETABLE_CONFLICT", "LOWER_RANK"}


def test_planner_skips_courses_already_taken_this_semester(env):
    """alice 已在预选阶段提交 CS201 志愿 ⇒ 不再重复规划该课程。"""
    env["client"].post("/api/enrollments", headers=hdr(env, "alice"),
                       json={"section_id": env["s1"]})
    body = plan(env, "alice").json()
    assert "CS201" in body["current_courses"]
    offered = {i["course"] for p in body["plans"] for i in p["picks"]}
    assert "CS201" not in offered


def test_planner_handles_student_without_prerequisites(env):
    """dave 没通过任何课程 ⇒ 只能选无先修要求的 MATH101，其余给出先修缺口。"""
    body = plan(env, "dave").json()
    offered = {i["course"] for p in body["plans"] for i in p["picks"]}
    assert offered == {"MATH101"}
    blocked = {b["course"] for b in body["blocked_by_prerequisites"]}
    assert {"CS201", "EE210"} <= blocked
    assert "CS101" in body["not_offered"]      # 目录里有，但本学期未开教学班


# --------------------------------------------- 方案 B：工具面与编排器
def test_agent_endpoints_require_student_role(env):
    """教师角色缺少 agent:use 能力 ⇒ 403（能力矩阵而非硬编码角色判断）。"""
    assert env["client"].post("/api/agent/plan",
                              headers=hdr(env, "carol"), json={}).status_code == 403
    assert chat(env, "carol", "推荐下学期的选课方案").status_code == 403


def test_agent_status_reports_offline_mode_without_api_key(env, monkeypatch):
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)
    body = env["client"].get("/api/agent/status", headers=hdr(env, "alice")).json()
    assert body == {"enabled": False, "mode": "offline", "model": None}


def test_chat_plan_intent_uses_planner_tool(env):
    r = chat(env, "alice", "推荐下学期的选课方案")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "offline"
    assert [t["tool"] for t in body["tool_trace"]] == ["plan_courses"]
    assert "方案 1" in body["reply"] and "CS201" in body["reply"]
    assert body["pending_actions"] == []


def test_chat_check_course_surfaces_reason_code(env):
    """dave 查询 CS201：工具返回机器可读原因码，回答保留该原因码。"""
    body = chat(env, "dave", "CS201 能选吗").json()
    assert [t["tool"] for t in body["tool_trace"]] == ["check_course"]
    assert "PREREQ_MISSING" in body["reply"]


def test_chat_check_course_passes_for_eligible_student(env):
    body = chat(env, "alice", "CS201 能选吗").json()
    assert "可以选" in body["reply"]
    assert "OK" in body["reply"]


def test_chat_timetable_intent(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    env["client"].post("/api/enrollments", headers=hdr(env, "alice"),
                       json={"section_id": env["s1"]})
    body = chat(env, "alice", "看下我的课表").json()
    assert [t["tool"] for t in body["tool_trace"]] == ["my_timetable"]
    assert "CS201" in body["reply"]


def test_chat_unknown_intent_falls_back_to_help(env):
    """与选课无关的问题：给出帮助，并且帮助里带上该生当前的真实状态。"""
    body = chat(env, "alice", "今天天气怎么样").json()
    assert "我可以帮你" in body["reply"]
    assert "你本学期还没有提交任何选课申请" in body["reply"]


@pytest.mark.parametrize("message", [
    "下学期帮我排一下",
    "选啥好",
    "怎么选课比较合适",
    "帮我搭配一下课程组合",
])
def test_chat_understands_colloquial_plan_phrasings(env, message):
    """非标准措辞也应命中规划工具（加权意图评分，而非硬关键词命中）。"""
    body = chat(env, "alice", message).json()
    assert [t["tool"] for t in body["tool_trace"]] == ["plan_courses"], message


@pytest.mark.parametrize("message", [
    "这门课还能报吗 CS201",
    "CS201 还有名额吗",
    "CS201 满了没",
])
def test_chat_understands_colloquial_check_phrasings(env, message):
    body = chat(env, "alice", message).json()
    assert [t["tool"] for t in body["tool_trace"]] == ["check_course"], message


@pytest.mark.parametrize("message", ["什么时候上课", "每周上什么课"])
def test_chat_understands_colloquial_timetable_phrasings(env, message):
    body = chat(env, "alice", message).json()
    assert [t["tool"] for t in body["tool_trace"]] == ["my_timetable"], message


def test_chat_bare_course_code_falls_back_to_check(env):
    """只丢一个课程代码、没有动词：「CS201」也应被理解为查询而不是帮助文本。"""
    body = chat(env, "alice", "CS201").json()
    assert [t["tool"] for t in body["tool_trace"]] == ["check_course"]
    assert "可以选" in body["reply"]


def test_help_reports_current_enrollment_state(env):
    """兜底帮助不再是静态文本：带出该生当前选课记录。"""
    env["client"].post("/api/enrollments", headers=hdr(env, "alice"),
                       json={"section_id": env["s1"]})
    body = chat(env, "alice", "嗯").json()
    assert "你本学期当前的选课记录" in body["reply"]
    assert "CS201.01" in body["reply"]
    assert "已校验" in body["reply"]


# --------------------------------------------- 写操作：提案 → 确认 → 执行
def test_write_tool_only_proposes_and_does_not_persist(env):
    """「帮我选 CS201」只生成待确认提案，数据库不得有任何写入。"""
    body = chat(env, "alice", "帮我选 CS201").json()
    assert [t["status"] for t in body["tool_trace"]][-1] == "requires_confirmation"
    assert body["pending_actions"]
    action = body["pending_actions"][0]
    assert action["tool"] == "submit_enrollment"
    assert action["args"]["section_id"] == env["s1"]
    assert "待确认" in body["reply"] or "确认" in body["reply"]

    mine = env["client"].get("/api/enrollments/me", headers=hdr(env, "alice")).json()
    assert mine == []                                     # 未落库


def test_confirmed_action_executes_through_the_service_layer(env):
    """用户确认后，写操作走正常服务层，状态机与策略链全量生效。"""
    proposal = chat(env, "alice", "帮我选 CS201").json()["pending_actions"][0]
    r = act(env, "alice", proposal["tool"], **proposal["args"])
    assert r.status_code == 200
    assert r.json()["state"] == "VALIDATED"               # 预选阶段：容量无关的志愿

    mine = env["client"].get("/api/enrollments/me", headers=hdr(env, "alice")).json()
    assert len(mine) == 1 and mine[0]["section"] == "CS201.01"


def test_confirmed_action_cannot_bypass_constraint_policies(env):
    """dave 即使确认选课提案，也必须被先修策略链拒绝（智能体无法绕过约束）。"""
    r = act(env, "dave", "submit_enrollment", section_id=env["s1"])
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "PREREQ_MISSING"


def test_action_endpoint_rejects_unknown_write_tool(env):
    r = env["client"].post("/api/agent/actions", headers=hdr(env, "alice"),
                           json={"tool": "run_allocation", "args": {}})
    assert r.status_code == 422                            # 白名单之外的写工具被拒绝


def test_chat_prepares_drop_proposal(env):
    """口语化的退课请求（「把 CS201 退了」）应生成退课提案，而不是选课提案。"""
    env["client"].post("/api/enrollments", headers=hdr(env, "alice"),
                       json={"section_id": env["s1"]})
    body = chat(env, "alice", "把 CS201 退了").json()
    last = body["tool_trace"][-1]
    assert last["tool"] == "drop_enrollment"
    assert last["status"] == "requires_confirmation"
    assert body["pending_actions"][0]["args"]["enrollment_id"]


def test_confirmed_drop_goes_through_the_service_layer(env):
    advance_to(env, "add_drop", hdr(env, "registrar"))
    created = env["client"].post("/api/enrollments", headers=hdr(env, "alice"),
                                 json={"section_id": env["s1"]}).json()
    proposal = chat(env, "alice", "退掉 CS201").json()["pending_actions"][0]
    assert proposal["args"]["enrollment_id"] == created["id"]

    r = act(env, "alice", proposal["tool"], **proposal["args"])
    assert r.status_code == 200
    mine = env["client"].get("/api/enrollments/me", headers=hdr(env, "alice")).json()
    assert all(e["state"] != "ENROLLED" for e in mine)


def test_chat_drop_without_course_code_asks_which_one(env):
    env["client"].post("/api/enrollments", headers=hdr(env, "alice"),
                       json={"section_id": env["s1"]})
    body = chat(env, "alice", "退课").json()
    assert "告诉我要退哪一门" in body["reply"]
    assert body["pending_actions"] == []                   # 信息不足时不猜


def test_chat_drop_when_nothing_enrolled(env):
    body = chat(env, "alice", "退掉 CS201").json()
    assert "没有可退的选课记录" in body["reply"]
    assert body["pending_actions"] == []


def test_action_endpoint_validates_arguments(env):
    r = act(env, "alice", "submit_enrollment")
    assert r.status_code == 422


# ------------------------------------------------------------- 可审计性
def test_agent_turns_are_recorded_in_the_event_log(env):
    chat(env, "alice", "推荐下学期的选课方案")
    events = env["client"].get("/api/admin/events", headers=hdr(env, "admin")).json()
    agent_events = [e for e in events if e["type"] == "agent.turn"]
    assert agent_events
    payload = json.loads(agent_events[0]["payload"])
    assert payload["mode"] == "offline"
    assert payload["tools"] == ["plan_courses"]
    assert payload["student"] == "alice"
