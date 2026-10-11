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
        return cls(True, "OK", "policy satisfied")

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
            f"{course_code} requires prerequisite(s) {', '.join(missing)} "
            f"which are not on the student transcript",
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
    for i, cand in enumerate(candidate_slots):
        for j, active in enumerate(enrolled_slots):
            if slots_overlap(cand, active):
                return PolicyResult.fail(
                    "TIMETABLE_CONFLICT",
                    f"candidate slot #{i} ({cand.day} {cand.start_min}-{cand.end_min}) "
                    f"overlaps enrolled section '{labels.get(j, f'#{j}')}' "
                    f"({active.day} {active.start_min}-{active.end_min})",
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
            f"enrolling would raise semester load to {total:g} credits, "
            f"exceeding the limit of {max_credits}",
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
            f"already enrolled or waitlisted in {course_code} this semester",
        )
    if course_code in completed:
        return PolicyResult.fail(
            "ALREADY_PASSED",
            f"{course_code} was already passed; re-taking is not allowed",
        )
    return PolicyResult.ok()
