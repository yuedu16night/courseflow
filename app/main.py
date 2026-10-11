"""Application factory and wiring (composition root).

Wires infrastructure (SQLite, EventBus) to services and the HTTP layer.
The WaitlistEngine is registered as the sole subscriber of
`section.seat_released`, demonstrating event-driven orchestration.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import api, repositories as repo
from app.database import connect, init_db
from app.enums import Phase
from app.events import EventBus
from app.services import EnrollmentService
from app.waitlist import WaitlistEngine

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(db_path: str | Path = "courseflow.db") -> FastAPI:
    app = FastAPI(
        title="CourseFlow",
        description="Course registration & timetable orchestration system "
                    "(MAiNS macroscopic system modeling project)",
        version="1.0.0",
    )
    init_db(db_path)
    app.state.db_factory = lambda: connect(db_path)
    app.state.bus = EventBus()

    conn = app.state.db_factory()
    try:
        # bootstrap the active semester in PRE_ENROLLMENT if missing
        from app.runtime import CURRENT_SEMESTER
        if repo.get_semester(conn, CURRENT_SEMESTER) is None:
            repo.upsert_semester(conn, CURRENT_SEMESTER, Phase.PRE_ENROLLMENT.value)
        # default accounts so the demo UI works out of the box
        if not repo.list_users(conn):
            repo.create_user(conn, "admin", "admin123", "System Admin", "admin")
            repo.create_user(conn, "registrar", "reg123", "Office Registrar", "registrar")
        # wire the event-driven waitlist engine (connection factory keeps
        # SQLite thread-safety: each event opens its own connection)
        engine = WaitlistEngine(app.state.db_factory, app.state.bus)
        engine.register()
    finally:
        conn.close()

    app.include_router(api.api, prefix="/api")
    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True),
                  name="static")
    return app
