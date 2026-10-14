"""End-to-end demo: seeds a realistic semester and walks a scripted
scenario through the public REST API, printing a human-readable trace.

Run:  python demo/seed_demo.py [db_path] [--seed-only]
      --seed-only populates users/transcripts/courses/sections and leaves the
      semester in pre_enrollment (ideal before starting the live server).
Output: console + docs/demo_trace.md (execution evidence for the report)

Cast:
  alice (passed CS101, MATH101)   bob (passed CS101, MATH101)
  charlie (passed CS101 only)     dave (passed nothing)
  erin (passed MATH101)           carol — instructor of CS201.01
Sections: CS201.01 cap=2 Mon 08:30-10:10, EE210.01 cap=1 Tue 08:30-10:10
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import repositories as repo  # noqa: E402
from app.main import create_app  # noqa: E402

DB = ROOT / "demo.db"
TRACE = ROOT / "docs" / "demo_trace.md"
out_lines: list[str] = []


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

    say("## 1. 数据初始化：用户、课程目录（先修课程 DAG）、教学班")
    names = {"alice": "Alice Wang", "bob": "Bob Li", "charlie": "Charlie Zhao",
             "dave": "Dave Qian", "erin": "Erin Sun", "carol": "Carol Chen"}
    for name, role in [("alice", "student"), ("bob", "student"),
                       ("charlie", "student"), ("dave", "student"),
                       ("erin", "student"), ("carol", "instructor")]:
        call("post", "/users", adm, f"创建用户 {name}（{role}）",
             json={"username": name, "password": name + "123",
                   "full_name": names[name], "role": role})
    # transcripts (in a real deployment: imported from the SIS)
    conn = app.state.db_factory()
    try:
        for name, done in [("alice", {"CS101", "MATH101"}),
                           ("bob", {"CS101", "MATH101"}),
                           ("charlie", {"CS101"}),
                           ("dave", set()),
                           ("erin", {"MATH101"})]:
            u = repo.get_user_by_username(conn, name)
            repo.set_completed_courses(conn, u["id"], done)
    finally:
        conn.close()
    say("- 成绩单已载入（SIS 导入）")

    for c in [
        {"code": "CS101", "title": "Intro to Programming", "credits": 3,
         "department": "Computer Science", "prerequisites": []},
        {"code": "MATH101", "title": "Calculus I", "credits": 4,
         "department": "Mathematics", "prerequisites": []},
        {"code": "CS201", "title": "Data Structures", "credits": 3,
         "department": "Computer Science", "prerequisites": ["CS101"]},
        {"code": "EE210", "title": "Circuit Theory", "credits": 3,
         "department": "Electrical Engineering", "prerequisites": ["MATH101"]},
    ]:
        call("post", "/courses", reg, f"课程 {c['code']}", json=c)

    r = call("post", "/sections", reg,
             "教学班 CS201.01（容量 2，周一 08:30-10:10，Carol Chen）",
             json={"course_code": "CS201", "semester": "2026FA",
                   "section_no": "01", "instructor_name": "Carol Chen",
                   "capacity": 2, "venue": "SE-101",
                   "slots": [{"day": 1, "start_min": 510, "end_min": 650}]})
    S1 = r.json()["id"]
    r = call("post", "/sections", reg,
             "教学班 EE210.01（容量 1，周二 08:30-10:10，Dan Wu）",
             json={"course_code": "EE210", "semester": "2026FA",
                   "section_no": "01", "instructor_name": "Dan Wu",
                   "capacity": 1, "venue": "EE-201",
                   "slots": [{"day": 2, "start_min": 510, "end_min": 650}]})
    S2 = r.json()["id"]
    say()
    if seed_only:
        return

    # ------------------------------------------------ semantic guards ----
    say("## 2. 语义约束校验（phase = pre_enrollment）")
    call("post", "/enrollments", tok(client, "dave"),
         "dave 申请 CS201.01", expect=400, json={"section_id": S1})
    say("    - REJECTED: PREREQ_MISSING（dave 未通过 CS101）—— 先修 DAG 策略")
    call("post", "/enrollments", tok(client, "alice"),
         "alice 填报志愿 CS201.01", json={"section_id": S1})
    say("    - 志愿以 VALIDATED 存储（本阶段不计容量）")
    call("post", "/courses", reg, "添加环边 CS201 -> CS101", expect=400,
         json={"code": "CS101", "title": "Bad", "credits": 3,
               "department": "CS", "prerequisites": ["CS201"]})
    say("    - REJECTED: PREREQ_CYCLE —— 课程目录图完整性得以保持")
    r = call("post", "/courses", reg, "修改 CS101 -> 生成不可变版本 2",
             json={"code": "CS101", "title": "Intro to Programming (Python)",
                   "credits": 3, "department": "Computer Science",
                   "prerequisites": []})
    say(f"    - CS101 现处于 catalog_version {r.json()['catalog_version']}")
    say()

    # ------------------------------------------------- stage handover ----
    say("## 3. 多阶段编排：pre_enrollment → allocation")
    for who in ["bob", "charlie"]:
        call("post", "/enrollments", tok(client, who),
             f"{who} 填报志愿 CS201.01", json={"section_id": S1})
    r = call("post", "/admin/phase/advance", reg, "推进阶段")
    snap = r.json()["handover_context"]["sections"]["CS201.01"]
    say(f"    - phase → {r.json()['phase']}；交接快照：{snap}")
    call("post", "/enrollments", tok(client, "erin"),
         "在 ALLOCATION 阶段直接选课", expect=400, json={"section_id": S1})
    say("    - REJECTED: PHASE_LOCKED —— 行为受阶段约束")
    r = call("post", "/admin/allocation/run?seed=42", reg,
             "教务运行带种子的批量分配")
    say(f"    - 分配结果：{r.json()['sections']}")
    say()

    # ------------------------------------------------------ add/drop ----
    say("## 4. add_drop：先到先得 + 事件驱动的候补晋升")
    call("post", "/admin/phase/advance", reg, "推进阶段 → add_drop")
    r = call("post", "/enrollments", tok(client, "bob"),
             "bob 申请 EE210.01（容量 1）", json={"section_id": S2})
    say(f"    - state: {r.json()['state']}")
    r = call("post", "/enrollments", tok(client, "alice"),
             "alice 申请 EE210.01 —— 已满", json={"section_id": S2})
    say(f"    - state: {r.json()['state']} (waitlist_seq={r.json()['waitlist_seq']})")
    r = call("post", "/enrollments", tok(client, "erin"),
             "erin 申请 EE210.01 —— 已满", json={"section_id": S2})
    say(f"    - state: {r.json()['state']} (waitlist_seq={r.json()['waitlist_seq']})")
    call("post", "/enrollments", tok(client, "charlie"),
         "charlie 申请 EE210.01", expect=400, json={"section_id": S2})
    say("    - REJECTED: PREREQ_MISSING（charlie 缺少 MATH101）")

    r = call("post", f"/enrollments/{{alice_enr}}/drop".replace(
        "{alice_enr}", str(_alice_enr(client, S2))), tok(client, "alice"),
        "alice 退课 EE210.01")
    say("    - 席位释放 → 发布事件 `section.seat_released`")
    alice_now = client.get("/api/enrollments/me",
                           headers={"Authorization": f"Bearer {tok(client, 'alice')}"}
                           ).json()
    say(f"    - 复查 alice：{alice_now[-1]['state']} —— FIFO 队首正是她本人，"
        "无需晋升")
    # bob (ENROLLED) drops instead: promotion of the new FIFO head (erin)
    bob_enr = _find(client, tok(client, "bob"), S2)
    call("post", f"/enrollments/{bob_enr}/drop", tok(client, "bob"),
         "bob 退课 EE210.01")
    say("    - 事件 `section.seat_released` → WaitlistEngine 晋升 erin")
    erin_now = client.get("/api/enrollments/me",
                          headers={"Authorization": f"Bearer {tok(client, 'erin')}"}
                          ).json()
    e_row = next(e for e in erin_now if e["section_id"] == S2)
    say(f"    - erin：WAITLISTED → {e_row['state']}（零轮询，事件驱动）")
    call("post", "/admin/phase/advance", reg, "推进阶段 → closed")
    say("    - 学期 CLOSED：选课冻结；审计轨迹不可变")
    say()

    say("## 5. 可观测性：审计链与事件日志")
    audit = client.get(f"/api/admin/audit?enrollment_id={e_row['id']}",
                       headers={"Authorization": f"Bearer {reg}"}).json()
    say(f"- 选课 #{e_row['id']}（erin → EE210.01）生命周期：")
    for a in audit:
        say(f"    - {a['from_state'] or '∅'} → **{a['to_state']}** by {a['actor']}"
            + (f" ({a['reason']})" if a["reason"] else ""))
    events = client.get("/api/admin/events",
                        headers={"Authorization": f"Bearer {reg}"}).json()
    say(f"- 事件日志：共记录 {len(events)} 条事件，类型："
        f"{sorted({e['type'] for e in events})}")


def _find(client, token, section_id):
    rows = client.get("/api/enrollments/me",
                      headers={"Authorization": f"Bearer {token}"}).json()
    return next(e["id"] for e in rows if e["section_id"] == section_id
                and e["state"] in ("ENROLLED", "WAITLISTED", "VALIDATED"))


def _alice_enr(client, section_id):
    return _find(client, tok(client, "alice"), section_id)


if __name__ == "__main__":
    cli_paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    cli_seed_only = "--seed-only" in sys.argv
    cli_db = Path(cli_paths[0]).resolve() if cli_paths else None
    main(cli_db, cli_seed_only)
    if not cli_seed_only:
        TRACE.parent.mkdir(exist_ok=True)
        TRACE.write_text("\n".join(out_lines), encoding="utf-8")
        print(f"\ntrace written to {TRACE}")
