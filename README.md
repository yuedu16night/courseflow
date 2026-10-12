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
python -m pytest tests -q      # 73 automated tests (incl. the agent layer)
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
auto-promote → inspect the audit trail and event log. Students also get a
**智能选课助手** tab (see "Agent layer" below).

## Agent layer (optional)

Two composable components sit *above* the service layer, alongside the
browser UI — they reuse the services rather than reimplementing them:

- **Scheme A — `PlannerAgent` (`app/agent/planner.py`)**: turns the
  system's *single-request validation* into *multi-course combination
  search*. It enumerates feasible course sets under the prerequisite DAG,
  timetable-overlap, credit-ceiling and duplicate/retake constraints, ranks
  them deterministically, and explains every choice (and every exclusion)
  with the same machine-readable reason codes the policy chain uses.
  Read-only, reproducible, fully unit-tested.
- **Scheme B — orchestration (`app/agent/tools.py`, `llm.py`,
  `orchestrator.py`)**: exposes the existing use-cases as LLM tools
  (function calling) so the system can be driven in natural language. The
  LLM only decides *which* tools to call; all planning comes from scheme A
  and all validation from the existing policy chain.

Default credentials and the LLM are independent: **scheme B runs in a
deterministic offline rule mode unless an API key is configured**, so the
system works with no network at all.

```bash
# Optional: enable the LLM orchestration path (any OpenAI-compatible gateway)
export AGENT_LLM_BASE_URL=https://api.deepseek.com/v1   # or DashScope / Zhipu / local Ollama
export AGENT_LLM_API_KEY=sk-...                         # keep out of git (see .gitignore)
export AGENT_LLM_MODEL=deepseek-chat
```

The LLM path has been verified against a live DeepSeek endpoint end to end
(intent parsing, tool calls, write proposal, confirm-and-execute). The
system prompt carries two constraints that a real model otherwise gets
wrong: it must *actually call* the write tool rather than merely describing
a proposal in prose (a described proposal produces no confirmation button),
and an explicit "帮我选 X" must produce the proposal directly instead of
re-asking for permission first.

Two invariants hold regardless of mode:

1. **Write operations are never executed by the agent.** `submit_enrollment`
   / `drop_enrollment` only produce a *pending proposal*; the user must
   confirm it, and the confirmed action is then executed through the normal
   REST endpoint — so the constraint policy chain and RBAC always apply.
2. **The agent acts as the signed-in user.** Every tool call runs under the
   caller's identity, so row-level permissions hold (a student agent cannot
   see another student's data) and every turn is appended to the audit
   `event_log` as `agent.turn`.

## Architecture at a glance

```text
Consumers (parallel):  Browser UI  ·  Agent layer (planner + LLM orchestrator)
                                │            │  tools only, never raw SQL
                                ▼            ▼
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
| Structural | 4-layer separation; single SQL seam; pure domain modules; event bus; agent layer as a second consumer of the same service API |
| Behavioral | Enrollment FSM + audit; 4-stage semester machine with handover snapshots; FCFS/lottery/waitlist stage behaviors; capability-matrix RBAC; agent turns in the audit log; propose→confirm→execute write protocol |
| Semantic | Versioned immutable catalog; prerequisite DAG (cycle detection + topological order); formal JSON Schemas; policy chain with reason codes; constraint-satisfying combination search (PlannerAgent) |
