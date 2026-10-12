"""Role-Based Access Control: capability matrix + FastAPI dependencies.

Access is expressed as capabilities (fine-grained permissions), not raw
roles, so endpoint guards stay stable when role definitions evolve.
Row-level checks (e.g. instructor may only see own rosters) live in the
routers on top of these capability guards.
"""
from __future__ import annotations

from app.enums import Role

# capability -> roles allowed
PERMISSIONS: dict[str, set[Role]] = {
    # read
    "catalog:read":       {Role.STUDENT, Role.INSTRUCTOR, Role.REGISTRAR, Role.ADMIN},
    "timetable:read_own": {Role.STUDENT},
    "enroll:read_own":    {Role.STUDENT},
    "enroll:read_all":    {Role.REGISTRAR, Role.ADMIN},
    "roster:read_own":    {Role.INSTRUCTOR},
    "roster:read_any":    {Role.REGISTRAR, Role.ADMIN},
    # student actions
    "enroll:create":      {Role.STUDENT},
    "enroll:drop_own":    {Role.STUDENT},
    # agent layer (bound to the caller's identity, so row-level checks still apply)
    "agent:use":          {Role.STUDENT},
    # registrar actions
    "catalog:write":      {Role.REGISTRAR},
    "section:write":      {Role.REGISTRAR},
    "phase:advance":      {Role.REGISTRAR},
    "allocation:run":     {Role.REGISTRAR},
    "enroll:force":       {Role.REGISTRAR},
    # admin actions
    "user:write":         {Role.ADMIN},
    "audit:read":         {Role.ADMIN, Role.REGISTRAR},
    "events:read":        {Role.ADMIN, Role.REGISTRAR},
}


def role_has(role: str, capability: str) -> bool:
    try:
        r = Role(role)
    except ValueError:
        return False
    return r in PERMISSIONS.get(capability, set())
