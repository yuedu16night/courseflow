"""HTTP layer: routers translate REST <-> service calls and enforce RBAC.

The HTTP layer contains no domain logic: guards (capabilities), request
models, and error mapping only. This keeps the API replaceable (e.g. by
a gRPC surface) without touching services.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app import repositories as repo
from app.agent.llm import ChatLLM
from app.agent.orchestrator import AgentOrchestrator
from app.agent.planner import PlannerAgent
from app.enums import Role
from app.models import (
    AgentActionIn,
    AgentChatIn,
    AgentPlanIn,
    CourseIn,
    DegreeRequirementIn,
    EnrollmentIn,
    LoginIn,
    SectionIn,
    TimetablePlanIn,
    UserIn,
)
from app.rbac import role_has
from app.services import (
    AllocationService,
    CatalogService,
    DegreeService,
    DomainError,
    EnrollmentService,
    PhaseService,
    SemesterService,
    TimetableService,
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
def me(user=Depends(current_user), conn=Depends(get_conn)):
    code = repo.get_current_semester(conn)
    sem = repo.get_semester(conn, code) or {}
    return {"username": user["username"], "role": user["role"],
            "full_name": user["full_name"], "major": user["major"],
            "grade": user.get("grade"), "semester": code,
            "phase": sem.get("phase")}


# ---------------------------------------------------------------------------
# users (admin)
# ---------------------------------------------------------------------------
@api.post("/users")
def create_user(body: UserIn, conn=Depends(get_conn),
                _=Depends(require("user:write"))):
    if repo.get_user_by_username(conn, body.username):
        raise HTTPException(409, "用户名已存在")
    uid = repo.create_user(conn, body.username, body.password, body.full_name,
                           body.role, body.major, body.max_credits,
                           grade=body.grade)
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
        semester or repo.get_current_semester(conn), course_code)


@api.get("/sections/eligible")
def eligible_sections(request: Request, conn=Depends(get_conn),
                      user=Depends(require("enroll:create"))):
    """该学生此刻可选的教学班：先修满足、未通过、未在选、有余额。"""
    return EnrollmentService(conn, request.app.state.bus).eligible_sections(user)


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
        semester or repo.get_current_semester(conn))


@api.get("/timetable/me")
def my_timetable(semester: str | None = None, conn=Depends(get_conn),
                 user=Depends(require("timetable:read_own"))):
    return EnrollmentService(conn, None).timetable(
        user, semester or repo.get_current_semester(conn))


# ---------------------------------------------------------------------------
# degree (培养方案审核 / 毕业规划) & timetable planning
# ---------------------------------------------------------------------------
@api.get("/degree/me")
def degree_me(conn=Depends(get_conn), user=Depends(require("degree:read"))):
    """本人毕业学分审核：逐条规则状态 + 总学分缺口。"""
    try:
        return DegreeService(conn).audit(user)
    except DomainError as e:
        map_domain_error(e)


@api.get("/degree/me/plan")
def degree_me_plan(conn=Depends(get_conn), user=Depends(require("degree:read"))):
    """本人毕业路线图：待修课程按先修 DAG 拓扑序排入未来学期。"""
    try:
        return DegreeService(conn).plan(user)
    except DomainError as e:
        map_domain_error(e)


@api.get("/degree/requirements")
def degree_requirements(conn=Depends(get_conn),
                        _=Depends(require("degree:read"))):
    """列出全部培养方案版本（含历史版本与其 active 标志）。"""
    return DegreeService(conn).list_requirements()


@api.post("/degree/requirements")
def create_degree_requirement(body: DegreeRequirementIn, conn=Depends(get_conn),
                              _=Depends(require("degree:write"))):
    """教务新建培养方案版本：同专业旧版本自动失效。"""
    try:
        return DegreeService(conn).create_requirement(body.model_dump())
    except DomainError as e:
        map_domain_error(e)


@api.post("/timetable/plan")
def plan_timetable(body: TimetablePlanIn, semester: str | None = None,
                   conn=Depends(get_conn),
                   user=Depends(require("timetable:read_own"))):
    """给定课程集合求无冲突的教学班组合与合并周课表。"""
    try:
        return TimetableService(conn).plan(user, body.course_codes,
                                           semester or repo.get_current_semester(conn))
    except DomainError as e:
        map_domain_error(e)


# ---------------------------------------------------------------------------
# admin: phases / allocation / audit / events
# ---------------------------------------------------------------------------
@api.get("/admin/phase")
def phase_status(semester: str | None = None, conn=Depends(get_conn),
                 _=Depends(require("enroll:read_all"))):
    try:
        return PhaseService(conn, None).status(
            semester or repo.get_current_semester(conn))
    except DomainError as e:
        map_domain_error(e)


@api.post("/admin/phase/advance")
def advance_phase(request: Request, semester: str | None = None,
                  conn=Depends(get_conn), _=Depends(require("phase:advance"))):
    try:
        return PhaseService(conn, request.app.state.bus).advance(
            semester or repo.get_current_semester(conn))
    except DomainError as e:
        map_domain_error(e)


@api.post("/admin/allocation/run")
def run_allocation(request: Request, semester: str | None = None,
                   seed: int = 42, conn=Depends(get_conn),
                   _=Depends(require("allocation:run"))):
    enroll_svc = EnrollmentService(conn, request.app.state.bus)
    svc = AllocationService(conn, request.app.state.bus, enroll_svc)
    try:
        return svc.run_allocation(
            semester or repo.get_current_semester(conn), seed)
    except DomainError as e:
        map_domain_error(e)


@api.get("/admin/semester")
def semester_status(conn=Depends(get_conn),
                    _=Depends(require("enroll:read_all"))):
    """当前学期与所处阶段。"""
    return SemesterService(conn, None).status()


@api.post("/admin/semester/advance")
def advance_semester(request: Request, conn=Depends(get_conn),
                     _=Depends(require("semester:advance"))):
    """推进到下一学期：自动按课程常规开课学期生成教学班；跨学年时学生年级 +1。"""
    try:
        return SemesterService(conn, request.app.state.bus).advance()
    except DomainError as e:
        map_domain_error(e)


@api.get("/admin/audit")
def audit(enrollment_id: int | None = None, conn=Depends(get_conn),
          _=Depends(require("audit:read"))):
    return repo.list_audit(conn, enrollment_id)


@api.get("/admin/events")
def events(conn=Depends(get_conn), _=Depends(require("events:read"))):
    return repo.list_events(conn)


# ---------------------------------------------------------------------------
# agent layer — scheme A (deterministic planning) + scheme B (LLM orchestration)
# ---------------------------------------------------------------------------
def _int_arg(args: dict, key: str) -> int:
    try:
        return int(args[key])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, f"缺少或非法的参数 {key}")


@api.get("/agent/status")
def agent_status(_=Depends(require("agent:use"))):
    """前端据此显示当前是 LLM 模式还是离线规则模式。"""
    llm = ChatLLM.from_env()
    return {"enabled": llm is not None,
            "mode": "llm" if llm else "offline",
            "model": llm.label if llm else None}


@api.post("/agent/plan")
def agent_plan(body: AgentPlanIn, conn=Depends(get_conn),
               user=Depends(require("agent:use"))):
    """方案 A：确定性的选课组合规划（只读）。"""
    return PlannerAgent(conn, repo.get_current_semester(conn)).plan(
        user, max_credits=body.max_credits, top_k=body.top_k)


@api.post("/agent/chat")
def agent_chat(body: AgentChatIn, request: Request, conn=Depends(get_conn),
               user=Depends(require("agent:use"))):
    """方案 B：自然语言编排（未配置 LLM 时自动降级为离线规则模式）。"""
    orchestrator = AgentOrchestrator(conn, request.app.state.bus, user,
                                     repo.get_current_semester(conn))
    return orchestrator.chat(body.message, body.history)


@api.post("/agent/actions")
def agent_action(body: AgentActionIn, request: Request, conn=Depends(get_conn),
                 user=Depends(require("agent:use"))):
    """用户确认后执行写操作：走正常服务层，RBAC 与约束策略链全量生效。"""
    svc = EnrollmentService(conn, request.app.state.bus)
    if body.tool == "submit_enrollment":
        if not role_has(user["role"], "enroll:create"):
            raise HTTPException(403, "当前角色无权提交选课申请")
        try:
            return svc.enroll(user, _int_arg(body.args, "section_id"))
        except DomainError as e:
            map_domain_error(e)
    if not role_has(user["role"], "enroll:drop_own"):
        raise HTTPException(403, "当前角色无权退课")
    try:
        return svc.drop(user, _int_arg(body.args, "enrollment_id"))
    except DomainError as e:
        map_domain_error(e)
