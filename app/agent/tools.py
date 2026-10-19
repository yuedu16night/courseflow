"""Agent 工具层（方案 B 的能力面）。

把系统**既有用例**包装成可被 LLM 调用的工具：目录查询、我的成绩单、
我的选课、我的课表、选课预检（复用策略链）、选课规划（复用方案 A）。

两条硬规则：
1. 工具只调用服务层/repositories，不新增任何领域逻辑；
2. 写工具（选课/退课）不直接落库，只返回 `requires_confirmation`
   的待确认提案，由用户确认后经正常 REST 端点执行——约束系统与
   RBAC 因此无法被智能体绕过。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app import repositories as repo
from app.agent.planner import PlannerAgent, format_slots
from app.services import (
    DegreeService,
    DomainError,
    EnrollmentService,
    TimetableService,
)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Any]
    write: bool = False
    #: 执行写工具时需要的既有能力（沿用 RBAC 能力矩阵，不另起一套权限）
    capability: str | None = None


def _free_seats(conn, section: dict) -> int:
    return section["capacity"] - repo.count_enrolled(conn, section["id"])


def _tool(name, description, parameters, write=False, capability=None):
    def deco(fn):
        return Tool(name, description, parameters, fn, write, capability)
    return deco


class ToolRegistry:
    """按请求实例化：绑定当前连接、事件总线、调用者身份与学期。"""

    def __init__(self, conn, bus, user: dict, semester: str):
        self.conn = conn
        self.bus = bus
        self.user = user
        self.semester = semester
        self._tools: dict[str, Tool] = {}
        for tool in self._build():
            self._tools[tool.name] = tool

    # ------------------------------------------------------------- 工具定义
    def _build(self) -> list[Tool]:
        conn, semester, user = self.conn, self.semester, self.user

        @_tool("list_courses", "列出课程目录中全部在用的课程（含学分与先修课程）。",
               {"type": "object", "properties": {}, "required": []})
        def list_courses(_args):
            return [{"code": c["code"], "title": c["title"], "credits": c["credits"],
                     "department": c["department"], "prerequisites": c["prerequisites"]}
                    for c in repo.list_active_courses(conn)]

        @_tool("get_course", "按课程代码查询单门课程的详情与先修要求。",
               {"type": "object",
                "properties": {"code": {"type": "string", "description": "课程代码，如 CS201"}},
                "required": ["code"]})
        def get_course(args):
            code = str(args["code"]).upper()
            course = repo.get_active_course(conn, code)
            if course is None:
                raise DomainError("COURSE_UNKNOWN", f"课程目录中不存在课程 '{code}'", 404)
            return course

        @_tool("list_sections", "列出本学期教学班及余量；可按课程代码过滤。",
               {"type": "object",
                "properties": {"course_code": {"type": "string",
                                               "description": "可选，课程代码过滤"}},
                "required": []})
        def list_sections(args):
            code = args.get("course_code")
            code = str(code).upper() if code else None
            return [{"section_id": s["id"], "course_code": s["course_code"],
                     "section_no": s["section_no"], "instructor": s["instructor_name"],
                     "capacity": s["capacity"], "free_seats": _free_seats(conn, s),
                     "venue": s["venue"], "slots": s["slots"],
                     "slots_text": format_slots(s["slots"])}
                    for s in repo.list_sections(conn, semester=semester, course_code=code)]

        @_tool("my_transcript", "读取当前学生的成绩单（已通过课程、学分上限、专业）。",
               {"type": "object", "properties": {}, "required": []})
        def my_transcript(_args):
            return {"username": user["username"], "major": user["major"],
                    "max_credits": user["max_credits"],
                    "completed_courses": sorted(user["completed_courses"])}

        @_tool("my_enrollments", "读取当前学生本学期的选课记录与状态。",
               {"type": "object", "properties": {}, "required": []})
        def my_enrollments(_args):
            svc = EnrollmentService(conn, self.bus)
            return [{"enrollment_id": e["id"], "section": e["section"],
                     "state": e["state"], "waitlist_seq": e["waitlist_seq"],
                     "reason": e["reason"]}
                    for e in svc.my_enrollments(user)]

        @_tool("my_timetable", "读取当前学生本学期已选上课程的每周课表。",
               {"type": "object", "properties": {}, "required": []})
        def my_timetable(_args):
            return EnrollmentService(conn, self.bus).timetable(user, semester)

        @_tool("semester_phase", "查询当前学期所处的选课阶段。",
               {"type": "object", "properties": {}, "required": []})
        def semester_phase(_args):
            row = repo.get_semester(conn, semester) or {}
            return {"semester": semester, "phase": row.get("phase")}

        @_tool("check_course",
               "选课预检（只读）：对某门课程的每个教学班跑一遍完整约束策略链，"
               "返回是否可选的判断与原因码，不产生任何写操作。",
               {"type": "object",
                "properties": {"code": {"type": "string", "description": "课程代码"}},
                "required": ["code"]})
        def check_course(args):
            code = str(args["code"]).upper()
            course = repo.get_active_course(conn, code)
            if course is None:
                raise DomainError("COURSE_UNKNOWN", f"课程目录中不存在课程 '{code}'", 404)
            sections = repo.list_sections(conn, semester=semester, course_code=code)
            if not sections:
                return {"course": code, "title": course["title"], "selectable": False,
                        "reason_code": "SECTION_UNKNOWN",
                        "message": f"本学期未开设 {code} 的教学班", "options": []}
            svc = EnrollmentService(conn, self.bus)
            options = []
            for section in sections:
                try:
                    svc._run_policies(user, section)      # 只读：跑策略链不落库
                    options.append({
                        "section_id": section["id"],
                        "section_no": section["section_no"],
                        "free_seats": _free_seats(conn, section),
                        "slots_text": format_slots(section["slots"]),
                        "selectable": True, "reason_code": "OK",
                        "message": "全部约束策略通过",
                    })
                except DomainError as exc:
                    options.append({
                        "section_id": section["id"],
                        "section_no": section["section_no"],
                        "free_seats": _free_seats(conn, section),
                        "slots_text": format_slots(section["slots"]),
                        "selectable": False, "reason_code": exc.code,
                        "message": exc.message,
                    })
            return {"course": code, "title": course["title"],
                    "credits": course["credits"],
                    "prerequisites": course["prerequisites"],
                    "selectable": any(o["selectable"] for o in options),
                    "options": options}

        @_tool("plan_courses",
               "生成选课组合方案：在满足先修 DAG、课表冲突、学分上限等全部约束的前提下，"
               "枚举可行的多课程组合并给出理由。这是规划智能体（方案 A）。",
               {"type": "object",
                "properties": {
                    "max_credits": {"type": "integer",
                                    "description": "可选，覆盖默认学分上限"},
                    "top_k": {"type": "integer", "description": "返回的方案数量，默认 3"}},
                "required": []})
        def plan_courses(args):
            return PlannerAgent(conn, semester).plan(
                user,
                max_credits=args.get("max_credits"),
                top_k=int(args.get("top_k") or 3),
            )

        @_tool("audit_degree",
               "毕业学分审核（只读）：按当前专业生效的培养方案逐条检查必修清单、"
               "N 选一与分类学分下限，返回总学分缺口与未满足的规则。",
               {"type": "object", "properties": {}, "required": []})
        def audit_degree(_args):
            return DegreeService(conn).audit(user)

        @_tool("plan_graduation",
               "毕业路线图（只读）：把待修课程按先修 DAG 拓扑排序，再按每学期学分上限"
               "确定性排入未来学期；不可行时给出诊断。",
               {"type": "object", "properties": {}, "required": []})
        def plan_graduation(_args):
            return DegreeService(conn).plan(user)

        @_tool("plan_timetable",
               "无冲突课表规划（只读）：为给定课程集合各选一个教学班，使每周课表区间"
               "两两不重叠，优先余席较多的教学班；无解时给出冲突诊断。",
               {"type": "object",
                "properties": {"course_codes": {
                    "type": "array", "items": {"type": "string"},
                    "description": "要安排的课程代码列表，如 [\"CS201\", \"EE210\"]"}},
                "required": ["course_codes"]})
        def plan_timetable(args):
            codes = args.get("course_codes") or []
            if isinstance(codes, str):
                codes = [codes]
            codes = [str(c).upper() for c in codes if str(c).strip()]
            if not codes:
                raise DomainError("BAD_ARGUMENTS", "请提供至少一个课程代码")
            return TimetableService(conn).plan(user, codes, semester)

        @_tool("submit_enrollment",
               "提交选课申请（写操作，需要用户确认）。",
               {"type": "object",
                "properties": {"section_id": {"type": "integer", "description": "教学班 ID"}},
                "required": ["section_id"]},
               write=True, capability="enroll:create")
        def submit_enrollment(_args):   # pragma: no cover - 由确认端点执行
            raise DomainError("AGENT_WRITE_BLOCKED", "写操作必须先经用户确认")

        @_tool("drop_enrollment",
               "退掉一条选课记录（写操作，需要用户确认）。",
               {"type": "object",
                "properties": {"enrollment_id": {"type": "integer",
                                                 "description": "选课记录 ID"}},
                "required": ["enrollment_id"]},
               write=True, capability="enroll:drop_own")
        def drop_enrollment(_args):     # pragma: no cover - 由确认端点执行
            raise DomainError("AGENT_WRITE_BLOCKED", "写操作必须先经用户确认")

        return [list_courses, get_course, list_sections, my_transcript,
                my_enrollments, my_timetable, semester_phase, check_course,
                plan_courses, audit_degree, plan_graduation, plan_timetable,
                submit_enrollment, drop_enrollment]

    # ------------------------------------------------------------- 对外接口
    @property
    def tools(self) -> dict[str, Tool]:
        return dict(self._tools)

    def specs(self) -> list[dict]:
        """OpenAI 兼容的 function-calling 工具声明。"""
        return [{
            "type": "function",
            "function": {"name": t.name, "description": t.description,
                         "parameters": t.parameters},
        } for t in self._tools.values()]

    def execute(self, name: str, args: dict) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"status": "error", "error_code": "UNKNOWN_TOOL",
                    "error": f"未知工具 '{name}'"}
        if tool.write:
            return {"status": "requires_confirmation",
                    "action": {"tool": name, "args": args},
                    "summary": self._summarize_write(tool, args)}
        try:
            return {"status": "ok", "result": tool.handler(args)}
        except DomainError as exc:
            return {"status": "error", "error_code": exc.code, "error": exc.message}
        except (KeyError, TypeError, ValueError) as exc:
            return {"status": "error", "error_code": "BAD_ARGUMENTS",
                    "error": f"工具参数不合法：{exc}"}

    def _summarize_write(self, tool: Tool, args: dict) -> str:
        if tool.name == "submit_enrollment":
            section = repo.get_section(self.conn, int(args.get("section_id", 0)))
            if section:
                return (f"提交选课申请：{section['course_code']}.{section['section_no']}"
                        f"（{section['venue']}，{format_slots(section['slots'])}）")
            return f"提交选课申请：教学班 #{args.get('section_id')}"
        if tool.name == "drop_enrollment":
            enrollment = repo.get_enrollment(self.conn, int(args.get("enrollment_id", 0)))
            if enrollment:
                section = repo.get_section(self.conn, enrollment["section_id"])
                label = (f"{section['course_code']}.{section['section_no']}"
                         if section else f"#{enrollment['section_id']}")
                return f"退课：选课记录 #{enrollment['id']}（{label}）"
            return f"退课：选课记录 #{args.get('enrollment_id')}"
        return f"执行 {tool.name}"
