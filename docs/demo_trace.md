# CourseFlow — End-to-End Execution Trace

Scripted demo executed against the REST API by `demo/seed_demo.py`.

## 0. Bootstrap
- app factory created default accounts; registrar & admin sessions issued

## 1. Seed: users, catalog (prerequisite DAG), sections
- `OK create user alice (student)` → HTTP 200
- `OK create user bob (student)` → HTTP 200
- `OK create user charlie (student)` → HTTP 200
- `OK create user dave (student)` → HTTP 200
- `OK create user erin (student)` → HTTP 200
- `OK create user carol (instructor)` → HTTP 200
- transcripts loaded (SIS import)
- `OK course CS101` → HTTP 200
- `OK course MATH101` → HTTP 200
- `OK course CS201` → HTTP 200
- `OK course EE210` → HTTP 200
- `OK section CS201.01 (cap 2, Mon 08:30-10:10, Carol Chen)` → HTTP 200
- `OK section EE210.01 (cap 1, Tue 08:30-10:10, Dan Wu)` → HTTP 200

## 2. Semantic constraint enforcement (phase = pre_enrollment)
- `OK dave requests CS201.01` → HTTP 400
    - REJECTED: PREREQ_MISSING (dave has no CS101) — DAG policy
- `OK alice wishes CS201.01` → HTTP 200
    - wish stored as VALIDATED (capacity-blind in this stage)
- `OK add cycle edge CS201 -> CS101` → HTTP 400
    - REJECTED: PREREQ_CYCLE — catalog graph integrity preserved
- `OK edit CS101 -> immutable version 2` → HTTP 200
    - CS101 now at catalog_version 2

## 3. Multi-stage orchestration: pre_enrollment → allocation
- `OK bob wishes CS201.01` → HTTP 200
- `OK charlie wishes CS201.01` → HTTP 200
- `OK advance phase` → HTTP 200
    - phase → allocation; handover snapshot: {'capacity': 2, 'enrolled': 0, 'validated_pending': 3, 'waitlisted': 0}
- `OK enroll while in ALLOCATION` → HTTP 400
    - REJECTED: PHASE_LOCKED — behavior is stage-scoped
- `OK registrar runs seeded batch allocation` → HTTP 200
    - allocation result: {'CS201.01': {'capacity': 2, 'requests': 3, 'enrolled': 2, 'waitlisted': 1}, 'EE210.01': {'capacity': 1, 'requests': 0, 'enrolled': 0, 'waitlisted': 0}}

## 4. add_drop: FCFS + event-driven waitlist promotion
- `OK advance phase → add_drop` → HTTP 200
- `OK bob requests EE210.01 (cap 1)` → HTTP 200
    - state: ENROLLED
- `OK alice requests EE210.01 — full` → HTTP 200
    - state: WAITLISTED (waitlist_seq=1)
- `OK erin requests EE210.01 — full` → HTTP 200
    - state: WAITLISTED (waitlist_seq=2)
- `OK charlie requests EE210.01` → HTTP 400
    - REJECTED: PREREQ_MISSING (charlie lacks MATH101)
- `OK alice drops EE210.01` → HTTP 200
    - seat released → event `section.seat_released` published
    - alice re-checked: CANCELLED — FIFO head was herself, no promotion needed
- `OK bob drops EE210.01` → HTTP 200
    - event `section.seat_released` → WaitlistEngine promotes erin
    - erin: WAITLISTED → ENROLLED (zero polling, event-driven)
- `OK advance phase → closed` → HTTP 200
    - semester CLOSED: enrollment frozen; audit trail immutable

## 5. Observability: audit chain & event log
- enrollment #7 (erin → EE210.01) lifecycle:
    - SUBMITTED → **VALIDATED** by erin (policies passed)
    - VALIDATED → **WAITLISTED** by erin (section full, waitlisted)
    - WAITLISTED → **ENROLLED** by system:waitlist (promoted from waitlist)
- event log: 19 events recorded, types: ['enrollment.state_changed', 'phase.changed', 'section.seat_released', 'waitlist.promoted']