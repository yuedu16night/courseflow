"""Domain enumerations shared across all layers of CourseFlow."""
from enum import Enum


class Role(str, Enum):
    """System roles used by the RBAC permission matrix."""
    STUDENT = "student"
    INSTRUCTOR = "instructor"
    REGISTRAR = "registrar"
    ADMIN = "admin"


class Phase(str, Enum):
    """Academic phases of a semester. Transitions are strictly linear
    (see PHASE_ORDER) and each phase changes enrollment behaviour."""
    PRE_ENROLLMENT = "pre_enrollment"   # wishes collection, capacity-blind
    ALLOCATION = "allocation"           # batch lottery / capacity assignment
    ADD_DROP = "add_drop"               # first-come-first-served + waitlist
    CLOSED = "closed"                   # frozen, read-only


PHASE_ORDER: list[Phase] = [
    Phase.PRE_ENROLLMENT,
    Phase.ALLOCATION,
    Phase.ADD_DROP,
    Phase.CLOSED,
]


class EnrollmentState(str, Enum):
    """States of the enrollment lifecycle finite state machine."""
    SUBMITTED = "SUBMITTED"
    VALIDATED = "VALIDATED"
    ENROLLED = "ENROLLED"
    WAITLISTED = "WAITLISTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    DROPPED = "DROPPED"


TERMINAL_STATES = {
    EnrollmentState.REJECTED,
    EnrollmentState.CANCELLED,
    EnrollmentState.EXPIRED,
    EnrollmentState.DROPPED,
}
