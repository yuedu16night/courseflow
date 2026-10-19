"""Application factory and wiring (composition root).

Wires infrastructure (SQLite, EventBus) to services and the HTTP layer.
The WaitlistEngine is registered as the sole subscriber of
`section.seat_released`, demonstrating event-driven orchestration.
"""
from __future__ import annotations

import os
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
ENV_FILE = Path(__file__).resolve().parent.parent / ".env.local"


def load_local_env(path: str | Path | None = None) -> None:
    """从项目根目录的 `.env.local` 载入运行时配置（已存在的环境变量优先）。

    行格式支持 `KEY=VALUE`（`#` 起首为注释）；若某行不含 `=`，则视其为裸的
    LLM 密钥并注入 `AGENT_LLM_API_KEY`，这样只往该文件粘贴一个 key 就能启用
    智能体的 LLM 通道。只在进程入口显式调用（`run.py`），因此测试直接调
    `create_app` 时不会被真实密钥污染，离线规则模式照常可复现。
    """
    env_path = Path(path) if path is not None else ENV_FILE
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
        else:
            key, value = "AGENT_LLM_API_KEY", line
        if key and value:
            os.environ.setdefault(key, value)


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
        # bootstrap：库内当前学期设置 + 对应学期行（PRE_ENROLLMENT）
        from app.runtime import CURRENT_SEMESTER
        if repo.get_setting(conn, repo.CURRENT_SEMESTER_KEY) is None:
            repo.set_current_semester(conn, CURRENT_SEMESTER)
        current = repo.get_current_semester(conn)
        if repo.get_semester(conn, current) is None:
            repo.upsert_semester(conn, current, Phase.PRE_ENROLLMENT.value)
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
