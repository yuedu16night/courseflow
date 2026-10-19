"""Service layer: orchestration of domain logic (behavioral dimension).

Services own use-cases; they compose repositories, the FSM, and the
constraint policies, publish events, and write audit records. No HTTP
concepts leak into this layer (state/logic separation).

Stage handover: PhaseService.advance() freezes the current stage and
writes a context snapshot (counts per section + pending/waitlisted
requests) that the next stage handler reads — no in-flight state is
lost across phase transitions.
"""
from __future__ import annotations

import json
import random

from app import curriculum as curriculum_domain
from app import degree as degree_domain
from app import repositories as repo
from app import timetable as timetable_domain
from app.enums import EnrollmentState as S, Phase, PHASE_ORDER
from app.events import (
    EVT_CATALOG_VERSIONED,
    EVT_ENROLLMENT_STATE,
    EVT_PHASE_CHANGED,
    EVT_SEAT_RELEASED,
    EVT_SEMESTER_ADVANCED,
    EVT_WAITLIST_PROMOTED,
    EventBus,
)
from app.graph import creates_cycle, topological_order
from app.policies import (
    PolicyResult,
    check_credit_limit,
    check_duplicate,
    check_prerequisites,
    check_timetable,
)
from app.models import SectionIn, ScheduleSlot
from app.state_machine import transition


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


# ===========================================================================
# Catalog service (semantic: registry + versioning + DAG integrity)
# ===========================================================================
class CatalogService:
    def __init__(self, conn, bus: EventBus):
        self.conn = conn
        self.bus = bus

    def create_course(self, data: dict) -> dict:
        # registry integrity: new prerequisites must not close a cycle
        prereqs = data.get("prerequisites", [])
        graph = repo.course_graph(self.conn)
        for pre in prereqs:
            if creates_cycle(graph, pre, data["code"]):
                raise DomainError(
                    "PREREQ_CYCLE",
                    f"为 '{data['code']}' 添加先修课 '{pre}' "
                    f"会在课程先修 DAG 中形成环",
                )
            if not repo.get_active_course(self.conn, pre) and pre != data["code"]:
                raise DomainError(
                    "PREREQ_UNKNOWN",
                    f"先修课程 '{pre}' 不存在于课程目录",
                )
        course_id, version = repo.create_course_version(self.conn, data)
        self.bus.publish(EVT_CATALOG_VERSIONED, {
            "code": data["code"], "catalog_version": version,
        })
        course = repo.get_active_course(self.conn, data["code"])
        return {**course, "id": course_id}

    def list_courses(self) -> list[dict]:
        return repo.list_active_courses(self.conn)

    def curriculum_order(self) -> dict:
        """Topological study order over the active catalog (analysis view)."""
        try:
            order = topological_order(repo.course_graph(self.conn))
        except ValueError as exc:
            raise DomainError("PREREQ_CYCLE", str(exc))
        return {"topological_order": order}

    def create_section(self, data: SectionIn) -> dict:
        if not repo.get_active_course(self.conn, data.course_code):
            raise DomainError("COURSE_UNKNOWN",
                              f"课程 '{data.course_code}' 不存在")
        section_id = repo.create_section(self.conn, data.model_dump())
        return repo.get_section(self.conn, section_id)

    def list_sections(self, semester: str | None, course_code: str | None):
        return repo.list_sections(self.conn, semester, course_code)


