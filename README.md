# CourseFlow — Course Registration & Timetable Orchestration System

MAiNS (Models and Algorithms in Networks and Systems) course project,
Track Option 4 (self-defined). CourseFlow manages the full lifecycle of
course enrollment requests across a semester's three registration stages,
with an explicit enrollment FSM, a staged semester pipeline with handover
snapshots, event-driven waitlist promotion, capability-based RBAC, and a
versioned course catalog protected by prerequisite-DAG integrity checks.

- Full architecture write-up: [`docs/report.md`](docs/report.md)
- Instructor proposal: [`docs/proposal.md`](docs/proposal.md)
- Scripted execution trace: [`docs/demo_trace.md`](docs/demo_trace.md)
- Formal data schemas: `docs/schemas/*.schema.json`

## Quick start

```bash
pip install -r requirements.txt

python run.py                  # API + demo UI at http://127.0.0.1:8000
python demo/seed_demo.py       # scripted end-to-end scenario (prints trace)
python -m pytest tests -q      # 37 automated tests
```

Open http://127.0.0.1:8000 in a browser and sign in:

| account | password | role |
|---|---|---|
| registrar | reg123 | registrar (phases, allocation, catalog) |
| admin | admin123 | admin (users, audit) |
| alice / bob / charlie / dave / erin | `<name>123` | students |

Demo flow to showcase: sign in as `registrar` → seed the catalog (or run
`python demo/seed_demo.py` first for a pre-populated semester) → sign in
as students and submit requests → advance phases → run the batch
allocation → drop a section as a student and watch the waitlist
auto-promote → inspect the audit trail and event log.

## Architecture at a glance

```text
API layer (thin REST + RBAC guards)
        │
Control plane:  CatalogService · EnrollmentService · AllocationService · PhaseService
        │                              │ publishes events
Pure domain:    state_machine (FSM) · policies (prereq DAG / timetable / credit / duplicate) · graph
        │                              ▼
Data plane:     EventBus ──► WaitlistEngine (auto-promotion)
        │
Repository (single SQL module) ──► SQLite (+ append-only audit & event logs)
```

## Complexity dimensions addressed

| Dimension | Where |
|---|---|
| Structural | 4-layer separation; single SQL seam; pure domain modules; event bus |
| Behavioral | Enrollment FSM + audit; 4-stage semester machine with handover snapshots; FCFS/lottery/waitlist stage behaviors; capability-matrix RBAC |
| Semantic | Versioned immutable catalog; prerequisite DAG (cycle detection + topological order); formal JSON Schemas; policy chain with reason codes |
