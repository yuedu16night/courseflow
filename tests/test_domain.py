"""Unit tests: FSM transition table and constraint policies (pure domain)."""
import pytest

from app.enums import EnrollmentState as S
from app.graph import creates_cycle, topological_order
from app.models import ScheduleSlot
from app.policies import (
    check_credit_limit,
    check_duplicate,
    check_prerequisites,
    check_timetable,
    slots_overlap,
)
from app.state_machine import InvalidTransitionError, transition


# ---------------------------------------------------------------- FSM ----
def test_happy_path_transitions():
    assert transition(S.SUBMITTED, S.VALIDATED) == S.VALIDATED
    assert transition(S.VALIDATED, S.ENROLLED) == S.ENROLLED
    assert transition(S.ENROLLED, S.DROPPED) == S.DROPPED


def test_illegal_transitions_rejected():
    with pytest.raises(InvalidTransitionError):
        transition(S.SUBMITTED, S.ENROLLED)        # must be validated first
    with pytest.raises(InvalidTransitionError):
        transition(S.ENROLLED, S.ENROLLED)         # no self-loop
    with pytest.raises(InvalidTransitionError):
        transition(S.DROPPED, S.ENROLLED)          # terminal state


def test_waitlisted_can_only_promote_or_expire():
    assert transition(S.WAITLISTED, S.ENROLLED) == S.ENROLLED
    with pytest.raises(InvalidTransitionError):
        transition(S.WAITLISTED, S.REJECTED)


# ------------------------------------------------------------ policies ----
def _slot(day, start, end):
    return ScheduleSlot(day=day, start_min=start, end_min=end)


def test_slots_overlap_detection():
    assert slots_overlap(_slot(1, 510, 650), _slot(1, 600, 700))
    assert not slots_overlap(_slot(1, 510, 650), _slot(2, 510, 650))
    assert not slots_overlap(_slot(1, 510, 650), _slot(1, 650, 790))  # adjacency ok
    with pytest.raises(ValueError):
        _slot(1, 700, 600)                                             # inverted


def test_prerequisite_policy():
    ok = check_prerequisites("CS201", ["CS101"], {"CS101", "MATH101"})
    assert ok.passed
    bad = check_prerequisites("CS201", ["CS101", "MATH101"], {"CS101"})
    assert not bad.passed and bad.code == "PREREQ_MISSING"


def test_credit_limit_policy():
    assert check_credit_limit(10, 3, 35).passed
    bad = check_credit_limit(33, 3, 35)
    assert not bad.passed and bad.code == "CREDIT_LIMIT"


def test_duplicate_policy():
    assert check_duplicate("CS201", set(), set()).passed
    assert check_duplicate("CS201", {"CS201"}, set()).code == "DUPLICATE_ENROLLMENT"
    assert check_duplicate("CS201", set(), {"CS201"}).code == "ALREADY_PASSED"


def test_timetable_policy_reports_conflicting_section():
    cand = [_slot(1, 600, 700)]
    active = [_slot(1, 510, 650)]
    res = check_timetable(cand, active, {0: "CS201.01"})
    assert not res.passed and res.code == "TIMETABLE_CONFLICT"
    assert "CS201.01" in res.message


# -------------------------------------------------------------- graph ----
def test_cycle_detection():
    graph = {"CS101": {"CS201"}, "CS201": set()}
    assert creates_cycle(graph, "CS201", "CS101")     # CS101 -> CS201 closes cycle
    assert not creates_cycle(graph, "CS101", "CS301")


def test_topological_order_and_cycle_error():
    graph = {"CS101": {"CS201"}, "MATH101": {"CS201", "EE210"}, "CS201": {"CS301"}}
    order = topological_order(graph)
    assert order.index("CS101") < order.index("CS201") < order.index("CS301")
    with pytest.raises(ValueError, match="cycle"):
        topological_order({"A": {"B"}, "B": {"A"}})
