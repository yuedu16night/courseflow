"""无冲突课表规划（语义 / 行为维度）。

纯领域模块：不依赖 SQLite 与 FastAPI，只依赖标准库、`app.models.ScheduleSlot`
与 `app.policies.slots_overlap`。

`plan_timetable` 回答一个具体问题：给定一组必上课程，为每门各选一个教学班，
使任意两门课的每周上课区间两两不重叠；在可行解中优先选择余席较多的教学班。
采用带回溯的深度优先搜索（有界 CSP），节点数有硬上限，最坏情况仍然可控；
整个过程确定性（无随机、无时钟），相同输入必定得到相同输出。
"""
from __future__ import annotations

from app.models import ScheduleSlot
from app.policies import slots_overlap

MAX_SEARCH_NODES = 50_000


def _slots(section: dict) -> list[ScheduleSlot]:
    out = []
    for s in section.get("slots") or []:
        out.append(s if isinstance(s, ScheduleSlot) else ScheduleSlot(**s))
    return out


def _free_seats(section: dict, enrolled_counts: dict[int, int]) -> int:
    return int(section["capacity"]) - int(enrolled_counts.get(section["id"], 0))


def plan_timetable(target_codes: list[str], sections_by_course: dict[str, list[dict]],
                   enrolled_counts: dict[int, int] | None = None) -> dict:
    """为每门目标课程选一个教学班，保证周课表区间两两不重叠。

    返回：`assignments`（每门课的教学班）、`weekly_grid`（合并周课表）、
    `feasible` 与 `diagnostics`（不可行时的人类可读原因）。
    """
    enrolled_counts = enrolled_counts or {}
    codes = sorted(set(target_codes))

    # 候选教学班：按余席降序（偏好余席多）-> 班号 -> id，确定性
    candidates: dict[str, list[dict]] = {}
    for code in codes:
        secs = list(sections_by_course.get(code) or [])
        secs.sort(key=lambda s: (
            -_free_seats(s, enrolled_counts),
            str(s.get("section_no", "")),
            int(s["id"]),
        ))
        candidates[code] = secs

    assignment: dict[str, dict] = {}
    chosen_slots: list[ScheduleSlot] = []
    best: dict[str, dict] | None = None
    nodes = 0

    def rec(i: int) -> None:
        nonlocal nodes, best
        nodes += 1
        if best is not None or nodes > MAX_SEARCH_NODES:
            return
        if i == len(codes):
            best = dict(assignment)
            return
        code = codes[i]
        for section in candidates[code]:
            sec_slots = _slots(section)
            if any(slots_overlap(a, b) for a in sec_slots for b in chosen_slots):
                continue
            assignment[code] = section
            chosen_slots.extend(sec_slots)
            rec(i + 1)
            del chosen_slots[len(chosen_slots) - len(sec_slots):]
            assignment.pop(code, None)
            if best is not None:
                return

    rec(0)

    if best is None:
        return {
            "assignments": [],
            "weekly_grid": [],
            "feasible": False,
            "diagnostics": _diagnostics(codes, candidates),
        }

    assignments = []
    for code in codes:
        section = best[code]
        assignments.append({
            "course": code,
            "section_id": section["id"],
            "section_no": section["section_no"],
            "venue": section.get("venue", ""),
            "slots": section.get("slots") or [],
            "free_seats": _free_seats(section, enrolled_counts),
        })

    grid = []
    for item in assignments:
        for slot in item["slots"]:
            grid.append({
                "day": slot["day"], "start_min": slot["start_min"],
                "end_min": slot["end_min"], "course": item["course"],
                "section_no": item["section_no"], "venue": item["venue"],
            })
    grid.sort(key=lambda x: (x["day"], x["start_min"], x["course"]))

    return {
        "assignments": assignments,
        "weekly_grid": grid,
        "feasible": True,
        "diagnostics": [],
    }


def _diagnostics(codes: list[str],
                 candidates: dict[str, list[dict]]) -> list[dict]:
    """不可行时给出人类可读诊断：指出缺失教学班的课程与必然冲突的课程对。"""
    out: list[dict] = []
    for code in codes:
        if not candidates[code]:
            out.append({
                "code": "SECTION_UNKNOWN",
                "courses": [code],
                "message": f"课程 {code} 本学期没有教学班，无法排课",
            })

    available = [c for c in codes if candidates[c]]
    for i in range(len(available)):
        for j in range(i + 1, len(available)):
            a, b = available[i], available[j]
            if _all_pairs_conflict(candidates[a], candidates[b]):
                out.append({
                    "code": "TIMETABLE_CONFLICT",
                    "courses": [a, b],
                    "message": (f"{a} 与 {b} 的全部教学班两两冲突："
                                f"无论怎么选都会周课表重叠"),
                })

    if not out:
        out.append({
            "code": "TIMETABLE_INFEASIBLE",
            "courses": available,
            "message": ("在课表区间不重叠的前提下，无法为全部课程同时找到"
                        "教学班组合（存在多门课程的连锁冲突）"),
        })
    return out


def _sections_overlap(a: dict, b: dict) -> bool:
    return any(slots_overlap(x, y) for x in _slots(a) for y in _slots(b))


def _all_pairs_conflict(a_secs: list[dict], b_secs: list[dict]) -> bool:
    """两门课的任意教学班组合都冲突（即两者无法共存）。"""
    if not a_secs or not b_secs:
        return False
    return all(_sections_overlap(sa, sb)
               for sa in a_secs for sb in b_secs)