# ===========================================================================
# Enrollment service (behavioral: FSM + policy chain + audit)
# ===========================================================================
class EnrollmentService:
    def __init__(self, conn, bus: EventBus):
        self.conn = conn
        self.bus = bus

    # -- policy chain (semantic constraints) --------------------------------
    def _run_policies(self, user: dict, section: dict,
                      exclude_enrollment_id: int | None = None) -> None:
        course = repo.get_active_course(self.conn, section["course_code"])
        if course is None:
            raise DomainError("COURSE_UNKNOWN",
                              f"课程 '{section['course_code']}' 不在课程目录中")

        completed: set[str] = set(user["completed_courses"])

        # 本学期仍然"有效"的选课记录：预选阶段提交的志愿停留在
        # SUBMITTED / VALIDATED，要到 add_drop 阶段抽签后才成为 ENROLLED /
        # WAITLISTED。若只统计后两者，预选阶段提交的课程就不参与冲突、重复
        # 与学分校验，会造成同一时间段的两门课被同时选中。
        ACTIVE_STATES = (S.SUBMITTED.value, S.VALIDATED.value,
                         S.ENROLLED.value, S.WAITLISTED.value)
        mine = [
            e for e in repo.list_enrollments(self.conn, student_id=user["id"],
                                             semester=section["semester"])
            if e["state"] in ACTIVE_STATES
            and e["id"] != exclude_enrollment_id
        ]
        active_codes = {repo.get_section(self.conn, e["section_id"])["course_code"]
                        for e in mine}

        results = [
            check_duplicate(course["code"], active_codes, completed),
            check_prerequisites(course["code"], course["prerequisites"], completed),
        ]

        # 时间冲突 + 学分上限针对上述全部有效记录（含尚未抽签的志愿）；
        # 一个教学班可能占多个每周时段，逐个登记以便冲突提示指到具体课程。
        enrolled_slots: list[ScheduleSlot] = []
        labels: dict[int, str] = {}
        current_credits = 0.0
        for e in mine:
            sec = repo.get_section(self.conn, e["section_id"])
            label = f"{sec['course_code']}.{sec['section_no']}"
            for s in sec["slots"]:
                labels[len(enrolled_slots)] = label
                enrolled_slots.append(ScheduleSlot(**s))
            current_credits += course_of(self.conn, sec)["credits"]

        results.append(check_timetable(
            [ScheduleSlot(**s) for s in section["slots"]],
            enrolled_slots, labels,
        ))
        results.append(check_credit_limit(
            current_credits, course["credits"], user["max_credits"],
        ))

        failed = next((r for r in results if not r.passed), None)
        if failed is not None:
            raise DomainError(failed.code, failed.message)

    # -- helpers -------------------------------------------------------------
    def _audit_and_publish(self, enr: dict, target: S, actor: str,
                           reason: str = "", extra: dict | None = None) -> None:
        current = S(enr["state"])
        new_state = transition(current, target)
        repo.update_enrollment_state(self.conn, enr["id"], new_state.value,
                                     enr.get("waitlist_seq"), reason)
        repo.append_audit(self.conn, enr["id"], current.value,
                          new_state.value, actor, reason)
        payload = {
            "enrollment_id": enr["id"], "student_id": enr["student_id"],
            "section_id": enr["section_id"], "from": current.value,
            "to": new_state.value, "reason": reason, **(extra or {}),
        }
        repo.append_event(self.conn, EVT_ENROLLMENT_STATE, payload)
        self.bus.publish(EVT_ENROLLMENT_STATE, payload)

    def _free_seats(self, section: dict) -> int:
        return section["capacity"] - repo.count_enrolled(self.conn, section["id"])

    # -- public use-cases -----------------------------------------------------
    def enroll(self, user: dict, section_id: int) -> dict:
        semester = self._require_open_semester()
        section = repo.get_section(self.conn, section_id)
        if section is None or section["semester"] != semester:
            raise DomainError("SECTION_UNKNOWN",
                              f"在学期 {semester} 中找不到教学班 {section_id}")
        phase = Phase(repo.get_semester(self.conn, semester)["phase"])
        if phase not in (Phase.PRE_ENROLLMENT, Phase.ADD_DROP):
            raise DomainError(
                "PHASE_LOCKED",
                f"当前阶段（{phase.value}）不允许直接选课；"
                f"名额由批量分配服务产生")

        existing = repo.get_enrollment_by_student_section(
            self.conn, user["id"], section_id)
        if existing and existing["state"] in (
                S.SUBMITTED.value, S.VALIDATED.value, S.ENROLLED.value,
                S.WAITLISTED.value):
            raise DomainError("DUPLICATE_ENROLLMENT",
                              "本学期已存在有效的选课请求")

        if existing is not None:
            # 复用已终止的记录（CANCELLED/DROPPED/REJECTED/EXPIRED）重新提交：
            # enrollments 上有 (student_id, section_id) 唯一约束，
            # 不能为同一教学班再插入新行，否则退课后无法再次选课。
            repo.update_enrollment_state(self.conn, existing["id"],
                                         S.SUBMITTED.value)
            enr_id = existing["id"]
        else:
            enr_id = repo.create_enrollment(
                self.conn, user["id"], section_id, semester, S.SUBMITTED.value)
        enr = repo.get_enrollment(self.conn, enr_id)

        try:
            # 排除刚写入的自身记录：此时它已是 SUBMITTED，会被计入有效集合
            self._run_policies(user, section, exclude_enrollment_id=enr_id)
        except DomainError as exc:
            self._audit_and_publish(enr, S.REJECTED, user["username"],
                                    f"{exc.code}: {exc.message}")
            raise

        self._audit_and_publish(enr, S.VALIDATED, user["username"],
                                "策略校验全部通过")

        if phase is Phase.PRE_ENROLLMENT:
            # capacity-blind wish; seats are assigned by the AllocationService
            return self._view(repo.get_enrollment(self.conn, enr_id))

        # ADD_DROP: synchronous first-come-first-served
        if self._free_seats(section) > 0:
            self._audit_and_publish(repo.get_enrollment(self.conn, enr_id),
                                    S.ENROLLED, user["username"], "名额已分配（先到先得）")
        else:
            seq = repo.next_waitlist_seq(self.conn, section_id)
            repo.update_enrollment_state(self.conn, enr_id,
                                         S.WAITLISTED.value, seq,
                                         "名额已满，进入候补队列")
            repo.append_audit(self.conn, enr_id, S.VALIDATED.value,
                              S.WAITLISTED.value, user["username"],
                              "名额已满，进入候补队列")
        return self._view(repo.get_enrollment(self.conn, enr_id))

    def drop(self, user: dict, enrollment_id: int) -> dict:
        enr = repo.get_enrollment(self.conn, enrollment_id)
        if enr is None or enr["student_id"] != user["id"]:
            raise DomainError("ENROLLMENT_UNKNOWN",
                              "找不到该学生的选课记录", 404)
        if S(enr["state"]) not in (S.ENROLLED, S.WAITLISTED, S.VALIDATED):
            raise DomainError("INVALID_STATE",
                              f"当前状态（{enr['state']}）不允许退课")

        # FSM: ENROLLED -> DROPPED, pending states -> CANCELLED
        target = S.DROPPED if S(enr["state"]) == S.ENROLLED else S.CANCELLED
        was_enrolled = S(enr["state"]) == S.ENROLLED
        self._audit_and_publish(enr, target, user["username"],
                                "学生发起退课")
        if was_enrolled:
            section = repo.get_section(self.conn, enr["section_id"])
            payload = {"section_id": section["id"],
                       "semester": section["semester"]}
            repo.append_event(self.conn, EVT_SEAT_RELEASED, payload)
            self.bus.publish(EVT_SEAT_RELEASED, payload)
        return self._view(repo.get_enrollment(self.conn, enrollment_id))

    def my_enrollments(self, user: dict) -> list[dict]:
        return [self._view(e) for e in
                repo.list_enrollments(self.conn, student_id=user["id"])]

    def eligible_sections(self, user: dict) -> list[dict]:
        """当前学期中该学生此刻可选的教学班：先修已满足、未通过、仍有余额。
        已在本学期选中的教学班同样返回，并附带 enrollment_id，
        供界面把按钮就地切换为「退课」而课程行不消失。"""
        semester = repo.get_current_semester(self.conn)
        catalog = {c["code"]: c for c in repo.list_active_courses(self.conn)}
        completed: set[str] = set(user["completed_courses"])
        active: dict[int, dict] = {}
        active_codes: set[str] = set()
        for e in repo.list_enrollments(self.conn, student_id=user["id"],
                                       semester=semester):
            if e["state"] not in (S.SUBMITTED.value, S.VALIDATED.value,
                                  S.ENROLLED.value, S.WAITLISTED.value):
                continue
            active[e["section_id"]] = e
            sec = repo.get_section(self.conn, e["section_id"])
            if sec is not None:
                active_codes.add(sec["course_code"])

        out = []
        for sec in repo.list_sections(self.conn, semester, None):
            course = catalog.get(sec["course_code"])
            if course is None:
                continue
            code = course["code"]
            mine = active.get(sec["id"])
            if mine is None:                   # 尚未选中的教学班需通过全部过滤
                if code in completed or code in active_codes:
                    continue                   # 已通过 / 本学期已在选其它班
                if any(p not in completed for p in course["prerequisites"]):
                    continue                   # 先修未满足
                if self._free_seats(sec) <= 0:
                    continue                   # 无余额
            out.append({**sec, "title": course["title"],
                        "credits": course["credits"],
                        "category": course["category"],
                        "prerequisites": course["prerequisites"],
                        "free_seats": self._free_seats(sec),
                        "enrolled_count":
                            repo.count_active_enrollments(self.conn, sec["id"]),
                        "enrollment_id": mine["id"] if mine else None,
                        "enrollment_state": mine["state"] if mine else None})
        return out

    def timetable(self, user: dict, semester: str) -> list[dict]:
        """Merged weekly timetable of ENROLLED and pre-selected (VALIDATED)
        sections. 预选落选（WAITLISTED/REJECTED/CANCELLED）不在其中。"""
        grid = []
        for e in repo.list_enrollments(self.conn, student_id=user["id"],
                                       semester=semester):
            if e["state"] not in (S.ENROLLED.value, S.VALIDATED.value):
                continue
            sec = repo.get_section(self.conn, e["section_id"])
            course = course_of(self.conn, sec)
            for slot in sec["slots"]:
                grid.append({**slot, "course": course["code"],
                             "title": course["title"],
                             "section": sec["section_no"],
                             "venue": sec["venue"]})
        return sorted(grid, key=lambda x: (x["day"], x["start_min"]))

    def roster(self, section_id: int) -> dict:
        section = repo.get_section(self.conn, section_id)
        if section is None:
            raise DomainError("SECTION_UNKNOWN", f"教学班 {section_id} 不存在", 404)
        students = []
        for e in repo.list_enrollments(self.conn, section_id=section_id):
            if e["state"] == S.ENROLLED.value:
                u = repo.get_user(self.conn, e["student_id"])
                students.append({"username": u["username"],
                                 "full_name": u["full_name"]})
        return {"section": section, "enrolled": students}

    def all_enrollments(self, semester: str | None = None) -> list[dict]:
        return [self._view(e) for e in
                repo.list_enrollments(self.conn, semester=semester)]

    def _view(self, enr: dict) -> dict:
        sec = repo.get_section(self.conn, enr["section_id"])
        u = repo.get_user(self.conn, enr["student_id"])
        course = course_of(self.conn, sec)
        # 附带课程名/学分/上课时段：前端把「已选中的课」直接画进周课表，无需二次查询
        return {**enr, "section": sec["course_code"] + "." + sec["section_no"],
                "course_code": sec["course_code"], "section_no": sec["section_no"],
                "title": course["title"], "credits": course["credits"],
                "slots": sec["slots"], "venue": sec["venue"],
                "semester": sec["semester"], "student": u["username"]}

    def _require_open_semester(self) -> str:
        """当前可操作学期：由库内 `app_settings` 决定（可随「推进学期」改变）。"""
        return repo.get_current_semester(self.conn)


