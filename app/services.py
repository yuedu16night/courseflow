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

from app import repositories as repo
from app.enums import EnrollmentState as S, Phase, PHASE_ORDER
from app.events import (
    EVT_CATALOG_VERSIONED,
    EVT_ENROLLMENT_STATE,
    EVT_PHASE_CHANGED,
    EVT_SEAT_RELEASED,
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

        # active enrollments of the student in the same semester
        mine = [
            e for e in repo.list_enrollments(self.conn, student_id=user["id"],
                                             semester=section["semester"])
            if e["state"] in (S.ENROLLED.value, S.WAITLISTED.value)
            and e["id"] != exclude_enrollment_id
        ]
        active_codes = {repo.get_section(self.conn, e["section_id"])["course_code"]
                        for e in mine}

        results = [
            check_duplicate(course["code"], active_codes, completed),
            check_prerequisites(course["code"], course["prerequisites"], completed),
        ]

        # timetable conflict + credit load against enrolled sections only
        enrolled = [e for e in mine if e["state"] == S.ENROLLED.value]
        enrolled_slots: list[ScheduleSlot] = []
        labels: dict[int, str] = {}
        current_credits = 0.0
        for i, e in enumerate(enrolled):
            sec = repo.get_section(self.conn, e["section_id"])
            enrolled_slots += [ScheduleSlot(**s) for s in sec["slots"]]
            labels[len(enrolled_slots) - len(sec["slots"])] = (
                f"{sec['course_code']}.{sec['section_no']}"
            )
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

        enr_id = repo.create_enrollment(
            self.conn, user["id"], section_id, semester, S.SUBMITTED.value)
        enr = repo.get_enrollment(self.conn, enr_id)

        try:
            self._run_policies(user, section)
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

    def timetable(self, user: dict, semester: str) -> list[dict]:
        """Merged weekly timetable of ENROLLED sections."""
        grid = []
        for e in repo.list_enrollments(self.conn, student_id=user["id"],
                                       semester=semester):
            if e["state"] != S.ENROLLED.value:
                continue
            sec = repo.get_section(self.conn, e["section_id"])
            course = course_of(self.conn, sec)
            for slot in sec["slots"]:
                grid.append({**slot, "course": course["code"],
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
        return {**enr, "section": sec["course_code"] + "." + sec["section_no"],
                "semester": sec["semester"], "student": u["username"]}

    @staticmethod
    def _require_open_semester() -> str:
        from app.runtime import CURRENT_SEMESTER
        return CURRENT_SEMESTER


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
