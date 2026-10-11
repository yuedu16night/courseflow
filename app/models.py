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