def course_of(conn, section: dict) -> dict:
    course = repo.get_active_course(conn, section["course_code"])
    if course is None:  # catalog superseded mid-semester: fall back to any version
        row = conn.execute(
            "SELECT * FROM courses WHERE code=? ORDER BY catalog_version DESC LIMIT 1",
            (section["course_code"],),
        ).fetchone()
        course = {
            "code": row["code"], "title": row["title"], "credits": row["credits"],
            "prerequisites": json.loads(row["prerequisites"]),
        }
    return course


# ===========================================================================
# Allocation service (behavioral: batch lottery between stages)
# ===========================================================================
class AllocationService:
    def __init__(self, conn, bus: EventBus, enroll_service: EnrollmentService):
        self.conn = conn
        self.bus = bus
        self.enroll = enroll_service

    def run_allocation(self, semester: str, seed: int = 42) -> dict:
        phase = Phase(repo.get_semester(self.conn, semester)["phase"])
        if phase is not Phase.ALLOCATION:
            raise DomainError("WRONG_PHASE",
                              f"批量分配要求阶段为 allocation，当前为 {phase.value}")

        rng = random.Random(seed)   # seeded => reproducible, auditable draw
        summary = {"seed": seed, "sections": {}}
        sections = repo.list_sections(self.conn, semester=semester)
        for section in sections:
            pending = repo.list_enrollments(self.conn, section_id=section["id"],
                                            state=S.VALIDATED.value)
            rng.shuffle(pending)
            seats = section["capacity"]
            enrolled_n = waitlisted_n = 0
            for i, e in enumerate(pending):
                enr = repo.get_enrollment(self.conn, e["id"])
                if i < seats:
                    self.enroll._audit_and_publish(enr, S.ENROLLED,
                                                   "system:allocation",
                                                   "抽签分配到名额")
                    enrolled_n += 1
                else:
                    seq = repo.next_waitlist_seq(self.conn, section["id"])
                    repo.update_enrollment_state(
                        self.conn, e["id"], S.WAITLISTED.value, seq,
                        "抽签未中，进入候补队列")
                    repo.append_audit(self.conn, e["id"], S.VALIDATED.value,
                                      S.WAITLISTED.value, "system:allocation",
                                      "抽签未中，进入候补队列")
                    waitlisted_n += 1
            summary["sections"][f"{section['course_code']}.{section['section_no']}"] = {
                "capacity": section["capacity"], "requests": len(pending),
                "enrolled": enrolled_n, "waitlisted": waitlisted_n,
            }
        return summary


