"""培养方案审核与毕业规划（语义 / 行为维度）。

纯领域模块：只依赖标准库与 `app.graph`，不触碰 SQLite、FastAPI 或任何
框架。两条核心能力都是确定性的（无随机、无时钟）：

- `audit_requirements`：把成绩单（已通过课程 + 学分）与培养方案的规则行
  逐条比对，给出每条规则的 satisfied / partial / unmet 三态、缺失课程、
  已获/所需学分，并汇总全局状态。
- `plan_graduation`：把待修课程按先修 DAG 拓扑排序后，确定性地装箱到
  未来若干学期（每学期学分上限 + 先修先后约束），不可行时给出诊断。

规则语义（与培养方案注册表一致）：
- `all_of`   —— `courses` 中每门都必须通过（必修清单）
- `choose_n` —— `courses` 中至少通过 `min_courses` 门（N 选一 / 选修池）
- `min_credits` —— 在 `courses` 池内至少取得 `min_credits` 学分

机器可读原因码保持英文（如 `DEGREE_REQ_UNMET`、`PREREQ_CYCLE`），便于
日志、审计与报告对照。
"""
from __future__ import annotations

from collections import defaultdict

from app.graph import topological_order

FA = "FA"
SP = "SP"

_SATISFIED = "satisfied"
_PARTIAL = "partial"
_UNMET = "unmet"

_REASON = {
    _SATISFIED: "DEGREE_REQ_SATISFIED",
    _PARTIAL: "DEGREE_REQ_PARTIAL",
    _UNMET: "DEGREE_REQ_UNMET",
}


# ---------------------------------------------------------------------------
# 学期码：SP / FA 年际交替（2026FA → 2027SP → 2027FA → 2028SP ...）
# ---------------------------------------------------------------------------
def next_semester_codes(start_semester: str, count: int) -> list[str]:
    """从 `start_semester` 起，按 SP/FA 交替生成 `count` 个学期码。"""
    if count <= 0:
        return []
    year = int(start_semester[:4])
    term = start_semester[4:]
    codes: list[str] = []
    for _ in range(count):
        codes.append(f"{year}{term}")
        if term == FA:
            year += 1
            term = SP
        else:                      # SP / SU：同年秋季（FA）为主学期继任
            term = FA
    return codes


# ---------------------------------------------------------------------------
# 培养方案审核
# ---------------------------------------------------------------------------
def _rule_credits(codes: list[str], course_credits: dict[str, float]) -> float:
    return float(sum(course_credits.get(c, 0.0) for c in codes))


def _audit_rule(rule: dict, completed: set[str],
                course_credits: dict[str, float]) -> dict:
    category = rule.get("category", "")
    rule_type = rule.get("rule_type", "")
    courses = list(rule.get("courses") or [])
    min_credits = rule.get("min_credits")
    min_courses = rule.get("min_courses")

    satisfied_courses = [c for c in courses if c in completed]
    missing_courses = [c for c in courses if c not in completed]
    credits_earned = _rule_credits(satisfied_courses, course_credits)
    met = len(satisfied_courses)

    if rule_type == "all_of":
        total = len(courses)
        if met == total:
            status, required = _SATISFIED, []
        elif met == 0:
            status, required = _UNMET, list(missing_courses)
        else:
            status, required = _PARTIAL, list(missing_courses)
        credits_needed = _rule_credits(required, course_credits)

    elif rule_type == "choose_n":
        need = int(min_courses or 0)
        if met >= need:
            status, required = _SATISFIED, []
        elif met == 0 and need > 0:
            status, required = _UNMET, _cheapest(missing_courses, need,
                                                 course_credits)
        else:
            status, required = _PARTIAL, _cheapest(missing_courses,
                                                   need - met, course_credits)
        credits_needed = _rule_credits(required, course_credits)

    elif rule_type == "min_credits":
        need = float(min_credits or 0.0)
        if credits_earned >= need:
            status, required = _SATISFIED, []
        else:
            status = _UNMET if credits_earned <= 0 and need > 0 else _PARTIAL
            # 只列出"补足缺口所需的最少课程"（按学分从高到低累加），
            # 而不是整个选修池；否则毕业路线图会把池内所有课程都当成必修，
            # 待修学分被严重高估，导致排不进任何学期。
            required = _cover_credits(missing_courses,
                                      max(0.0, need - credits_earned),
                                      course_credits)
        credits_needed = max(0.0, need - credits_earned)

    else:                                     # 未知规则类型：保守视为未满足
        status, required = _UNMET, list(missing_courses)
        credits_needed = _rule_credits(missing_courses, course_credits)

    return {
        "category": category,
        "rule_type": rule_type,
        "courses": courses,
        "min_credits": min_credits,
        "min_courses": min_courses,
        "status": status,
        "reason_code": _REASON[status],
        "met_count": met,
        "satisfied_courses": satisfied_courses,
        "missing_courses": missing_courses,
        "required_courses": required,
        "credits_earned": credits_earned,
        "credits_needed": credits_needed,
    }


def _cheapest(codes: list[str], k: int,
              course_credits: dict[str, float]) -> list[str]:
    """从候选课程中挑 k 门学分最低的（同分按代码字典序），确定性。"""
    if k <= 0:
        return []
    ordered = sorted(codes, key=lambda c: (course_credits.get(c, 0.0), c))
    return ordered[:k]


