"""AgentOrchestrator（方案 B）：LLM 工具调用编排 + 离线规则降级。

职责边界（关键设计）：
- 编排器只负责「理解意图 → 选择工具 → 组织语言」，**不实现任何领域
  规则**；规划能力全部来自方案 A（PlannerAgent），约束校验全部来自
  既有的策略链。
- 写操作只生成待确认提案（pending_actions），由用户确认后经正常
  REST 端点执行，智能体无法绕过约束系统与 RBAC。
- 未配置 LLM（或调用失败）时自动降级为确定性的离线规则模式，系统
  在无网络环境下功能完整、行为可复现，同时也让测试不依赖外部服务。
"""
from __future__ import annotations

import json
import re

from app import repositories as repo
from app.agent.llm import ChatLLM, LLMError
from app.agent.planner import format_slots
from app.agent.tools import ToolRegistry
from app.events import EVT_AGENT_TURN

MAX_HISTORY_TURNS = 8
MAX_TOOL_STEPS = 6

_DAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_CODE_RE = re.compile(r"\b([A-Za-z]{2,4})\s?(\d{2,4})\b")

# 意图识别采用「加权同义词评分」而非「命中即路由」：把消息里出现的每个模式
# 的权重累加，取总分最高的意图。这样「下学期帮我排一下」「选啥好」「把 CS201 退了」
# 这类口语变体也能命中，同时保持完全确定性（无随机、无模型参与）。
_INTENTS: dict[str, tuple[tuple[str, float], ...]] = {
    "timetable": (
        ("课表", 3.0), ("时间表", 3.0), ("上课时间", 3.0), ("什么时候上课", 3.0),
        ("课程安排", 2.0), ("每周上", 1.5), ("我的课", 1.0),
    ),
    "drop": (
        ("退课", 3.0), ("退选", 3.0), ("退掉", 3.0), ("取消选课", 3.0),
        ("退了", 2.5), ("drop", 2.5), ("撤销", 2.0), ("不要了", 2.0), ("退", 1.0),
    ),
    "enroll": (
        ("帮我选", 3.0), ("帮我报", 3.0), ("帮我提交", 3.0), ("提交选课", 3.0),
        ("给我选", 2.5), ("我要选", 2.5), ("报名", 2.5), ("报上", 2.5),
        ("我想选", 2.0), ("选上", 2.0), ("加上", 2.0),
    ),
    "plan": (
        ("选课方案", 3.0), ("规划", 2.5), ("怎么选", 2.5), ("选什么", 2.5),
        ("选啥", 2.5), ("咋选", 2.5), ("排一下", 2.0), ("帮我排", 2.0),
        ("方案", 2.0), ("推荐", 2.0), ("组合", 2.0), ("搭配", 1.5), ("安排", 1.5),
    ),
    "check": (
        ("能不能选", 3.0), ("可以选吗", 3.0), ("能选吗", 3.0), ("可选吗", 3.0),
        ("还能报吗", 3.0), ("能报吗", 3.0), ("有没有位置", 2.5), ("还有名额", 2.5),
        ("先修", 2.5), ("冲突", 2.5), ("符合条件", 2.5), ("能修", 2.5),
        ("够格", 2.0), ("余量", 2.0), ("满了", 2.0),
    ),
    "degree": (
        ("毕业审核", 3.5), ("毕业", 3.0), ("培养方案", 2.5), ("还差", 2.5),
        ("差什么", 2.5), ("学分", 2.5), ("学位", 2.0), ("审核", 1.5),
    ),
    "plan_timetable": (
        ("排课表", 3.5), ("课表规划", 3.5), ("排课", 2.5), ("无冲突课表", 3.5),
    ),
}

#: 同分时的确定性优先级：具体意图优先于泛化意图，动作意图优先于查询意图
_INTENT_PRIORITY = ("timetable", "drop", "enroll", "plan", "check",
                    "plan_timetable", "degree")
_LOW_CONFIDENCE = 1.0

_ACTIVE_STATES = {"SUBMITTED", "VALIDATED", "ENROLLED", "WAITLISTED"}
_STATE_ZH = {
    "SUBMITTED": "已提交", "VALIDATED": "已校验", "ENROLLED": "已选上",
    "WAITLISTED": "候补中", "REJECTED": "已拒绝", "CANCELLED": "已取消",
    "EXPIRED": "已过期", "DROPPED": "已退课",
}