DEFAULT_MAX_SEMESTERS = 8


# ===========================================================================
# Degree service (semantic: 培养方案审核 + 毕业路线图)
# ===========================================================================
class DegreeService:
    def __init__(self, conn):
        self.conn = conn

    def _active_program(self, user: dict) -> dict:
        major = user.get("major")
        if not major:
            raise DomainError("DEGREE_PROGRAM_NOT_FOUND",
                              "该学生未填写专业，无法匹配培养方案")
        program = repo.get_active_degree_requirement(self.conn, major)
        if program is None:
            raise DomainError("DEGREE_PROGRAM_NOT_FOUND",
                              f"专业 {major} 暂无生效的培养方案")
        return program

    def _in_progress_codes(self, user: dict) -> set[str]:
        """本学期「在修」的课程代码（已选上或已校验，尚未出成绩）。"""
        semester = repo.get_current_semester(self.conn)
        codes: set[str] = set()
        for e in repo.list_enrollments(self.conn, student_id=user["id"],
                                       semester=semester):
            if e["state"] not in (S.ENROLLED.value, S.VALIDATED.value):
                continue
            sec = repo.get_section(self.conn, e["section_id"])
            if sec is not None:
                codes.add(sec["course_code"])
        return codes

    def audit(self, user: dict) -> dict:
        program = self._active_program(user)
        catalog = {c["code"]: c for c in repo.list_active_courses(self.conn)}
        result = degree_domain.audit_requirements(
            set(user["completed_courses"]),
            {code: c["credits"] for code, c in catalog.items()},
            program["rules"], program["total_credits_min"])

        # 分类别列出每门课的修读状态（已修 / 在修 / 未修），供培养计划页展示
        completed = set(user["completed_courses"])
        in_progress = self._in_progress_codes(user)
        course_status = []
        for rule in result["rules"]:
            rows = []
            for code in sorted(rule["courses"]):
                course = catalog.get(code)
                if course is None:
                    continue
                if code in completed:
                    state = "已修"
                elif code in in_progress:
                    state = "在修"
                else:
                    state = "未修"
                rows.append({"code": code, "title": course["title"],
                             "credits": course["credits"], "status": state})
            course_status.append({"category": rule["category"],
                                  "rule_type": rule["rule_type"],
                                  "courses": rows})

        return {
            "student": user["username"],
            "major": program["major"],
            "program": {"version": program["version"], "label": program["label"],
                        "total_credits_min": program["total_credits_min"]},
            "course_status": course_status,
            **result,
        }

    def plan(self, user: dict) -> dict:
        audit = self.audit(user)
        catalog = {c["code"]: c for c in repo.list_active_courses(self.conn)}
        missing_courses = []
        for code in audit["missing_courses"]:
            course = catalog.get(code)
            if course is None:
                continue
            missing_courses.append({"code": code, "title": course["title"],
                                    "credits": course["credits"]})

        # 先修子图（仅保留两端都在待修集合内的边）
        missing_set = {c["code"] for c in missing_courses}
        subgraph: dict[str, set[str]] = {}
        for pre, dsts in repo.course_graph(self.conn).items():
            kept = {d for d in dsts if d in missing_set}
            if pre in missing_set and kept:
                subgraph[pre] = kept

        cap = int(user["max_credits"])
        route = degree_domain.plan_graduation(
            missing_courses, subgraph, cap, repo.get_current_semester(self.conn),
            DEFAULT_MAX_SEMESTERS)
        return {
            "student": user["username"],
            "major": audit["major"],
            "status": audit["status"],
            "missing_courses": sorted(missing_set),
            "per_semester_cap": cap,
            "max_semesters": DEFAULT_MAX_SEMESTERS,
            **route,
        }

    def create_requirement(self, data: dict) -> dict:
        rules = data["rules"]
        known = {c["code"] for c in repo.list_active_courses(self.conn)}
        unknown: set[str] = set()
        for rule in rules:
            unknown.update(c for c in rule.get("courses", []) if c not in known)
        if unknown:
            raise DomainError("COURSE_NOT_FOUND",
                              f"培养方案引用了课程目录中不存在的课程："
                              f"{'、'.join(sorted(unknown))}")

        # 与课程目录版本化一致：新版本落地时旧版本失效
        repo.deactivate_degree_versions(self.conn, data["major"])
        requirement_id = repo.create_degree_requirement(
            self.conn, data["major"], data["total_credits_min"],
            data.get("label", ""))
        for order, rule in enumerate(rules):
            repo.append_degree_rule(
                self.conn, requirement_id, rule["category"], rule["rule_type"],
                rule.get("courses", []), rule.get("min_credits"),
                rule.get("min_courses"), order)
        return repo.get_active_degree_requirement(self.conn, data["major"])

    def list_requirements(self) -> list[dict]:
        return repo.list_degree_requirements(self.conn, active_only=False)


