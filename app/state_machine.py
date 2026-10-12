"""Formal finite state machine for the enrollment lifecycle.

Every enrollment record moves through an explicitly declared transition
table. Illegal transitions raise InvalidTransitionError; every successful
transition is audited by the caller. This module is pure domain logic:
no database, no framework.
"""
from __future__ import annotations

from app.enums import EnrollmentState as S


class InvalidTransitionError(Exception):
    def __init__(self, current: S, target: S):
        self.current = current
        self.target = target
        super().__init__(f"非法状态转移：{current.value} → {target.value}")


TRANSITIONS: dict[S, set[S]] = {
    S.SUBMITTED: {S.VALIDATED, S.REJECTED, S.CANCELLED},
    S.VALIDATED: {S.ENROLLED, S.WAITLISTED, S.CANCELLED, S.EXPIRED},
    S.WAITLISTED: {S.ENROLLED, S.CANCELLED, S.EXPIRED},
    S.ENROLLED: {S.DROPPED},
    S.REJECTED: set(),
    S.CANCELLED: set(),
    S.EXPIRED: set(),
    S.DROPPED: set(),
}


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS.get(current, set())


def transition(current: S, target: S) -> S:
    """Validate and perform a state transition. Returns the new state."""
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
    return target
