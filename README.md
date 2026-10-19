# CourseFlow — 选课与排课编排系统

MAiNS（网络与系统中的模型与算法）课程项目，类型 4 —— 自定义项目。CourseFlow 管理选课日历各阶段中选课申请的全生命周期，具备显式的选课有限状态机、带交接快照的分阶段学期流水线、事件驱动的候补晋升、基于能力矩阵的 RBAC，以及受先修课程 DAG 完整性校验保护的版本化课程目录。在此之上，系统还提供**培养方案审核与毕业路线图**、**无冲突课表规划**，以及一个可离线运行的**智能体层**。

- 完整架构说明：[`docs/report.md`](docs/report.md)
- 项目提案（提交指导教师）：[`docs/proposal.md`](docs/proposal.md)
- 脚本化执行轨迹：[`docs/demo_trace.md`](docs/demo_trace.md)
- 正式数据 Schema：`docs/schemas/*.schema.json`

## 快速开始

```bash
pip install -r requirements.txt

python run.py                  # 在 http://127.0.0.1:8000 提供 API 与演示界面
python demo/seed_demo.py       # 脚本化端到端场景（打印执行轨迹）
python -m pytest tests -q      # 103 个自动化测试（含智能体层）
```

在浏览器中打开 http://127.0.0.1:8000 并登录：

| 账号 | 密码 | 角色 |
|---|---|---|
| registrar | reg123 | registrar（阶段推进、分配、课程目录） |
| admin | admin123 | admin（用户、审计） |
| alice / bob / charlie / dave / erin | `<name>123` | 学生 |

推荐演示流程：以 `registrar` 身份登录 → 写入课程目录（或先运行 `python demo/seed_demo.py` 得到一个已初始化的学期）→ 以学生身份登录并提交选课请求 → 推进阶段 → 运行批量分配 → 学生退掉一个教学班并观察候补队列自动晋升 → 查看审计轨迹与事件日志。学生另有**我的课表**（周课表网格）、**培养计划**（培养方案审核 + 毕业路线图）与**智能选课助手**（见下文「智能体层」）三个标签页；教务可推进学期，系统按课程常规开课学期自动投放教学班。

## 智能体层（可选）

两个可组合的组件位于服务层*之上*，与浏览器界面并列——它们复用既有服务，而非重新实现：

- **方案 A —— `PlannerAgent`（`app/agent/planner.py`）**：把系统的*单次请求校验*扩展为*多课程组合搜索*。它在先修课程 DAG、课表重叠、学分上限以及重复/重修等约束下枚举可行的课程集合，对其进行确定性排序，并用与策略链相同的机器可读原因码解释每一项选择（以及每一项排除）。只读、可复现、具备完整单元测试。
- **方案 B —— 编排（`app/agent/tools.py`、`llm.py`、`orchestrator.py`）**：把既有用例暴露为 14 个供 LLM 调用的工具（函数调用），使系统可以用自然语言驱动；其中 `audit_degree`（毕业审核）、`plan_graduation`（毕业路线图）、`plan_timetable`（无冲突课表规划）是三个只读规划工具，另有两个写工具只产出待确认提案。LLM 只负责决定*调用哪些*工具；所有规划都来自方案 A 或上述纯领域模块，所有校验都来自既有的策略链。

LLM 通道与系统默认配置相互独立：**除非配置了 API 密钥，方案 B 运行在确定性的离线规则模式下**，因此系统在完全无网络时也能工作。

```bash
# 可选：启用 LLM 编排通道（任意 OpenAI 兼容网关）
export AGENT_LLM_BASE_URL=https://api.deepseek.com/v1   # 也可用 DashScope / 智谱 / 本地 Ollama
export AGENT_LLM_API_KEY=sk-...                         # 不要提交到 git（见 .gitignore）
export AGENT_LLM_MODEL=deepseek-chat
```

也可以不动环境变量，直接把上面几行（或仅一行裸密钥）写进项目根目录的
`.env.local`：`run.py` 启动时会自动载入，已存在的环境变量优先。该文件已在
`.gitignore` 中，不会进入版本库。

LLM 路径已针对在线的 DeepSeek 端点做过端到端验证（意图解析、工具调用、写入提议、确认并执行）。系统提示词携带两条约束，这两点真实模型往往做不对：必须*真正调用*写入工具，而不是仅在文字里描述一个提议（只描述提议不会产生确认按钮）；以及当用户明确说「帮我选 X」时，应直接给出提议，而不是先反问是否获得许可。

无论运行在哪种模式下，以下两条不变式都成立：

1. **写操作从不由智能体直接执行。** `submit_enrollment` / `drop_enrollment` 只产生一个*待确认提议*；必须由用户确认，确认后的动作再经由常规 REST 端点执行——因此约束策略链与 RBAC 始终生效。
2. **智能体始终以登录用户的身份行动。** 每次工具调用都在调用者的身份下运行，因此行级权限成立（学生智能体无法看到其他学生的数据），且每个回合都会以 `agent.turn` 追加到审计 `event_log`。

## 架构一览

```text
消费者（并列）：浏览器界面  ·  智能体层（规划器 + LLM 编排器）
                   │            │ 仅调用工具，绝不直接写 SQL
                   ▼            ▼
REST 接口层（薄层：REST 映射 + RBAC 守卫，32 个端点）
        │
控制平面：CatalogService · EnrollmentService · AllocationService · PhaseService
          · SemesterService · DegreeService · TimetableService
        │                              │ 发布事件
纯领域层：state_machine（状态机）· policies（先修 DAG / 课表 / 学分 / 重复）· graph
          · degree（培养方案审核 + 毕业规划）· timetable（无冲突课表规划）· curriculum（开课编排）
        │                              ▼
数据平面：EventBus ──► WaitlistEngine（自动晋升）
        │
仓储层（唯一 SQL 模块）──► SQLite（含只追加的审计与事件日志）
```

## 覆盖的复杂度维度

| 维度 | 体现位置 |
|---|---|
| 结构 | 四层分离；单一 SQL 接缝；纯领域模块（策略与三个规划器全部无 I/O）；事件总线；智能体层作为同一服务 API 的第二个消费者 |
| 行为 | 选课有限状态机 + 审计；带交接快照的四阶段学期状态机；FCFS/抽签/候补各阶段行为；学期推进与按开课学期自动投放教学班；基于能力矩阵的 RBAC；审计日志中的智能体回合；提议→确认→执行的写入协议 |
| 语义 | 版本化不可变课程目录；先修课程 DAG（环检测 + 拓扑序）；**第二份版本化注册表**（培养方案：必修全部 / N 选一 / 分类学分下限）；培养方案三态审核与毕业路线图（拓扑排序 + 学期学分装箱）；无冲突课表规划（有界回溯 CSP）；正式的 JSON Schema；带原因码的策略链；满足约束的组合搜索（PlannerAgent） |