# ===========================================================================
# Timetable service (behavioral: 无冲突课表规划)
# ===========================================================================
class TimetableService:
    def __init__(self, conn):
        self.conn = conn

    def plan(self, user: dict, course_codes: list[str],
             semester: str | None = None) -> dict:
        semester = semester or repo.get_current_semester(self.conn)
        codes = sorted({str(c).upper() for c in course_codes})
        known = {c["code"] for c in repo.list_active_courses(self.conn)}
        unknown = [c for c in codes if c not in known]
        if unknown:
            raise DomainError("COURSE_NOT_FOUND",
                              f"课程目录中不存在课程 {'、'.join(unknown)}")

        sections_by_course: dict[str, list[dict]] = {}
        enrolled_counts: dict[int, int] = {}
        for section in repo.list_sections(self.conn, semester=semester):
            if section["course_code"] not in codes:
                continue
            sections_by_course.setdefault(section["course_code"], []).append(section)
            enrolled_counts[section["id"]] = repo.count_enrolled(
                self.conn, section["id"])

        result = timetable_domain.plan_timetable(
            codes, sections_by_course, enrolled_counts)
        return {"student": user["username"], "semester": semester, **result}


# ===========================================================================
# Phase service (behavioral: multi-stage handover with context sharing)
# ===========================================================================
class PhaseService:
    def __init__(self, conn, bus: EventBus):
        self.conn = conn
        self.bus = bus

    def status(self, semester: str) -> dict:
        sem = repo.get_semester(self.conn, semester)
        if sem is None:
            raise DomainError("SEMESTER_UNKNOWN",
                              f"学期 {semester} 未初始化", 404)
        return sem

    def advance(self, semester: str) -> dict:
        sem = repo.get_semester(self.conn, semester)
        if sem is None:
            raise DomainError("SEMESTER_UNKNOWN",
                              f"学期 {semester} 未初始化", 404)
        current = Phase(sem["phase"])
        idx = PHASE_ORDER.index(current)
        if idx + 1 >= len(PHASE_ORDER):
            raise DomainError("PHASE_TERMINAL",
                              f"学期已处于终态阶段 {current.value}，无法继续推进")
        nxt = PHASE_ORDER[idx + 1]

        # ---- stage handover: snapshot context for the incoming stage -------
        snapshot = self._build_snapshot(semester)
        repo.upsert_semester(self.conn, semester, nxt.value, snapshot)
        payload = {"semester": semester, "from": current.value,
                   "to": nxt.value, "handover_context": snapshot}
        repo.append_event(self.conn, EVT_PHASE_CHANGED, payload)
        self.bus.publish(EVT_PHASE_CHANGED, payload)
        return repo.get_semester(self.conn, semester)

    def _build_snapshot(self, semester: str) -> dict:
        sections = repo.list_sections(self.conn, semester=semester)
        per_section = {}
        for s in sections:
            rows = repo.list_enrollments(self.conn, section_id=s["id"])
            per_section[f"{s['course_code']}.{s['section_no']}"] = {
                "capacity": s["capacity"],
                "enrolled": sum(r["state"] == S.ENROLLED.value for r in rows),
                "validated_pending": sum(r["state"] == S.VALIDATED.value for r in rows),
                "waitlisted": sum(r["state"] == S.WAITLISTED.value for r in rows),
            }
        return {"generated_at": "phase-advance", "sections": per_section}