def _classify(text: str) -> str | None:
    """返回加权得分最高的意图；得分低于阈值时返回 None，交给兜底逻辑。"""
    lowered = text.lower()
    scores: dict[str, float] = {}
    for intent, patterns in _INTENTS.items():
        total = sum(weight for pattern, weight in patterns if pattern in lowered)
        if total:
            scores[intent] = total
    if not scores:
        return None
    best = max(scores.values())
    if best < _LOW_CONFIDENCE:
        return None
    for intent in _INTENT_PRIORITY:          # 同分时按固定优先级取，保证确定性
        if scores.get(intent) == best:
            return intent
    return None


SYSTEM_PROMPT = """你是 CourseFlow（高校选课与排课编排系统）的智能选课助手。

当前用户：{name}（用户名 {username}，角色 {role}，专业 {major}，学分上限 {max_credits}）
当前学期：{semester}，所处阶段：{phase}
该生已通过课程：{completed}

工作方式：
- 事实必须来自工具调用，绝不凭空编造课程、教学班、名额、时间或选课结果。
- 选课规划交给工具 plan_courses（它会在全部约束下枚举可行组合），
  不要自己心算组合；单门课程是否可选交给工具 check_course。
- 复述工具结果时使用中文，并保留其中的机器可读原因码（如 PREREQ_MISSING、
  TIMETABLE_CONFLICT、CREDIT_LIMIT），便于用户与审计对照。

硬性规则：
1. 绝不声称已经完成选课或退课。发起写操作时，**必须实际调用写工具**
   （submit_enrollment / drop_enrollment），由工具返回提案、界面据此展示「确认执行」按钮；
   **不要只用文字描述一个提案**——文字描述不会产生按钮，用户无法确认。
   写工具返回 requires_confirmation 即表示提案已就绪，此时才向用户说明"已生成待确认提案"。
2. 用户已经明确要求选课或退课时（如"帮我选 CS201"），**直接调用写工具生成提案**，
   不要再反问"要不要提交"。生成提案≠执行：提案只是草案，用户点击确认按钮才是授权点，
   所以这一步可以放心做。仅在目标不明确时才追问（例如只说了"退课"却没说是哪一门）。
3. 只能访问当前用户自己的数据；涉及他人数据或超出权限的请求，礼貌拒绝。
4. 用户的问题与选课无关时，简短回应并说明你能做什么。
5. 回答简洁：先给结论，再给必要的理由与下一步建议，不要长篇铺陈。
6. 使用纯文本：不要输出 Markdown 语法（**、`、# 等），换行与短横线列举即可。
"""


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _extract_course_code(text: str) -> str | None:
    match = _CODE_RE.search(text or "")
    return f"{match.group(1)}{match.group(2)}".upper() if match else None


def _extract_course_codes(text: str) -> list[str]:
    """抽取文本中出现的全部课程代码（去重、保持首次出现顺序）。"""
    out: list[str] = []
    for match in _CODE_RE.finditer(text or ""):
        code = f"{match.group(1)}{match.group(2)}".upper()
        if code not in out:
            out.append(code)
    return out


def _hh(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _sanitize_history(history) -> list[dict]:
    """客户端传入的历史不可信：只保留最近若干轮的 user/assistant 文本。"""
    out: list[dict] = []
    for item in (history or [])[-MAX_HISTORY_TURNS:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content})
    return out


# ---------------------------------------------------------------------------
# 离线规则模式
# ---------------------------------------------------------------------------
def _fmt_plan(result: dict) -> str:
    plans = result.get("plans") or []
    if not plans:
        lines = [f"当前没有可行的选课组合（学期 {result['semester']}，"
                 f"学分上限 {result['max_credits']}）。"]
    else:
        lines = [f"为你生成了 {len(plans)} 套可行方案"
                 f"（学期 {result['semester']}，阶段 {result['phase']}，"
                 f"学分上限 {result['max_credits']}）："]
        for plan in plans:
            picks = "、".join(f"{i['course']}.{i['section_no']}" for i in plan["picks"])
            lines.append("")
            lines.append(f"【方案 {plan['rank']}】{picks}　合计 {plan['total_credits']:g} 学分")
            for item in plan["picks"]:
                lines.append(f"  · {item['course']}《{item['title']}》"
                             f"（{item['credits']:g} 学分，余 {item['free_seats']} 席）")
                for reason in item["reasons"]:
                    lines.append(f"      - {reason}")
            if plan["unlocks"]:
                lines.append(f"  · 修完后可解锁：{'、'.join(plan['unlocks'])}")
            lines.append(f"  · {plan['explanation']}")
    if result.get("excluded_from_best_plan"):
        lines.append("")
        lines.append("未纳入第 1 方案的课程：")
        for item in result["excluded_from_best_plan"]:
            lines.append(f"  · {item['course']} — [{item['reason_code']}] {item['message']}")
    if result.get("blocked_by_prerequisites"):
        lines.append("")
        lines.append("暂不可选（先修未满足）：")
        for item in result["blocked_by_prerequisites"]:
            lines.append(f"  · {item['course']}《{item['title']}》— "
                         f"缺少 {'、'.join(item['missing_prerequisites'])}")
    return "\n".join(lines)


