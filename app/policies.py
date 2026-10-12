"""Constraint policies (semantic dimension).

Each policy is a small, pure, independently testable rule that guards
enrollment transitions. The EnrollmentService composes them into a
validation chain; a failing policy rejects the request with a precise
machine-readable reason code.

PolicyResult reasons double as domain error codes surfaced by the API,
so the semantic layer is fully observable in logs and reports.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import ScheduleSlot


@dataclass(frozen=True)
class PolicyResult:
    passed: bool
    code: str
    message: str

    @classmethod
    def ok(cls) -> "PolicyResult":
        return cls(True, "OK", "校验通过")

    @classmethod
    def fail(cls, code: str, message: str) -> "PolicyResult":
        return cls(False, code, message)


# ---------------------------------------------------------------------------
# 1. Prerequisite policy — student transcript must cover the DAG ancestors
# ---------------------------------------------------------------------------
def check_prerequisites(
    course_code: str,
    prerequisites: list[str],
    completed: set[str],
) -> PolicyResult:
    missing = sorted(p for p in prerequisites if p not in completed)
    if missing:
        return PolicyResult.fail(
            "PREREQ_MISSING",
            f"{course_code} 要求先修课程 {', '.join(missing)}，"
            f"但成绩单中尚未通过",
        )
    return PolicyResult.ok()


# ---------------------------------------------------------------------------
# 2. Timetable policy — no overlapping weekly slots with active sections
# ---------------------------------------------------------------------------
def slots_overlap(a: ScheduleSlot, b: ScheduleSlot) -> bool:
    return a.day == b.day and a.start_min < b.end_min and b.start_min < a.end_min


def check_timetable(
    candidate_slots: list[ScheduleSlot],
    enrolled_slots: list[ScheduleSlot],
    enrolled_labels: dict[int, str] | None = None,
) -> PolicyResult:
    labels = enrolled_labels or {}
    for cand in candidate_slots:
        for j, active in enumerate(enrolled_slots):
            if slots_overlap(cand, active):
                return PolicyResult.fail(
                    "TIMETABLE_CONFLICT",
                    f"所选时段（周{cand.day} {cand.start_min}-{cand.end_min}）"
                    f"与已选课程 '{labels.get(j, f'#{j}')}' "
                    f"（周{active.day} {active.start_min}-{active.end_min}）时间冲突",
                )
    return PolicyResult.ok()


# ---------------------------------------------------------------------------
# 3. Credit limit policy — per-semester workload ceiling
# ---------------------------------------------------------------------------
def check_credit_limit(
    current_credits: float,
    candidate_credits: float,
    max_credits: int,
) -> PolicyResult:
    total = current_credits + candidate_credits
    if total > max_credits:
        return PolicyResult.fail(
            "CREDIT_LIMIT",
            f"选课后学期总学分将达到 {total:g}，超过上限 {max_credits} 学分",
        )
    return PolicyResult.ok()


# ---------------------------------------------------------------------------
# 4. Duplicate / re-take policy
# ---------------------------------------------------------------------------
def check_duplicate(
    course_code: str,
    active_course_codes: set[str],
    completed: set[str],
) -> PolicyResult:
    if course_code in active_course_codes:
        return PolicyResult.fail(
            "DUPLICATE_ENROLLMENT",
            f"本学期已选或已候补 {course_code}，不能重复选课",
        )
    if course_code in completed:
        return PolicyResult.fail(
            "ALREADY_PASSED",
            f"{course_code} 已通过，不允许重修选课",
        )
    return PolicyResult.ok()
