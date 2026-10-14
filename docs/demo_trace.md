# CourseFlow — 端到端执行轨迹

由 `demo/seed_demo.py` 针对 REST API 执行的脚本化演示。

## 0. 引导启动
- 应用工厂创建默认账号；已签发 registrar 与 admin 会话

## 1. 数据初始化：用户、课程目录（先修课程 DAG）、教学班
- `OK 创建用户 alice（student）` → HTTP 200
- `OK 创建用户 bob（student）` → HTTP 200
- `OK 创建用户 charlie（student）` → HTTP 200
- `OK 创建用户 dave（student）` → HTTP 200
- `OK 创建用户 erin（student）` → HTTP 200
- `OK 创建用户 carol（instructor）` → HTTP 200
- 成绩单已载入（SIS 导入）
- `OK 课程 CS101` → HTTP 200
- `OK 课程 MATH101` → HTTP 200
- `OK 课程 CS201` → HTTP 200
- `OK 课程 EE210` → HTTP 200
- `OK 教学班 CS201.01（容量 2，周一 08:30-10:10，Carol Chen）` → HTTP 200
- `OK 教学班 EE210.01（容量 1，周二 08:30-10:10，Dan Wu）` → HTTP 200

## 2. 语义约束校验（phase = pre_enrollment）
- `OK dave 申请 CS201.01` → HTTP 400
    - REJECTED: PREREQ_MISSING（dave 未通过 CS101）—— 先修 DAG 策略
- `OK alice 填报志愿 CS201.01` → HTTP 200
    - 志愿以 VALIDATED 存储（本阶段不计容量）
- `OK 添加环边 CS201 -> CS101` → HTTP 400
    - REJECTED: PREREQ_CYCLE —— 课程目录图完整性得以保持
- `OK 修改 CS101 -> 生成不可变版本 2` → HTTP 200
    - CS101 现处于 catalog_version 2

## 3. 多阶段编排：pre_enrollment → allocation
- `OK bob 填报志愿 CS201.01` → HTTP 200
- `OK charlie 填报志愿 CS201.01` → HTTP 200
- `OK 推进阶段` → HTTP 200
    - phase → allocation；交接快照：{'capacity': 2, 'enrolled': 0, 'validated_pending': 3, 'waitlisted': 0}
- `OK 在 ALLOCATION 阶段直接选课` → HTTP 400
    - REJECTED: PHASE_LOCKED —— 行为受阶段约束
- `OK 教务运行带种子的批量分配` → HTTP 200
    - 分配结果：{'CS201.01': {'capacity': 2, 'requests': 3, 'enrolled': 2, 'waitlisted': 1}, 'EE210.01': {'capacity': 1, 'requests': 0, 'enrolled': 0, 'waitlisted': 0}}

## 4. add_drop：先到先得 + 事件驱动的候补晋升
- `OK 推进阶段 → add_drop` → HTTP 200
- `OK bob 申请 EE210.01（容量 1）` → HTTP 200
    - state: ENROLLED
- `OK alice 申请 EE210.01 —— 已满` → HTTP 200
    - state: WAITLISTED (waitlist_seq=1)
- `OK erin 申请 EE210.01 —— 已满` → HTTP 200
    - state: WAITLISTED (waitlist_seq=2)
- `OK charlie 申请 EE210.01` → HTTP 400
    - REJECTED: PREREQ_MISSING（charlie 缺少 MATH101）
- `OK alice 退课 EE210.01` → HTTP 200
    - 席位释放 → 发布事件 `section.seat_released`
    - 复查 alice：CANCELLED —— FIFO 队首正是她本人，无需晋升
- `OK bob 退课 EE210.01` → HTTP 200
    - 事件 `section.seat_released` → WaitlistEngine 晋升 erin
    - erin：WAITLISTED → ENROLLED（零轮询，事件驱动）
- `OK 推进阶段 → closed` → HTTP 200
    - 学期 CLOSED：选课冻结；审计轨迹不可变

## 5. 可观测性：审计链与事件日志
- 选课 #7（erin → EE210.01）生命周期：
    - SUBMITTED → **VALIDATED** by erin (策略校验全部通过)
    - VALIDATED → **WAITLISTED** by erin (名额已满，进入候补队列)
    - WAITLISTED → **ENROLLED** by system:waitlist (从候补队列晋升)
- 事件日志：共记录 19 条事件，类型：['enrollment.state_changed', 'phase.changed', 'section.seat_released', 'waitlist.promoted']