def _fmt_check(result: dict) -> str:
    code, title = result["course"], result.get("title", "")
    if not result.get("options"):
        return f"{code}《{title}》：不可选 — [{result.get('reason_code')}] {result.get('message')}"
    head = ("可以选" if result["selectable"] else "当前不可选")
    lines = [f"{code}《{title}》（{result.get('credits'):g} 学分）{head}。"
             f"先修要求：{'、'.join(result.get('prerequisites') or []) or '无'}"]
    for option in result["options"]:
        mark = "可选" if option["selectable"] else "不可选"
        lines.append(f"  · 教学班 {option['section_no']}（{option['slots_text']}，"
                     f"余 {option['free_seats']} 席）：{mark} — "
                     f"[{option['reason_code']}] {option['message']}")
    return "\n".join(lines)


def _fmt_timetable(rows: list[dict]) -> str:
    if not rows:
        return "本学期还没有已选上的课程，课表为空。"
    lines = ["本学期课表（已选上课程）："]
    for row in rows:
        lines.append(f"  · {_DAY_ZH[row['day'] - 1]} "
                     f"{_hh(row['start_min'])}-{_hh(row['end_min'])} "
                     f"{row['course']}.{row['section']}（{row['venue']}）")
    return "\n".join(lines)


_DEGREE_STATUS_ZH = {"satisfied": "已满足", "partial": "部分满足", "unmet": "未满足"}


def _fmt_degree(result: dict) -> str:
    if not result:
        return "暂时无法读取培养方案。"
    program = result.get("program") or {}
    lines = [f"毕业学分审核（专业 {result.get('major')}，"
             f"培养方案 {program.get('label', '')} v{program.get('version', '')}）："
             f"整体状态 {_DEGREE_STATUS_ZH.get(result.get('status'), result.get('status'))}。",
             f"已获学分 {result.get('total_credits_earned', 0):g} / "
             f"毕业要求 {result.get('total_credits_required', 0):g} 学分"
             f"（还差 {result.get('total_credits_gap', 0):g} 学分）。"]
    for rule in result.get("rules", []):
        status = _DEGREE_STATUS_ZH.get(rule["status"], rule["status"])
        detail = ""
        if rule["status"] != "satisfied":
            if rule["required_courses"]:
                detail = f"，还差 {'、'.join(rule['required_courses'])}"
            elif rule["credits_needed"]:
                detail = f"，还差 {rule['credits_needed']:g} 学分"
        lines.append(f"  · 【{rule['category']}·{rule['rule_type']}】"
                     f"{status} — [{rule['reason_code']}]{detail}")
    return "\n".join(lines)


def _fmt_plan_timetable(result: dict) -> str:
    if not result:
        return "暂时无法进行课表规划。"
    if not result.get("feasible"):
        lines = [f"无法为这些课程排出无冲突课表（学期 {result.get('semester')}）："]
        for diag in result.get("diagnostics", []):
            lines.append(f"  · [{diag['code']}] {diag['message']}")
        return "\n".join(lines)
    lines = [f"已为 {len(result['assignments'])} 门课各选出无冲突的教学班"
             f"（学期 {result.get('semester')}）："]
    for item in result["assignments"]:
        slots = "，".join(
            f"{_DAY_ZH[s['day'] - 1]} {_hh(s['start_min'])}-{_hh(s['end_min'])}"
            for s in item["slots"])
        lines.append(f"  · {item['course']}.{item['section_no']}（{item['venue']}，"
                     f"{slots}，余 {item['free_seats']} 席）")
    return "\n".join(lines)


