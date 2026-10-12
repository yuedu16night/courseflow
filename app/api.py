"""HTTP layer: routers translate REST <-> service calls and enforce RBAC.

The HTTP layer contains no domain logic: guards (capabilities), request
models, and error mapping only. This keeps the API replaceable (e.g. by
a gRPC surface) without touching services.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app import repositories as repo
from app.enums import Role
from app.models import CourseIn, EnrollmentIn, LoginIn, SectionIn, UserIn
from app.rbac import role_has
from app.runtime import CURRENT_SEMESTER
from app.services import (
    AllocationService,
    CatalogService,
    DomainError,
    EnrollmentService,
    PhaseService,
)


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------
def get_conn(request: Request):
    conn = request.app.state.db_factory()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn=Depends(get_conn)):
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    user = repo.get_session_user(conn, token) if token else None
    if user is None:
        raise HTTPException(401, "缺少或无效的访问令牌")
    return user


def require(capability: str):
    def guard(user=Depends(current_user)):
        if not role_has(user["role"], capability):
            raise HTTPException(
                403, f"角色 {user['role']} 无权执行该操作（缺少能力 {capability}）")
        return user
    return guard


def map_domain_error(exc: DomainError):
    raise HTTPException(exc.status, {"code": exc.code, "message": exc.message})


api = APIRouter()


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
@api.post("/auth/login")
def login(body: LoginIn, conn=Depends(get_conn)):
    row = conn.execute(
        "SELECT * FROM users WHERE username=?", (body.username,)
    ).fetchone()
    if row is None or not repo.verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    token = repo.create_session(conn, row["id"])
    return {"token": token, "role": row["role"], "full_name": row["full_name"]}


@api.get("/auth/me")
def me(user=Depends(current_user)):
    return {"username": user["username"], "role": user["role"],
            "full_name": user["full_name"], "major": user["major"]}


# ---------------------------------------------------------------------------
# users (admin)
# ---------------------------------------------------------------------------
@api.post("/users")
def create_user(body: UserIn, conn=Depends(get_conn),
                _=Depends(require("user:write"))):
    if repo.get_user_by_username(conn, body.username):
        raise HTTPException(409, "用户名已存在")
    uid = repo.create_user(conn, body.username, body.password, body.full_name,
                           body.role, body.major, body.max_credits)
    return {"id": uid, "username": body.username, "role": body.role}


@api.get("/users")
def list_users(role: str | None = None, conn=Depends(get_conn),
               _=Depends(require("user:write"))):
    return [{"username": u["username"], "role": u["role"],
             "full_name": u["full_name"]} for u in repo.list_users(conn, role)]


# ---------------------------------------------------------------------------
# catalog & sections
# ---------------------------------------------------------------------------
@api.get("/courses")
def list_courses(conn=Depends(get_conn), _=Depends(require("catalog:read"))):
    return CatalogService(conn, None).list_courses()


@api.get("/courses/curriculum-order")
def curriculum_order(conn=Depends(get_conn), _=Depends(require("catalog:read"))):
    try:
        return CatalogService(conn, None).curriculum_order()
    except DomainError as e:
        map_domain_error(e)


@api.post("/courses")
def create_course(body: CourseIn, request: Request, conn=Depends(get_conn),
                  _=Depends(require("catalog:write"))):
    try:
        return CatalogService(conn, request.app.state.bus).create_course(
            body.model_dump())
    except DomainError as e:
        map_domain_error(e)


@api.get("/sections")
def list_sections(semester: str | None = None, course_code: str | None = None,
                  conn=Depends(get_conn), _=Depends(require("catalog:read"))):
    return CatalogService(conn, None).list_sections(
        semester or CURRENT_SEMESTER, course_code)


@api.post("/sections")
def create_section(body: SectionIn, request: Request, conn=Depends(get_conn),
                   _=Depends(require("section:write"))):
    try:
        return CatalogService(conn, request.app.state.bus).create_section(body)
    except DomainError as e:
        map_domain_error(e)


@api.get("/sections/{section_id}/roster")
def roster(section_id: int, request: Request, conn=Depends(get_conn),
           user=Depends(current_user)):
    section = repo.get_section(conn, section_id)
    if section is None:
        raise HTTPException(404, "教学班不存在")
    if user["role"] == Role.INSTRUCTOR.value:
        if section["instructor_name"] != user["full_name"]:
            raise HTTPException(403, "教师只能查看自己执教班级的名单")
    elif not role_has(user["role"], "roster:read_any"):
        raise HTTPException(403, f"角色 {user['role']} 无权查看名单")
    try:
        return EnrollmentService(conn, request.app.state.bus).roster(section_id)
    except DomainError as e:
        map_domain_error(e)


# ---------------------------------------------------------------------------
# enrollment
# ---------------------------------------------------------------------------
@api.post("/enrollments")
def enroll(body: EnrollmentIn, request: Request, conn=Depends(get_conn),
           user=Depends(require("enroll:create"))):
    try:
        return EnrollmentService(conn, request.app.state.bus).enroll(
            user, body.section_id)
    except DomainError as e:
        map_domain_error(e)


@api.post("/enrollments/{enrollment_id}/drop")
def drop(enrollment_id: int, request: Request, conn=Depends(get_conn),
         user=Depends(require("enroll:drop_own"))):
    try:
        return EnrollmentService(conn, request.app.state.bus).drop(
            user, enrollment_id)
    except DomainError as e:
        map_domain_error(e)


@api.get("/enrollments/me")
def my_enrollments(request: Request, conn=Depends(get_conn),
                   user=Depends(require("enroll:read_own"))):
    return EnrollmentService(conn, request.app.state.bus).my_enrollments(user)


@api.get("/enrollments")
def all_enrollments(semester: str | None = None, conn=Depends(get_conn),
                    _=Depends(require("enroll:read_all"))):
    return EnrollmentService(conn, None).all_enrollments(
        semester or CURRENT_SEMESTER)


@api.get("/timetable/me")
def my_timetable(semester: str | None = None, conn=Depends(get_conn),
                 user=Depends(require("timetable:read_own"))):
    return EnrollmentService(conn, None).timetable(
        user, semester or CURRENT_SEMESTER)


# ---------------------------------------------------------------------------
# admin: phases / allocation / audit / events
# ---------------------------------------------------------------------------
@api.get("/admin/phase")
def phase_status(semester: str | None = None, conn=Depends(get_conn),
                 _=Depends(require("enroll:read_all"))):
    try:
        return PhaseService(conn, None).status(semester or CURRENT_SEMESTER)
    except DomainError as e:
        map_domain_error(e)


@api.post("/admin/phase/advance")
def advance_phase(request: Request, semester: str | None = None,
                  conn=Depends(get_conn), _=Depends(require("phase:advance"))):
    try:
        return PhaseService(conn, request.app.state.bus).advance(
            semester or CURRENT_SEMESTER)
    except DomainError as e:
        map_domain_error(e)


@api.post("/admin/allocation/run")
def run_allocation(request: Request, semester: str | None = None,
                   seed: int = 42, conn=Depends(get_conn),
                   _=Depends(require("allocation:run"))):
    enroll_svc = EnrollmentService(conn, request.app.state.bus)
    svc = AllocationService(conn, request.app.state.bus, enroll_svc)
    try:
        return svc.run_allocation(semester or CURRENT_SEMESTER, seed)
    except DomainError as e:
        map_domain_error(e)


@api.get("/admin/audit")
def audit(enrollment_id: int | None = None, conn=Depends(get_conn),
          _=Depends(require("audit:read"))):
    return repo.list_audit(conn, enrollment_id)


@api.get("/admin/events")
def events(conn=Depends(get_conn), _=Depends(require("events:read"))):
    return repo.list_events(conn)