def _cover_credits(codes: list[str], target_credits: float,
                   course_credits: dict[str, float]) -> list[str]:
    """挑出"补足 target_credits 所需的最少课程"。

    按学分从高到低累加（同分按代码字典序），确定性；相比按学分从低到高
    能显著减少所需门数，毕业路线图因此更短、更接近真实选课安排。
    """
    if target_credits <= 0:
        return []
    ordered = sorted(codes, key=lambda c: (-course_credits.get(c, 0.0), c))
    picked: list[str] = []
    acc = 0.0
    for code in ordered:
        if acc >= target_credits:
            break
        picked.append(code)
        acc += course_credits.get(code, 0.0)
    return picked


def audit_requirements(completed: set[str], course_credits: dict[str, float],
                       rules: list[dict],
                       total_credits_min: float | None = None) -> dict:
    """逐条审核培养方案规则并汇总。

    `total_credits_min` 为培养方案表头的总学分下限；未提供时退化为
    各 `min_credits` 规则下限之和。
    """
    rule_results = [_audit_rule(r, completed, course_credits) for r in rules]

    if total_credits_min is None:
        total_credits_min = float(sum(
            r.get("min_credits") or 0.0 for r in rules
            if r.get("rule_type") == "min_credits"))
    total_credits_required = float(total_credits_min)
    total_credits_earned = float(sum(course_credits.get(c, 0.0)
                                     for c in completed))

    statuses = [r["status"] for r in rule_results]
    credits_ok = total_credits_earned >= total_credits_required
    if all(s == _SATISFIED for s in statuses) and credits_ok:
        overall = _SATISFIED
    elif rule_results and all(s == _UNMET for s in statuses):
        overall = _UNMET
    else:
        overall = _PARTIAL

    unmet_rules = [r for r in rule_results if r["status"] != _SATISFIED]
    missing: set[str] = set()
    for r in rule_results:
        missing.update(r["required_courses"])

    return {
        "status": overall,
        "reason_code": _REASON[overall],
        "rules": rule_results,
        "unmet_rules": unmet_rules,
        "missing_courses": sorted(missing),
        "total_credits_earned": total_credits_earned,
        "total_credits_required": total_credits_required,
        "total_credits_gap": max(0.0, total_credits_required
                                 - total_credits_earned),
    }


# ---------------------------------------------------------------------------
# 毕业规划：拓扑排序 + 每学期学分上限装箱
# ---------------------------------------------------------------------------
def plan_graduation(missing_courses: list[dict], prereq_graph: dict[str, set[str]],
                    per_semester_cap: int, start_semester: str,
                    max_semesters: int) -> dict:
    """把待修课程确定性排入未来若干学期。

    `prereq_graph` 为「先修 -> 后继」的有向边（与 `repositories.course_graph`
    同构）；仅保留两端都在 `missing_courses` 中的边构成子图。
    """
    courses = {c["code"]: c for c in missing_courses}
    diagnostics: list[dict] = []
    if not courses:
        return {"semesters": [], "feasible": True, "diagnostics": [],
                "start_semester": start_semester}

    # 子图：所有待修课程都是节点，只保留子图内部的先修边
    sub: dict[str, set[str]] = {code: set() for code in courses}
    for pre, dsts in (prereq_graph or {}).items():
        if pre in sub:
            sub[pre] |= {d for d in dsts if d in sub}

    try:
        order = topological_order(sub)
    except ValueError as exc:
        diagnostics.append({
            "code": "PREREQ_CYCLE",
            "message": f"待修课程之间存在先修环，无法拓扑排序：{exc}",
        })
        return {"semesters": [], "feasible": False, "diagnostics": diagnostics,
                "start_semester": start_semester}

    # 每门课的先修（子图内部）：先修 -> 后继，反向求出
    prereqs_of: dict[str, set[str]] = {c: set() for c in courses}
    for pre, dsts in sub.items():
        for dst in dsts:
            prereqs_of[dst].add(pre)

    semester_codes = next_semester_codes(start_semester, max(0, max_semesters))
    slot_credits: list[float] = [0.0] * max(0, max_semesters)
    slot_courses: list[list[dict]] = [[] for _ in range(max(0, max_semesters))]
    placed: dict[str, int] = {}

    for code in order:
        course = courses[code]
        internal_pre = [p for p in prereqs_of[code]]
        blocked = [p for p in internal_pre if p not in placed]
        if blocked:
            diagnostics.append({
                "code": "PREREQ_UNSCHEDULED",
                "course": code,
                "message": (f"课程 {code} 的先修 {sorted(blocked)} 未能排入学期，"
                            f"无法安排"),
            })
            continue

        earliest = 0
        for p in internal_pre:
            earliest = max(earliest, placed[p] + 1)

        chosen_idx = None
        for idx in range(earliest, len(slot_credits)):
            if slot_credits[idx] + course["credits"] <= per_semester_cap:
                chosen_idx = idx
                break
        if chosen_idx is None:
            diagnostics.append({
                "code": "SEMESTER_LIMIT",
                "course": code,
                "message": (f"课程 {code}（{course['credits']:g} 学分）在 "
                            f"{max_semesters} 个学期、每学期 {per_semester_cap:g} "
                            f"学分的上限内无法排入（先修先后约束或学分不足）"),
            })
            continue

        placed[code] = chosen_idx
        slot_credits[chosen_idx] += course["credits"]
        slot_courses[chosen_idx].append({
            "code": code, "title": course.get("title", ""),
            "credits": course["credits"],
        })

    semesters = [
        {"semester": semester_codes[i], "courses": slot_courses[i],
         "total_credits": slot_credits[i]}
        for i in range(len(slot_credits)) if slot_courses[i]
    ]
    feasible = len(placed) == len(courses)
    return {
        "semesters": semesters,
        "feasible": feasible,
        "diagnostics": diagnostics,
        "start_semester": start_semester,
    }