class OfflineResponder:
    """未配置 LLM 时的确定性替代：加权意图评分 + 直接调用工具。

    与 LLM 模式能力等价（同一套工具、同样的写操作闸门），差别只在
    「理解意图」这一步：这里用可解释的加权关键词打分代替模型推理，
    因此同样的说法会被路由到同样的工具，行为完全可复现。
    """

    def __init__(self, tools: ToolRegistry):
        self.tools = tools
        self.trace: list[dict] = []

    def _call(self, name: str, args: dict | None = None) -> dict:
        out = self.tools.execute(name, args or {})
        self.trace.append({"tool": name, "arguments": args or {},
                           "status": out["status"], "result": out})
        return out

    def _done(self, reply: str) -> dict:
        pending = [{**t["result"]["action"], "summary": t["result"]["summary"]}
                   for t in self.trace
                   if t["status"] == "requires_confirmation"]
        return {"reply": reply, "trace": self.trace, "pending": pending}

    def respond(self, message: str) -> dict:
        text = (message or "").strip()
        if not text:
            return self._done(self._help_with_state())

        code = _extract_course_code(text)
        intent = _classify(text)

        if intent == "timetable":
            out = self._call("my_timetable")
            return self._done(_fmt_timetable(out.get("result") or []))

        if intent == "drop":
            return self._done(self._propose_drop(code))

        if intent == "enroll":
            return self._done(self._propose_enroll(code))

        if intent == "plan":
            out = self._call("plan_courses", {"top_k": 3})
            return self._done(_fmt_plan(out["result"]))

        if intent == "check":
            if not code:
                # 没给课程代码的模糊提问（如「先修怎么算」）⇒ 用先修缺口总览回答
                out = self._call("plan_courses", {"top_k": 1})
                return self._done(_fmt_plan(out["result"]))
            out = self._call("check_course", {"code": code})
            if out["status"] != "ok":
                return self._done(f"{code}：{out.get('error')}")
            return self._done(_fmt_check(out["result"]))

        if intent == "degree":
            out = self._call("audit_degree")
            if out["status"] != "ok":
                return self._done(f"毕业学分审核暂时不可用：{out.get('error')}")
            return self._done(_fmt_degree(out["result"]))

        if intent == "plan_timetable":
            codes = _extract_course_codes(text)
            if not codes:
                audit = self._call("audit_degree")
                if audit["status"] == "ok":
                    codes = list(audit["result"].get("missing_courses") or [])[:6]
            if not codes:
                return self._done(
                    "请告诉我你想上的课程代码（例如「帮我排课表 CS201 EE210」）。")
            out = self._call("plan_timetable", {"course_codes": codes})
            if out["status"] != "ok":
                return self._done(f"课表规划失败：{out.get('error')}")
            return self._done(_fmt_plan_timetable(out["result"]))

        # 兜底：识别不出明确意图，但消息里出现了课程代码 ⇒ 按课程查询处理
        if code:
            out = self._call("check_course", {"code": code})
            if out["status"] == "ok":
                return self._done(_fmt_check(out["result"]))

        return self._done(self._help_with_state())

    # ------------------------------------------------------------ 写操作提案
    def _propose_enroll(self, code: str | None) -> str:
        if not code:
            self._call("plan_courses", {"top_k": 1})
            return "请告诉我课程代码（例如「帮我选 CS201」），我来准备待确认的选课提案。"
        out = self._call("list_sections", {"course_code": code})
        options = out.get("result") or []
        if not options:
            return f"本学期没有 {code} 的教学班，无法提交选课申请。"
        chosen = next((s for s in options if s["free_seats"] > 0), options[0])
        self._call("submit_enrollment", {"section_id": chosen["section_id"]})
        return (f"已为你准备待确认的选课提案：{code}.{chosen['section_no']}"
                f"（{chosen['slots_text']}，余 {chosen['free_seats']} 席）。\n"
                f"点击下方「确认执行」按钮才会真正提交——智能体不会替你直接写入系统。")

    def _propose_drop(self, code: str | None) -> str:
        active = self._active_enrollments()
        if not active:
            return "你本学期还没有可退的选课记录。"
        if not code:
            listing = "、".join(f"{r['section']}（#{r['enrollment_id']}）" for r in active)
            return f"你本学期的选课记录：{listing}。告诉我要退哪一门，例如「退掉 CS201」。"
        target = next(
            (r for r in active
             if str(r["section"]).split(".")[0].upper() == code), None)
        if target is None:
            listing = "、".join(r["section"] for r in active)
            return f"你本学期没有选上 {code}。当前选课记录：{listing}。"
        self._call("drop_enrollment", {"enrollment_id": target["enrollment_id"]})
        return (f"已为你准备待确认的退课提案：{target['section']}"
                f"（选课记录 #{target['enrollment_id']}）。\n"
                f"点击「确认执行」才会真正退课；若该教学班有人候补，"
                f"退课会自动触发候补晋升。")

    def _active_enrollments(self) -> list[dict]:
        rows = self._call("my_enrollments").get("result") or []
        return [r for r in rows if r["state"] in _ACTIVE_STATES]

    # -------------------------------------------------------------- 兜底回答
    def _help_with_state(self) -> str:
        active = self._active_enrollments()
        lines: list[str] = []
        if active:
            listing = "、".join(
                f"{r['section']}（{_STATE_ZH.get(r['state'], r['state'])}）"
                for r in active)
            lines.append(f"你本学期当前的选课记录：{listing}。")
            waiting = [r for r in active if r["state"] == "WAITLISTED"]
            if waiting:
                lines.append(
                    f"其中 {len(waiting)} 门仍在候补队列——在「我的选课」页签可以看到"
                    f"队伍序号，退课时会自动晋升。")
        else:
            lines.append("你本学期还没有提交任何选课申请。")
        lines.append("")
        lines.append(self._help())
        return "\n".join(lines)

    @staticmethod
    def _help() -> str:
        return ("我可以帮你：\n"
                "  · 生成选课方案 —— 例如「推荐下学期的选课方案」「帮我排一下」\n"
                "  · 判断单门课程能否选 —— 例如「CS201 能选吗」「CS201 还有名额吗」\n"
                "  · 查看本学期课表 —— 例如「看下我的课表」「什么时候上课」\n"
                "  · 毕业学分审核与路线图 —— 例如「我还差什么课才能毕业」\n"
                "  · 无冲突课表规划 —— 例如「帮我排课表 CS201 EE210」\n"
                "  · 准备待确认的选课/退课提案 —— 例如「帮我选 CS201」「退掉 CS201」\n"
                "所有事实都来自系统工具，写操作必须先经你确认。")


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------
class AgentOrchestrator:
    def __init__(self, conn, bus, user: dict, semester: str, llm="auto",
                 registry: ToolRegistry | None = None):
        self.conn = conn
        self.bus = bus
        self.user = user
        self.semester = semester
        self.llm = ChatLLM.from_env() if llm == "auto" else llm
        self.tools = registry or ToolRegistry(conn, bus, user, semester)

    def chat(self, message: str, history=None, max_steps: int = MAX_TOOL_STEPS) -> dict:
        note = None
        if self.llm is None:
            mode = "offline"
            outcome = OfflineResponder(self.tools).respond(message)
        else:
            mode = "llm"
            try:
                outcome = self._run_llm(message, history, max_steps)
            except LLMError as exc:
                mode = "offline"
                note = f"LLM 暂不可用，已降级为离线规则模式（{exc}）"
                outcome = OfflineResponder(self.tools).respond(message)

        self._record(mode, outcome)
        return {
            "mode": mode,
            "model": self.llm.label if mode == "llm" else None,
            "note": note,
            "reply": outcome["reply"],
            "tool_trace": outcome["trace"],
            "pending_actions": outcome["pending"],
        }

    # ------------------------------------------------------------------ 内部
    def _run_llm(self, message, history, max_steps: int) -> dict:
        messages = [{"role": "system", "content": self._system()}]
        messages += _sanitize_history(history)
        messages.append({"role": "user", "content": message})

        specs = self.tools.specs()
        trace: list[dict] = []
        pending: list[dict] = []
        reply = ""

        for _ in range(max(1, max_steps)):
            msg = self.llm.complete(messages, specs)
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip()
                break
            messages.append({"role": "assistant",
                             "content": msg.get("content") or "",
                             "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = _parse_args(fn.get("arguments"))
                out = self.tools.execute(name, args)
                trace.append({"tool": name, "arguments": args,
                              "status": out["status"], "result": out})
                if out["status"] == "requires_confirmation":
                    pending.append({**out["action"], "summary": out["summary"]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(out, ensure_ascii=False, default=str),
                })

        if not reply:
            reply = "（本次工具调用已达上限，请把问题拆得更具体一些后重试。）"
        return {"reply": reply, "trace": trace, "pending": pending}

    def _system(self) -> str:
        semester_row = repo.get_semester(self.conn, self.semester) or {}
        return SYSTEM_PROMPT.format(
            name=self.user["full_name"],
            username=self.user["username"],
            role=self.user["role"],
            major=self.user["major"] or "未填写",
            max_credits=self.user["max_credits"],
            semester=self.semester,
            phase=semester_row.get("phase") or "未知",
            completed="、".join(sorted(self.user["completed_courses"])) or "无",
        )

    def _record(self, mode: str, outcome: dict) -> None:
        """智能体交互同样进入只追加的事件日志（可审计）。"""
        payload = {
            "mode": mode,
            "student": self.user["username"],
            "tools": [t["tool"] for t in outcome["trace"]],
            "pending_actions": len(outcome["pending"]),
        }
        repo.append_event(self.conn, EVT_AGENT_TURN, payload)
        self.bus.publish(EVT_AGENT_TURN, payload)


__all__ = ["AgentOrchestrator", "OfflineResponder", "format_slots"]
