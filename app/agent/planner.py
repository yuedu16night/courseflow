"""PlannerAgent（方案 A）：确定性的选课组合规划。

定位：位于服务层之上的**组合规划**组件，把系统原有的「单次选课校验」
升级为「多课程组合搜索」——在同时满足全部语义约束的前提下，为学生
枚举可行的选课组合，并为每个组合给出机器可读的理由码与人类可读的
解释。

设计约束（与整体架构一致）：
- 只读：规划过程绝不写入数据库，也不改变任何状态；
- 确定性：相同世界状态 + 相同参数 ⇒ 完全一致的输出（无随机、无时间）；
- 可测试：不依赖网络、不依赖 LLM，pytest 可直接覆盖；
- 分层：只经 repositories / policies / graph 访问领域事实。

搜索模型：每门「当前可选」课程是一个决策点（跳过 / 取某个教学班），
在学分上限与课表区间不重叠的约束下做带回溯的深度优先枚举，节点数
有硬上限，保证最坏情况仍然可控。
"""
from __future__ import annotations

from app import repositories as repo
from app.enums import EnrollmentState as S
from app.models import ScheduleSlot
from app.policies import slots_overlap

DEFAULT_TOP_K = 3
MAX_SEARCH_NODES = 50_000
PREREQ_FRONTIER_LIMIT = 8

# 这些状态意味着「本学期该课程已经占了位置」，不应再次规划
_OCCUPYING_STATES = {
    S.SUBMITTED.value,
    S.VALIDATED.value,
    S.ENROLLED.value,
    S.WAITLISTED.value,
}

_DAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _hh(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_slots(slots: list[dict]) -> str:
    return "，".join(
        f"{_DAY_ZH[s['day'] - 1]} {_hh(s['start_min'])}-{_hh(s['end_min'])}"
        for s in slots
    )


class PlannerAgent:
    """为一组给定约束生成可行选课组合（纯只读，确定性）。"""

    def __init__(self, conn, semester: str):
        self.conn = conn
        self.semester = semester

    # ------------------------------------------------------------------ plan
    def plan(self, user: dict, max_credits: int | None = None,
             top_k: int = DEFAULT_TOP_K) -> dict:
        budget = int(max_credits or user["max_credits"])
        courses = repo.list_active_courses(self.conn)
        completed = set(user["completed_courses"])
        taken = self._taken_codes(user)
        sections_by_course = self._sections_by_course()

        eligible: list[dict] = []
        not_offered: list[str] = []
        blocked: list[dict] = []

        for course in sorted(courses, key=lambda c: c["code"]):
            code = course["code"]
            if code in completed or code in taken:
                continue                      # 已通过 / 本学期已占位：不进规划
            missing = sorted(p for p in course["prerequisites"] if p not in completed)
            if missing:
                blocked.append({
                    "course": code,
                    "title": course["title"],
                    "missing_prerequisites": missing,
                    "reason_code": "PREREQ_MISSING",
                    "message": f"缺少先修课程 {'、'.join(missing)}（先修 DAG 未满足）",
                })
                continue
            if code not in sections_by_course:
                not_offered.append(code)
                continue
            eligible.append(course)

        candidates = [(c, sections_by_course[c["code"]]) for c in eligible]
        ranked = self._search(candidates, budget, top_k)
        plans = [self._render_plan(rank, credits, picks, completed, courses)
                 for rank, (credits, picks) in enumerate(ranked, start=1)]

        semester_row = repo.get_semester(self.conn, self.semester) or {}
        return {
            "student": user["username"],
            "semester": self.semester,
            "phase": semester_row.get("phase"),
            "max_credits": budget,
            "completed_courses": sorted(completed),
            "current_courses": sorted(taken),
            "plans": plans,
            "blocked_by_prerequisites": blocked[:PREREQ_FRONTIER_LIMIT],
            "not_offered": not_offered,
            "excluded_from_best_plan": (
                self._excluded(eligible, plans[0], sections_by_course, budget)
                if plans else []
            ),
        }

    # ------------------------------------------------------------ 搜索内核
    def _search(self, candidates: list[tuple[dict, list[dict]]],
                budget: int, top_k: int) -> list[tuple[float, tuple]]:
        """带回溯的 DFS 枚举，返回 (学分, 选择) 并按确定性规则排序。"""
        found: list[tuple[float, tuple]] = []
        nodes = 0
        total = len(candidates)

        def rec(i: int, picks: list, credits: float, slots: list[ScheduleSlot]) -> None:
            nonlocal nodes
            nodes += 1
            if nodes > MAX_SEARCH_NODES:
                return
            if i == total:
                if picks:
                    found.append((credits, tuple(picks)))
                return
            course, sections = candidates[i]
            rec(i + 1, picks, credits, slots)          # 分支一：跳过该课程
            if credits + course["credits"] > budget:
                return                                  # 剪枝：学分上限
            for section in sections:                    # 分支二：取某个教学班
                section_slots = [ScheduleSlot(**s) for s in section["slots"]]
                if any(slots_overlap(a, b)
                       for a in section_slots for b in slots):
                    continue                            # 剪枝：课表冲突
                picks.append((course, section))
                rec(i + 1, picks, credits + course["credits"],
                    slots + section_slots)
                picks.pop()

        rec(0, [], 0.0, [])

        # 排序键：学分优先（用满学习承载力）→ 有余席的教学班更多 → 代码字典序
        # 三个键都是确定性的，保证同输入同输出。
        found.sort(key=lambda item: (
            -item[0],
            -self._seat_bonus(item[1]),
            self._stable_key(item[1]),
        ))
        return found[:top_k]

    def _seat_bonus(self, picks: tuple) -> int:
        return sum(
            1 for _, section in picks
            if section["capacity"] - repo.count_enrolled(self.conn, section["id"]) > 0
        )

    @staticmethod
    def _stable_key(picks: tuple) -> tuple:
        return tuple(sorted(f"{c['code']}.{s['section_no']}" for c, s in picks))

    # -------------------------------------------------------------- 渲染
    def _render_plan(self, rank: int, credits: float, picks: tuple,
                     completed: set[str], courses: list[dict]) -> dict:
        items = []
        for course, section in picks:
            free = section["capacity"] - repo.count_enrolled(self.conn, section["id"])
            reasons = []
            if course["prerequisites"]:
                reasons.append(
                    f"先修课程 {'、'.join(course['prerequisites'])} 均已通过（先修 DAG 满足）")
            else:
                reasons.append("无先修课程要求")
            reasons.append(
                f"教学班 {section['section_no']}，{section['venue']}，"
                f"{format_slots(section['slots'])}")
            reasons.append(
                f"当前余 {free} 席" if free > 0
                else "当前已满：预选阶段仍可提交志愿，席位由抽签/候补决定")
            items.append({
                "course": course["code"],
                "title": course["title"],
                "credits": course["credits"],
                "section_id": section["id"],
                "section_no": section["section_no"],
                "venue": section["venue"],
                "slots": section["slots"],
                "free_seats": free,
                "reasons": reasons,
            })

        chosen = {i["course"] for i in items}
        return {
            "rank": rank,
            "total_credits": credits,
            "picks": items,
            "unlocks": self._unlocks(courses, completed, chosen),
            "explanation": (
                f"共 {credits:g} 学分、{len(items)} 门课程，"
                f"课表区间两两不冲突，且已通过全部先修与学分约束校验"
            ),
        }

    @staticmethod
    def _unlocks(courses: list[dict], completed: set[str],
                 plan_codes: set[str]) -> list[str]:
        """本方案修完后，哪些课程会新变为可选（先修增益）。"""
        after = completed | plan_codes
        out = []
        for course in courses:
            code = course["code"]
            if code in after:
                continue
            missing_now = [p for p in course["prerequisites"] if p not in completed]
            if missing_now and all(p in after for p in missing_now):
                out.append(code)
        return sorted(out)

    def _excluded(self, eligible: list[dict], best: dict,
                  sections_by_course: dict, budget: int) -> list[dict]:
        """第 1 方案之外，其余可选课程为何没被选上。"""
        chosen_codes = {p["course"] for p in best["picks"]}
        chosen_slots = [ScheduleSlot(**s)
                        for p in best["picks"] for s in p["slots"]]
        credits = best["total_credits"]
        out = []
        for course in eligible:
            if course["code"] in chosen_codes:
                continue
            if credits + course["credits"] > budget:
                out.append({
                    "course": course["code"],
                    "reason_code": "CREDIT_LIMIT",
                    "message": (f"再加 {course['code']}（{course['credits']:g} 学分）"
                                f"将达到 {credits + course['credits']:g}，"
                                f"超过上限 {budget} 学分"),
                })
                continue
            clash = None
            for section in sections_by_course.get(course["code"], []):
                section_slots = [ScheduleSlot(**s) for s in section["slots"]]
                if any(slots_overlap(a, b)
                       for a in section_slots for b in chosen_slots):
                    clash = section
                    break
            if clash is not None:
                out.append({
                    "course": course["code"],
                    "reason_code": "TIMETABLE_CONFLICT",
                    "message": (f"{course['code']} 的全部教学班都与第 1 方案"
                                f"课表重叠（如 {course['code']}.{clash['section_no']} "
                                f"{format_slots(clash['slots'])}）"),
                })
            else:
                out.append({
                    "course": course["code"],
                    "reason_code": "LOWER_RANK",
                    "message": f"{course['code']} 本身可行，但所属组合的排序评分更低",
                })
        return out

    # -------------------------------------------------------------- 事实读取
    def _taken_codes(self, user: dict) -> set[str]:
        codes = set()
        for row in repo.list_enrollments(self.conn, student_id=user["id"],
                                         semester=self.semester):
            if row["state"] not in _OCCUPYING_STATES:
                continue
            section = repo.get_section(self.conn, row["section_id"])
            if section:
                codes.add(section["course_code"])
        return codes

    def _sections_by_course(self) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for section in repo.list_sections(self.conn, semester=self.semester):
            grouped.setdefault(section["course_code"], []).append(section)
        return grouped
