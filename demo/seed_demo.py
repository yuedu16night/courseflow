"""End-to-end demo: seeds a realistic semester and walks a scripted
scenario through the public REST API, printing a human-readable trace.

Run:  python demo/seed_demo.py [db_path] [--seed-only]
      --seed-only populates users/transcripts/courses/degree programs/sections
      and leaves the current semester in pre_enrollment (ideal before starting
      the live server).
Output: console + docs/demo_trace.md (execution evidence for the report)

教学建模要点（与需求对应）：
- 每节课时间与标准作息节次对齐（curriculum.build_offering 投放的上课块，
  4 学分及以上每周两段，其余一段）。
- 122 门中文课程，分五类：主修课程 / 专业选修课 / 交叉课程 / 通识课程 / 个性化课程。
- 学生跨大一…大四（含 1 名 EE 学生），成绩单按年级递进。
- 每个专业一份培养方案：主修核心课程全部必修，四类选修只设较低学分下限，
  且新增课程不设先修，学生选课余地更大（总学分下限随核心学分自动汇总）。
- 可「推进学期」：进入下一学期并自动按课程常规开课学期开课；跨学年自动升年级。
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import repositories as repo  # noqa: E402
from app.curriculum import build_offering  # noqa: E402
from app.main import create_app  # noqa: E402

DB = ROOT / "demo.db"
TRACE = ROOT / "docs" / "demo_trace.md"
out_lines: list[str] = []

# ---------------------------------------------------------------------------
# 课程目录：122 门中文课，五类、常规开课学期 FA/SP、先修 DAG（无环）
# 字段顺序：(code, title, credits, category, term, prerequisites)
# ---------------------------------------------------------------------------
CORE = "主修课程"
ELECTIVE = "专业选修课"
CROSS = "交叉课程"
GENED = "通识课程"
PERSONAL = "个性化课程"

COURSES: list[tuple] = [
    # ---- 主修课程 · 数理基础（CS 与 EE 共享先修底座）----
    ("MATH101", "高等数学A(一)", 5, CORE, "FA", []),
    ("MATH102", "高等数学A(二)", 5, CORE, "SP", ["MATH101"]),
    ("MATH201", "线性代数", 3, CORE, "FA", []),
    ("MATH202", "概率论与数理统计", 3, CORE, "SP", ["MATH201"]),
    ("PHY101", "大学物理(一)", 4, CORE, "FA", []),
    ("PHY102", "大学物理(二)", 4, CORE, "SP", ["PHY101"]),
    ("PHY201", "大学物理实验", 2, CORE, "SP", ["PHY101"]),
    # ---- 主修课程 · 计算机 ----
    ("CS101", "程序设计基础", 4, CORE, "FA", []),
    ("CS102", "面向对象程序设计", 4, CORE, "SP", ["CS101"]),
    ("CS104", "计算思维导论", 2, CORE, "FA", []),
    ("CS105", "高级程序设计", 2, CORE, "SP", ["CS101"]),
    ("CS201", "数据结构与算法", 4, CORE, "FA", ["CS101"]),
    ("CS202", "计算机组成原理", 4, CORE, "SP", ["CS101"]),
    ("CS211", "离散数学", 3, CORE, "FA", ["MATH101"]),
    ("CS212", "操作系统", 4, CORE, "SP", ["CS201"]),
    ("CS301", "计算机网络", 3, CORE, "FA", ["CS202"]),
    ("CS302", "数据库系统", 3, CORE, "SP", ["CS201"]),
    ("CS311", "软件工程", 4, CORE, "FA", ["CS201"]),
    ("CS312", "算法设计与分析", 3, CORE, "SP", ["CS201"]),
    ("CS321", "编译原理", 4, CORE, "SP", ["CS211"]),
    ("CS401", "毕业设计(计算机)", 12, CORE, "SP", ["CS311"]),
    # ---- 主修课程 · 电子信息 ----
    ("EE101", "电路分析基础", 4, CORE, "FA", []),
    ("EE102", "模拟电子技术", 4, CORE, "SP", ["EE101"]),
    ("EE111", "电工电子实习", 3, CORE, "SP", ["EE101"]),
    ("EE201", "数字电子技术", 4, CORE, "FA", ["EE101"]),
    ("EE202", "信号与系统", 4, CORE, "SP", ["MATH102"]),
    ("EE211", "电磁场与电磁波", 4, CORE, "FA", ["PHY102"]),
    ("EE221", "微机原理与接口技术", 4, CORE, "SP", ["EE201"]),
    ("EE301", "通信原理", 4, CORE, "FA", ["EE202"]),
    ("EE302", "自动控制原理", 4, CORE, "SP", ["EE202"]),
    ("EE311", "电力电子技术", 4, CORE, "FA", ["EE201"]),
    ("EE321", "数字信号处理", 4, CORE, "SP", ["EE202"]),
    ("EE331", "嵌入式系统设计", 4, CORE, "FA", ["EE221"]),
    ("EE401", "毕业设计(电子信息)", 12, CORE, "SP", ["EE301"]),
    # ---- 专业选修课 · 计算机 ----
    ("CS331", "人工智能导论", 3, ELECTIVE, "FA", ["CS201"]),
    ("CS332", "机器学习", 3, ELECTIVE, "SP", ["CS331"]),
    ("CS341", "计算机图形学", 3, ELECTIVE, "FA", ["CS201"]),
    ("CS342", "计算机视觉", 3, ELECTIVE, "SP", ["CS331"]),
    ("CS351", "云计算技术", 3, ELECTIVE, "FA", ["CS301"]),
    ("CS352", "大数据技术", 3, ELECTIVE, "SP", ["CS302"]),
    ("CS361", "信息安全", 3, ELECTIVE, "SP", ["CS301"]),
    # ---- 专业选修课 · 电子信息 ----
    ("EE341", "射频电路设计", 3, ELECTIVE, "FA", ["EE211"]),
    ("EE342", "微波技术", 3, ELECTIVE, "SP", ["EE341"]),
    ("EE351", "数字图像处理", 3, ELECTIVE, "FA", ["EE202"]),
    ("EE352", "模式识别", 3, ELECTIVE, "SP", ["EE202"]),
    ("EE361", "电力系统分析", 3, ELECTIVE, "FA", ["EE301"]),
    ("EE362", "高电压技术", 3, ELECTIVE, "SP", ["EE311"]),
    ("EE371", "机器人学导论", 3, ELECTIVE, "FA", ["EE302"]),
    # ---- 交叉课程 ----
    ("IC301", "计算神经科学", 3, CROSS, "SP", []),
    ("IC302", "生物信息学", 3, CROSS, "FA", []),
    ("IC311", "智能硬件设计", 3, CROSS, "SP", ["EE201"]),
    ("IC312", "数据科学导论", 3, CROSS, "FA", ["MATH202"]),
    ("IC321", "量子计算导论", 3, CROSS, "FA", ["MATH201"]),
    ("IC322", "类脑智能", 3, CROSS, "SP", ["IC301"]),
    # ---- 通识课程 ----
    ("GE101", "思想道德与法治", 3, GENED, "FA", []),
    ("GE102", "中国近现代史纲要", 3, GENED, "SP", []),
    ("GE103", "马克思主义基本原理", 3, GENED, "FA", []),
    ("GE104", "毛泽东思想和中国特色社会主义理论体系概论", 3, GENED, "SP", []),
    ("GE201", "大学英语(一)", 2, GENED, "FA", []),
    ("GE202", "大学英语(二)", 2, GENED, "SP", ["GE201"]),
    ("GE203", "体育(一)", 1, GENED, "FA", []),
    ("GE204", "体育(二)", 1, GENED, "SP", ["GE203"]),
    ("GE301", "大学语文", 2, GENED, "FA", []),
    ("GE302", "军事理论", 2, GENED, "SP", []),
    ("GE401", "创新创业基础", 2, GENED, "FA", []),
    ("GE402", "职业发展与就业指导", 1, GENED, "SP", []),
    ("GE501", "艺术鉴赏", 2, GENED, "FA", []),
    ("GE601", "心理健康教育", 2, GENED, "SP", []),
    ("GE602", "自然科学导论", 2, GENED, "FA", []),
    # ---- 个性化课程 ----
    ("PE101", "工程实践创新", 2, PERSONAL, "FA", []),
    ("PE201", "学科竞赛实训", 2, PERSONAL, "SP", []),
    ("PE301", "科研训练", 2, PERSONAL, "FA", []),
    ("PE401", "跨学科项目设计", 2, PERSONAL, "SP", []),
    ("PE501", "自主学习与终身学习", 2, PERSONAL, "FA", []),
    # ==== 扩充课程（提高课程密度，制造更多时间冲突，且全部不设先修）====
    # 这些课程一律作为「专业选修课」，学生可按兴趣自由组合，不构成毕业强制项。
    # ---- 专业选修课 · 数理与计算机 ----
    ("MATH301", "数值分析", 3, ELECTIVE, "FA", []),
    ("MATH302", "复变函数与积分变换", 3, ELECTIVE, "SP", []),
    ("PHY202", "近代物理导论", 3, ELECTIVE, "FA", []),
    ("CS106", "程序设计进阶", 4, ELECTIVE, "FA", []),
    ("CS213", "图论", 3, ELECTIVE, "SP", []),
    ("CS222", "数字逻辑", 4, ELECTIVE, "FA", []),
    ("CS303", "移动应用开发", 3, ELECTIVE, "FA", []),
    ("CS313", "软件测试", 3, ELECTIVE, "FA", []),
    ("CS322", "程序分析", 4, ELECTIVE, "SP", []),
    ("CS402", "工程实践(计算机)", 4, ELECTIVE, "FA", []),
    # ---- 专业选修课 · 电子信息 ----
    ("EE112", "电路实验", 1, ELECTIVE, "SP", []),
    ("EE203", "电子测量技术", 4, ELECTIVE, "FA", []),
    ("EE231", "单片机原理与应用", 4, ELECTIVE, "SP", []),
    ("EE303", "通信电子线路", 4, ELECTIVE, "FA", []),
    # ---- 专业选修课 · 计算机（方向课）----
    ("CS333", "深度学习", 3, ELECTIVE, "FA", []),
    ("CS343", "虚拟现实技术", 3, ELECTIVE, "FA", []),
    ("CS353", "物联网技术", 3, ELECTIVE, "FA", []),
    ("CS362", "网络攻防", 3, ELECTIVE, "SP", []),
    ("CS371", "软件架构", 4, ELECTIVE, "FA", []),
    ("CS372", "分布式系统", 4, ELECTIVE, "SP", []),
    ("CS381", "自然语言处理", 3, ELECTIVE, "FA", []),
    ("CS382", "强化学习", 3, ELECTIVE, "SP", []),
    # ---- 专业选修课 · 电子信息（方向课）----
    ("EE343", "天线与电波传播", 3, ELECTIVE, "FA", []),
    ("EE353", "语音信号处理", 3, ELECTIVE, "SP", []),
    ("EE363", "智能电网", 4, ELECTIVE, "FA", []),
    ("EE372", "控制系统仿真", 3, ELECTIVE, "SP", []),
    ("EE373", "智能机器人", 4, ELECTIVE, "SP", []),
    ("EE381", "传感器技术", 3, ELECTIVE, "FA", []),
    # ---- 交叉课程 ----
    ("IC303", "认知科学导论", 3, CROSS, "FA", []),
    ("IC313", "计算材料学", 3, CROSS, "SP", []),
    ("IC323", "数字人文", 3, CROSS, "SP", []),
    ("IC331", "医学影像分析", 4, CROSS, "FA", []),
    ("IC332", "生物医学工程导论", 3, CROSS, "SP", []),
    # ---- 通识课程 ----
    ("GE105", "形势与政策", 2, GENED, "FA", []),
    ("GE106", "社会主义发展史", 2, GENED, "SP", []),
    ("GE205", "大学英语听说", 2, GENED, "FA", []),
    ("GE206", "学术英语写作", 2, GENED, "SP", []),
    ("GE303", "应用文写作", 2, GENED, "FA", []),
    ("GE403", "创业管理", 2, GENED, "FA", []),
    ("GE404", "领导力与团队协作", 2, GENED, "SP", []),
    ("GE502", "音乐鉴赏", 1, GENED, "SP", []),
    ("GE503", "影视鉴赏", 1, GENED, "FA", []),
    ("GE604", "环境与可持续发展", 2, GENED, "FA", []),
    # ---- 个性化课程 ----
    ("PE102", "工程设计表达", 2, PERSONAL, "FA", []),
    ("PE202", "学科竞赛实训(进阶)", 2, PERSONAL, "SP", []),
    ("PE302", "科研论文写作", 2, PERSONAL, "FA", []),
    ("PE402", "跨学科创新实践", 2, PERSONAL, "SP", []),
    ("PE601", "大学学习科学", 1, PERSONAL, "FA", []),
]

_PREFIX_DEPT = {
    "MATH": "数学学院", "PHY": "物理学院", "CS": "计算机学院",
    "EE": "电子工程学院", "IC": "交叉学院", "GE": "通识教育中心",
    "PE": "创新创业学院",
}

# 学生：四季递进（大一…大四）+ 1 名 EE 学生；成绩单按年级递进
STUDENTS = [
    ("alice", "王小明", "CS", 1, set()),
    ("bob", "李思远", "CS", 2, {"MATH101", "MATH102", "CS101", "CS102",
                                "GE101", "GE102", "GE201", "GE202",
                                "GE203", "GE204"}),
    ("charlie", "张雅涵", "CS", 3, {"MATH101", "MATH102", "MATH201", "MATH202",
                                    "PHY101", "PHY102", "CS101", "CS102",
                                    "CS201", "CS202", "CS211", "GE101",
                                    "GE102", "GE103", "GE201", "GE202",
                                    "GE203", "GE204", "GE301", "GE302"}),
    ("dave", "陈浩然", "CS", 4, {
        # 主修 82（数理底座 26 + 计算机 56，含毕业设计）
        "MATH101", "MATH102", "MATH201", "MATH202", "PHY101", "PHY102",
        "PHY201", "CS101", "CS102", "CS104", "CS105", "CS201", "CS202",
        "CS211", "CS212", "CS301", "CS302", "CS311", "CS312", "CS321",
        "CS401",
        # 专业选修 12
        "CS331", "CS341", "CS351", "CS361",
        # 交叉 6
        "IC302", "IC312",
        # 通识 22
        "GE101", "GE102", "GE103", "GE104", "GE201", "GE202", "GE203",
        "GE204", "GE301", "GE302",
        # 个性化 8
        "PE101", "PE201", "PE301", "PE401"}),
    ("erin", "赵一鸣", "EE", 2, {"MATH101", "MATH102", "MATH201", "CS101",
                                 "GE101", "GE102", "GE201", "GE202",
                                 "GE203", "GE204"}),
]

GRADE_ZH = {1: "大一", 2: "大二", 3: "大三", 4: "大四"}
FIRST_SEMESTER = "2026FA"


def dept_of(code: str) -> str:
    letters = "".join(ch for ch in code if ch.isalpha())
    return _PREFIX_DEPT.get(letters, "通识教育中心")


def say(line: str = "") -> None:
    print(line)
    out_lines.append(line)


def tok(client: TestClient, username: str) -> str:
    password = {"registrar": "reg123", "admin": "admin123"}.get(
        username, username + "123")
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _pools():
    """按类别 + 代码前缀切出各专业培养方案所用的课程池。

    主修课程池只保留「核心骨架」课程（见 COURSES 中类别为 CORE 的原始课程）；
    为提高课程密度而追加的课程一律定为专业选修课，因此不再强制必修，
    学生可以按兴趣自由组合。
    """
    cs_core = [c[0] for c in COURSES
               if c[3] == CORE and (c[0].startswith("CS") or
                                    c[0].startswith("MATH") or c[0].startswith("PHY"))]
    ee_core = [c[0] for c in COURSES
               if c[3] == CORE and (c[0].startswith("EE") or
                                    c[0].startswith("MATH") or c[0].startswith("PHY"))]
    cs_el = [c[0] for c in COURSES if c[3] == ELECTIVE and
             c[0].startswith(("CS", "MATH", "PHY"))]
    ee_el = [c[0] for c in COURSES if c[3] == ELECTIVE and
             c[0].startswith(("EE", "MATH", "PHY"))]
    cross = [c[0] for c in COURSES if c[3] == CROSS]
    gened = [c[0] for c in COURSES if c[3] == GENED]
    personal = [c[0] for c in COURSES if c[3] == PERSONAL]
    return cs_core, ee_core, cs_el, ee_el, cross, gened, personal


#: 各类别学分下限。主修课程为「清单内全部必修」，其学分数由课程目录汇总得出，
#: 总学分下限 = 必修核心学分 + 四类选修下限之和（保证方案标注与实际一致）。
#: 下限刻意压低，让学生把精力放在自己选的方向上，而不是被学分总量逼着凑课。
ELECTIVE_MIN, CROSS_MIN, GENED_MIN, PERSONAL_MIN = 15, 9, 20, 6


def program_rules(core_pool, elective_pool, cross, gened, personal):
    """一份培养方案的五类规则：主修课程=必修全部；其余按类别修满学分下限。"""
    return [
        {"category": CORE, "rule_type": "all_of",
         "courses": sorted(core_pool), "min_credits": None, "min_courses": None},
        {"category": ELECTIVE, "rule_type": "min_credits",
         "courses": sorted(elective_pool), "min_credits": ELECTIVE_MIN, "min_courses": None},
        {"category": CROSS, "rule_type": "min_credits",
         "courses": sorted(cross), "min_credits": CROSS_MIN, "min_courses": None},
        {"category": GENED, "rule_type": "min_credits",
         "courses": sorted(gened), "min_credits": GENED_MIN, "min_courses": None},
        {"category": PERSONAL, "rule_type": "min_credits",
         "courses": sorted(personal), "min_credits": PERSONAL_MIN, "min_courses": None},
    ]


def main(db_path: Path | None = None, seed_only: bool = False) -> None:
    db_path = db_path or DB
    if db_path.exists():
        db_path.unlink()
    app = create_app(db_path)
    client = TestClient(app)

    say("# CourseFlow — 端到端执行轨迹"
        if not seed_only else "# CourseFlow — 数据初始化摘要")
    say()
    say("由 `demo/seed_demo.py` 针对 REST API 执行的脚本化演示。"
        if not seed_only else f"数据库 `{db_path.name}` 已初始化；学期处于 pre_enrollment 阶段。")
    say()

    def call(method, path, token=None, label=None, expect=200, **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = getattr(client, method)("/api" + path, headers=headers, **kw)
        tag = label or f"{method.upper()} {path}"
        icon = "OK" if r.status_code == expect else "!!"
        say(f"- `{icon} {tag}` → HTTP {r.status_code}"
            + ("" if r.status_code == expect else f"（预期 {expect}）"))
        if r.status_code != expect:
            say(f"    - body: {r.json()}")
        return r

    # ---------------------------------------------------------- setup ----
    say("## 0. 引导启动")
    reg = tok(client, "registrar")
    adm = tok(client, "admin")
    say("- 应用工厂创建默认账号；已签发 registrar 与 admin 会话")
    say()

    say("## 1. 数据初始化：学生（大一…大四）、课程目录（五类）、培养方案、教学班")
    for name, full, major, grade, _done in STUDENTS:
        call("post", "/users", adm,
             f"创建学生 {name}（{full} · {GRADE_ZH[grade]}{major}）",
             json={"username": name, "password": name + "123",
                   "full_name": full, "role": "student", "major": major,
                   "grade": grade})
    call("post", "/users", adm, "创建教师 carol（林晓梅）",
         json={"username": "carol", "password": "carol123",
               "full_name": "林晓梅", "role": "instructor"})

    conn = app.state.db_factory()
    try:
        for name, full, major, grade, done in STUDENTS:
            u = repo.get_user_by_username(conn, name)
            repo.set_completed_courses(conn, u["id"], done)
    finally:
        conn.close()
    say("- 成绩单已载入（SIS 导入，按年级递进）")
    say()

    for code, title, credits, category, term, prereqs in COURSES:
        call("post", "/courses", reg, f"课程 {code} {title}",
             json={"code": code, "title": title, "credits": credits,
                   "department": dept_of(code), "category": category,
                   "term": term, "prerequisites": list(prereqs)})
    say(f"- 课程目录共 {len(COURSES)} 门，分五类；每门标注常规开课学期（FA/SP）")
    say()

    cs_core, ee_core, cs_el, ee_el, cross, gened, personal = _pools()
    credits_of = {c[0]: c[2] for c in COURSES}
    elective_floor = ELECTIVE_MIN + CROSS_MIN + GENED_MIN + PERSONAL_MIN
    cs_total = sum(credits_of[c] for c in cs_core) + elective_floor
    ee_total = sum(credits_of[c] for c in ee_core) + elective_floor
    seats = f"主修 {cs_total - elective_floor:g} 全部必修 + 选修 {ELECTIVE_MIN} + " \
            f"交叉 {CROSS_MIN} + 通识 {GENED_MIN} + 个性化 {PERSONAL_MIN}"
    call("post", "/degree/requirements", reg,
         f"培养方案：计算机科学与技术（{seats}，合计 {cs_total:g}）",
         json={"major": "CS", "total_credits_min": cs_total,
               "label": f"计算机科学与技术培养方案 2026（必修核心 "
                        f"{cs_total - elective_floor:g} 学分）",
               "rules": program_rules(cs_core, cs_el, cross, gened, personal)})
    call("post", "/degree/requirements", reg,
         f"培养方案：电子信息工程（{seats}，合计 {ee_total:g}）",
         json={"major": "EE", "total_credits_min": ee_total,
               "label": f"电子信息工程培养方案 2026（必修核心 "
                        f"{ee_total - elective_floor:g} 学分）",
               "rules": program_rules(ee_core, ee_el, cross, gened, personal)})
    say()

    # 自动开课：按课程常规开课学期投放教学班（时间与标准节次对齐）
    courses = client.get("/api/courses", headers={"Authorization": f"Bearer {reg}"}).json()
    offering = build_offering(courses, FIRST_SEMESTER,
                              capacity=60,
                              capacity_by_code={"CS201": 1, "CS104": 1})
    S_CS201 = S_CS104 = None
    for sec in offering:
        r = call("post", "/sections", reg,
                 f"教学班 {sec['course_code']}.01（{sec['instructor_name']} · "
                 f"{sec['venue']} · 容量 {sec['capacity']}）",
                 json=sec)
        if sec["course_code"] == "CS201":
            S_CS201 = r.json()["id"]
        if sec["course_code"] == "CS104":
            S_CS104 = r.json()["id"]
    sample = offering[0]
    say(f"- 共投放 {len(offering)} 个教学班；时间取自标准作息"
        f"（如第 1-2 节 {sample['slots'][0]['start_min']}–"
        f"{sample['slots'][0]['end_min']} 分钟）")
    say()
    if seed_only:
        return

    # ------------------------------------------------ semantic guards ----
    say("## 2. 语义约束校验（phase = pre_enrollment）")
    call("post", "/enrollments", tok(client, "alice"),
         "alice（大一，无先修）申请 CS201.01", expect=400, json={"section_id": S_CS201})
    say("    - REJECTED: PREREQ_MISSING —— 先修 DAG 策略（缺 CS101）")
    call("post", "/enrollments", tok(client, "alice"),
         "alice 填报志愿 MATH101.01", json={"section_id": _seg(client, reg, "MATH101")})
    say("    - 志愿以 VALIDATED 存储（本阶段不计容量）")
    call("post", "/courses", reg, "添加环边 CS101 -> CS201", expect=400,
         json={"code": "CS101", "title": "非法版本", "credits": 4,
               "department": "计算机学院", "category": CORE, "term": "FA",
               "prerequisites": ["CS201"]})
    say("    - REJECTED: PREREQ_CYCLE —— 目录先修 DAG 完整性得以保持")
    r = call("post", "/courses", reg, "修改 CS101 -> 生成不可变版本 2",
             json={"code": "CS101", "title": "程序设计基础(Python)", "credits": 4,
                   "department": "计算机学院", "category": CORE, "term": "FA",
                   "prerequisites": []})
    say(f"    - CS101 现处于 catalog_version {r.json()['catalog_version']}")
    say()

    # ------------------------------------------------- stage handover ----
    say("## 3. 多阶段编排：pre_enrollment → allocation（带种子抽签）")
    for who in ["bob", "erin"]:
        call("post", "/enrollments", tok(client, who),
             f"{who} 填报志愿 CS201.01（容量 1）", json={"section_id": S_CS201})
    call("post", "/enrollments", tok(client, "charlie"),
         "charlie 填报志愿 CS201.01", expect=400, json={"section_id": S_CS201})
    say("    - REJECTED: ALREADY_PASSED —— 已通过课程不允许重修选课")
    r = call("post", "/admin/phase/advance", reg, "推进阶段")
    snap = r.json()["handover_context"]["sections"]["CS201.01"]
    say(f"    - phase → {r.json()['phase']}；交接快照：{snap}")
    r = call("post", "/admin/allocation/run?seed=42", reg,
             "教务运行带种子的批量分配")
    say(f"    - 分配结果：{r.json()['sections']['CS201.01']}")
    say()

    # ------------------------------------------------------ add/drop ----
    say("## 4. add_drop：先到先得 + 事件驱动的候补晋升")
    call("post", "/admin/phase/advance", reg, "推进阶段 → add_drop")
    say("- 清空上一阶段的占位，观察纯净的先到先得/候补行为")
    for who, path in [("bob", "/enrollments/me"), ("charlie", "/enrollments/me"),
                      ("erin", "/enrollments/me"), ("alice", "/enrollments/me")]:
        h = {"Authorization": f"Bearer {tok(client, who)}"}
        for e in client.get("/api" + path, headers=h).json():
            if e["state"] in ("ENROLLED", "WAITLISTED"):
                client.post(f"/api/enrollments/{e['id']}/drop", headers=h)
    r = call("post", "/enrollments", tok(client, "bob"),
             "bob 申请 CS104.01（容量 1）", json={"section_id": S_CS104})
    say(f"    - state: {r.json()['state']}")
    r = call("post", "/enrollments", tok(client, "alice"),
             "alice 申请 CS104.01 —— 已满", json={"section_id": S_CS104})
    say(f"    - state: {r.json()['state']} (waitlist_seq={r.json()['waitlist_seq']})")
    r = call("post", "/enrollments", tok(client, "charlie"),
             "charlie 申请 CS104.01 —— 已满", json={"section_id": S_CS104})
    say(f"    - state: {r.json()['state']} (waitlist_seq={r.json()['waitlist_seq']})")
    bob_enr = _find(client, tok(client, "bob"), S_CS104)
    call("post", f"/enrollments/{bob_enr}/drop", tok(client, "bob"),
         "bob 退课 CS104.01")
    say("    - 事件 `section.seat_released` → WaitlistEngine 晋升候补队首 alice")
    alice_now = client.get("/api/enrollments/me",
                           headers={"Authorization": f"Bearer {tok(client, 'alice')}"}
                           ).json()
    a_row = next(e for e in alice_now if e["section_id"] == S_CS104)
    say(f"    - alice：WAITLISTED → {a_row['state']}（零轮询，事件驱动）")
    call("post", "/admin/phase/advance", reg, "推进阶段 → closed")
    say("    - 学期 CLOSED：选课冻结；审计轨迹不可变")
    say()

    say("## 5. 可观测性：审计链与事件日志")
    audit = client.get(f"/api/admin/audit?enrollment_id={a_row['id']}",
                       headers={"Authorization": f"Bearer {reg}"}).json()
    say(f"- 选课 #{a_row['id']}（alice → CS104.01）生命周期：")
    for a in audit:
        say(f"    - {a['from_state'] or '∅'} → **{a['to_state']}** by {a['actor']}"
            + (f" ({a['reason']})" if a["reason"] else ""))
    events = client.get("/api/admin/events",
                        headers={"Authorization": f"Bearer {reg}"}).json()
    say(f"- 事件日志：共记录 {len(events)} 条事件，类型："
        f"{sorted({e['type'] for e in events})}")
    say()

    # ------------------------------------------------- degree audit ----
    say("## 6. 培养方案审核与毕业规划（按课程类别学分）")
    for who, zh in [("dave", "大四"), ("charlie", "大三"), ("alice", "大一")]:
        body = client.get("/api/degree/me",
                          headers={"Authorization": f"Bearer {tok(client, who)}"}
                          ).json()
        cats = "；".join(
            (f"{r['category']} {r['met_count']}/{len(r['courses'])} 门"
             if r["rule_type"] == "all_of"
             else f"{r['category']} {r['credits_earned']:g}/{r['min_credits']:g}")
            for r in body["rules"])
        say(f"- {who}（{zh}）：状态 **{body['status']}**，"
            f"总学分 {body['total_credits_earned']:g}/{body['total_credits_required']:g}；{cats}")
    dplan = client.get("/api/degree/me/plan",
                       headers={"Authorization": f"Bearer {tok(client, 'dave')}"}
                       ).json()
    say(f"- dave 毕业路线图：feasible={dplan['feasible']}，"
        f"待修 {len(dplan['missing_courses'])} 门，跨越 {len(dplan['semesters'])} 个学期")
    say()

    # ------------------------------------------------- semester advance ----
    say("## 7. 推进学期：自动开课 + 跨学年自动升年级")
    for _ in range(2):
        r = call("post", "/admin/semester/advance", reg, "推进到下一学期")
        b = r.json()
        say(f"    - {b['previous']} → **{b['semester']}**；"
            f"自动开课 {b['sections_created']} 个，升年级 {b['students_promoted']} 人")
    me = client.get("/api/auth/me",
                    headers={"Authorization": f"Bearer {tok(client, 'alice')}"}).json()
    say(f"    - alice 当前年级：{GRADE_ZH.get(me['grade'], me['grade'])}"
        f"（grade={me['grade']}）；当前学期 {me['semester']}，阶段 {me['phase']}")


def _seg(client, reg, course_code):
    """取某课程在某学期的教学班 id。"""
    secs = client.get("/api/sections?course_code=" + course_code,
                      headers={"Authorization": f"Bearer {reg}"}).json()
    return secs[0]["id"]


def _find(client, token, section_id):
    rows = client.get("/api/enrollments/me",
                      headers={"Authorization": f"Bearer {token}"}).json()
    return next(e["id"] for e in rows if e["section_id"] == section_id
                and e["state"] in ("ENROLLED", "WAITLISTED", "VALIDATED"))


if __name__ == "__main__":
    cli_paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    cli_seed_only = "--seed-only" in sys.argv
    cli_db = Path(cli_paths[0]).resolve() if cli_paths else None
    main(cli_db, cli_seed_only)
    if not cli_seed_only:
        TRACE.parent.mkdir(exist_ok=True)
        TRACE.write_text("\n".join(out_lines), encoding="utf-8")
        print(f"\ntrace written to {TRACE}")