# ===========================================================================
# Semester service (behavioral: 推进到下一学期 + 自动开课 + 跨学年升年级)
# ===========================================================================
class SemesterService:
    """「推进学期」：进入下一学期，按课程常规开课学期自动生成教学班；
    若发生跨学年（SP → FA），在读学生年级自动 +1。"""

    def __init__(self, conn, bus: EventBus):
        self.conn = conn
        self.bus = bus

    def status(self) -> dict:
        code = repo.get_current_semester(self.conn)
        sem = repo.get_semester(self.conn, code) or {}
        return {"semester": code, "phase": sem.get("phase"),
                "handover_context": sem.get("handover_context", {})}

    def advance(self) -> dict:
        current = repo.get_current_semester(self.conn)
        nxt = degree_domain.next_semester_codes(current, 2)[1]

        # 目标学期行：缺失则以 PRE_ENROLLMENT 建之（幂等）
        if repo.get_semester(self.conn, nxt) is None:
            repo.upsert_semester(self.conn, nxt, Phase.PRE_ENROLLMENT.value)

        # 自动开课：按课程常规开课学期生成教学班（已存在则跳过）
        existing = {s["course_code"]
                    for s in repo.list_sections(self.conn, semester=nxt)}
        created = 0
        for section in curriculum_domain.build_offering(
                repo.list_active_courses(self.conn), nxt):
            if section["course_code"] in existing:
                continue
            repo.create_section(self.conn, section)
            created += 1

        # 跨学年（SP → FA）：在读学生年级 +1（毕业年级不再上浮）
        promoted = 0
        if current.endswith("SP"):
            for u in repo.list_users(self.conn, role="student"):
                grade = u.get("grade")
                if grade and grade < 4:
                    repo.update_user_grade(self.conn, u["id"], grade + 1)
                    promoted += 1

        repo.set_current_semester(self.conn, nxt)
        payload = {"from": current, "to": nxt,
                   "sections_created": created, "students_promoted": promoted}
        repo.append_event(self.conn, EVT_SEMESTER_ADVANCED, payload)
        self.bus.publish(EVT_SEMESTER_ADVANCED, payload)
        return {"previous": current, "semester": nxt,
                "phase": Phase.PRE_ENROLLMENT.value,
                "sections_created": created, "students_promoted": promoted}
