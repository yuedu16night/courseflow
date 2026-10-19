"""Pydantic models: the formal schema layer (semantic dimension).

These models act as the data registry contract for everything entering
the system through the API boundary. `CourseIn.model_json_schema()`
is exported to docs/schemas at build time as the formal JSON Schema
artifact referenced in the architecture report.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=6, max_length=64)
    full_name: str = Field(min_length=1, max_length=64)
    role: str = Field(pattern="^(student|instructor|registrar|admin)$")
    major: str | None = None
    grade: int | None = Field(default=None, ge=1, le=4)   # 年级 1..4（大一..大四）
    max_credits: int = Field(default=35, ge=1, le=60)


class LoginIn(BaseModel):
    username: str
    password: str


class ScheduleSlot(BaseModel):
    """One weekly meeting occurrence of a section.

    `day` uses ISO order: 1=Monday ... 7=Sunday. Times are minutes
    since midnight, e.g. 08:30 -> 510.
    """
    day: int = Field(ge=1, le=7)
    start_min: int = Field(ge=0, le=1440)
    end_min: int = Field(gt=0, le=1440)

    @model_validator(mode="after")
    def check_interval(self) -> "ScheduleSlot":
        if self.end_min <= self.start_min:
            raise ValueError("end_min must be greater than start_min")
        return self


class CourseIn(BaseModel):
    """Catalog entry. Creating a course with an existing `code`
    produces a NEW catalog version (immutability + versioning)."""
    code: str = Field(min_length=2, max_length=16, pattern=r"^[A-Z]{2,4}[0-9]{2,4}$")
    title: str = Field(min_length=2, max_length=128)
    credits: float = Field(ge=0.5, le=12)
    department: str = Field(min_length=2, max_length=64)
    category: str = Field(default="", max_length=32)      # 课程类别（培养方案口径）
    term: str = Field(default="FA", pattern="^(FA|SP)$")  # 常规开课学期
    prerequisites: list[str] = Field(default_factory=list)
    description: str = ""


class SectionIn(BaseModel):
    course_code: str
    semester: str = Field(pattern=r"^\d{4}(SP|FA|SU)$")
    section_no: str = Field(min_length=1, max_length=8)
    instructor_name: str
    capacity: int = Field(ge=1, le=500)
    venue: str
    slots: list[ScheduleSlot] = Field(min_length=1)


class EnrollmentIn(BaseModel):
    section_id: int


# ---------------------------------------------------------------------------
# agent layer (scheme A: planning, scheme B: LLM orchestration)
# ---------------------------------------------------------------------------
class AgentPlanIn(BaseModel):
    max_credits: int | None = Field(default=None, ge=1, le=60)
    top_k: int = Field(default=3, ge=1, le=10)


class AgentChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list)


class AgentActionIn(BaseModel):
    """用户确认后的写操作：只接受白名单工具与参数。"""
    tool: str = Field(pattern="^(submit_enrollment|drop_enrollment)$")
    args: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# degree requirements & timetable planning (formal input contracts)
# ---------------------------------------------------------------------------
class DegreeRuleIn(BaseModel):
    """培养方案的一条规则行。"""
    category: str = Field(min_length=1, max_length=64)
    rule_type: str = Field(pattern="^(all_of|choose_n|min_credits)$")
    courses: list[str] = Field(default_factory=list)
    min_credits: float | None = Field(default=None, ge=0)
    min_courses: int | None = Field(default=None, ge=0)


class DegreeRequirementIn(BaseModel):
    """教务新建培养方案版本：major 相同即产生新版本（旧版本自动失效）。"""
    major: str = Field(min_length=1, max_length=64)
    total_credits_min: float = Field(ge=0)
    label: str = Field(default="", max_length=128)
    rules: list[DegreeRuleIn] = Field(min_length=1)


class TimetablePlanIn(BaseModel):
    """无冲突课表规划入参：给定本学期要上的课程代码集合。"""
    course_codes: list[str] = Field(min_length=1)
