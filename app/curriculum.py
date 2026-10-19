"""开课编排（纯领域 / 语义维度）。

确定性、无随机、无时钟：给定课程目录与目标学期，按课程的「常规开课学期」
（FA/SP）过滤后，把每门课投入 1–2 个**与标准作息节次对齐**的每周上课块
（4 学分及以上两段，其余一段）。

- `PERIODS` / `period_label`：标准作息节次模板（与前端周课表一致）。
- `BLOCKS`：两节连排的上课块（第 1-2、3-4、5-6、7-8、9-10 节）。
- `build_offering`：把课程列表确定性地摊到一个教学班集合上。

同一时间块可容纳多门课程（真实大学开课中不同专业/教学班本就会撞车），
冲突只在**同一学生的选课组合**层面由 `policies` + 规划智能体消解。
"""
from __future__ import annotations

# 标准作息节次：(节次, 开始分钟, 结束分钟)。与前端 static/index.html 的 PERIODS 一致。
PERIODS: list[tuple[int, int, int]] = [
    (1, 480, 525), (2, 535, 580),
    (3, 600, 645), (4, 655, 700),
    (5, 840, 885), (6, 895, 940),
    (7, 960, 1005), (8, 1015, 1060),
    (9, 1140, 1185), (10, 1195, 1240),
]

# 上课块：两节连排，共 5 块/天（第1-2、3-4、5-6、7-8、9-10 节）
BLOCKS: list[tuple[int, int]] = [
    (480, 580), (600, 700), (840, 940), (960, 1060), (1140, 1240),
]

TEACHING_DAYS: list[int] = [1, 2, 3, 4, 5]      # 周一…周五

#: 授课教师池（与 demo 中 instructor 账号 林晓梅 对齐，便于名单演示）
INSTRUCTORS: list[str] = [
    "林晓梅", "陈志远", "王建国", "刘慧敏", "张晓东",
    "赵丹", "孙文博", "周倩", "吴海涛", "郑立",
]

#: 学院 -> 教学楼
_BUILDING: dict[str, str] = {
    "数学学院": "数理楼", "物理学院": "数理楼",
    "计算机学院": "计算机楼", "电子工程学院": "电子楼",
    "交叉学院": "交叉学科楼", "通识教育中心": "公共教学楼",
    "创新创业学院": "文体中心",
}
DEFAULT_BUILDING = "公共教学楼"


def period_label(start_min: int, end_min: int) -> str:
    """把分钟区间映射为「第 X-Y 节」；不属于任何节次时返回空串。"""
    hit = [(n, s, e) for (n, s, e) in PERIODS
           if start_min < e and s < end_min]
    if not hit:
        return ""
    first, last = hit[0][0], hit[-1][0]
    return f"第{first}节" if first == last else f"第{first}-{last}节"


def _venue(department: str, index: int) -> str:
    building = _BUILDING.get(department, DEFAULT_BUILDING)
    room = 101 + (index % 5) * 2 + (index // 5) % 2
    return f"{building}{room}"


def weekly_sessions(credits: float) -> int:
    """每门课的每周上课次数：4 学分及以上两段，其余一段。"""
    return 2 if float(credits) >= 4 else 1


def build_offering(courses: list[dict], semester: str, capacity: int = 60,
                   capacity_by_code: dict[str, int] | None = None) -> list[dict]:
    """为 `semester` 生成教学班（仅取常规开课学期匹配该学期的课程）。

    每门课投入 1–2 个每周上课块（4 学分及以上两段），时间格取自
    `BLOCKS × TEACHING_DAYS`（5 天 × 5 块 = 25 格）。课程数量远多于时间格，
    因此不同课程会在同一格撞车，选课组合层面的冲突由 policies/规划智能体消解。

    返回可直接交给 `repositories.create_section` 的 dict 列表；确定性：
    同输入 ⇒ 同输出。`capacity_by_code` 可对个别课程覆盖容量。
    """
    term = semester[-2:]
    caps = capacity_by_code or {}
    selected = sorted((c for c in courses
                       if (c.get("term") or "FA") == term),
                      key=lambda c: c["code"])
    combos = [(day, start, end)
              for day in TEACHING_DAYS
              for (start, end) in BLOCKS]
    sections: list[dict] = []
    for i, course in enumerate(selected):
        code = course["code"]
        day1, s1, e1 = combos[i % len(combos)]
        slots = [{"day": day1, "start_min": s1, "end_min": e1}]
        if weekly_sessions(course.get("credits", 3)) == 2:
            day2, s2, e2 = combos[(i * 7 + 3) % len(combos)]
            if (day2, s2) == (day1, s1):        # 与首选格错开
                day2, s2, e2 = combos[(i * 7 + 4) % len(combos)]
            slots.append({"day": day2, "start_min": s2, "end_min": e2})
        sections.append({
            "course_code": code,
            "semester": semester,
            "section_no": "01",
            "instructor_name": INSTRUCTORS[i % len(INSTRUCTORS)],
            "capacity": int(caps.get(code, capacity)),
            "venue": _venue(course.get("department", ""), i),
            "slots": slots,
        })
    return sections
