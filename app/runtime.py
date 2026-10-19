"""Runtime configuration shared across requests.

当前学期现已持久化在库内 `app_settings` 表（可随「推进学期」改变，
见 `repositories.get_current_semester` / `SemesterService.advance`）。
`CURRENT_SEMESTER` 仅作为全新数据库的初始学期值。
"""
CURRENT_SEMESTER = "2026FA"
