# Project Proposal — CourseFlow: A Course Registration & Timetable Orchestration System

**Course:** MAiNS (Models and Algorithms in Networks and Systems)
**Project Track:** Option 4 — Self-Defined Project
**Team Members:** [Name 1], [Name 2], [Name 3]
**Contact:** [email]

## 1. What We Propose

We propose to architect and implement **CourseFlow**, a university course
registration and timetable orchestration system. Registration week is a
canonical example of macroscopic complexity in a campus information system:
thousands of students submit concurrent, conflicting requests against a
shared pool of capacity-constrained sections, under rules that change
over time (prerequisite chains, credit ceilings, multi-stage enrollment
calendars) and by actor role (students, instructors, the registrar's
office). A naive monolithic "database + form" implementation fails here:
constraint logic leaks into every endpoint, stage-dependent behavior is
scattered across `if` statements, and no one can reconstruct why a student
was admitted, waitlisted, or rejected.

## 2. Why It Has Non-Trivial Complexity (per the MAiNS framework)

- **Structural complexity.** CourseFlow enforces a strict layered
  architecture: REST API → orchestration services → pure domain policies →
  repository/persistence, with an event bus decoupling reactive components
  from request handling. HTTP concerns never leak into domain logic.
- **Behavioral & orchestration complexity.** Each enrollment is governed by
  an explicit finite state machine (SUBMITTED → VALIDATED → ENROLLED /
  WAITLISTED / REJECTED → …), audited in an append-only trail. The semester
  itself is a multi-stage pipeline (pre-enrollment → allocation → add/drop →
  closed): every stage transition performs a **stage handover** that
  snapshots in-flight context for the next stage, and stage behavior differs
  (capacity-blind wishes vs. seeded batch lottery vs. first-come-first-served
  with event-driven waitlist promotion). RBAC (student / instructor /
  registrar / admin) is enforced through a capability matrix plus row-level
  checks (e.g., instructors may only view rosters of their own sections).
- **Semantic & domain complexity.** The course catalog is a formal, versioned
  registry: course entries are immutable per catalog version and carry a
  **prerequisite DAG** whose integrity is checked on every mutation (cycle
  detection) and analyzed via topological ordering. Enrollment requests are
  validated against a composable constraint system: prerequisite coverage,
  timetable interval-overlap conflict detection, per-semester credit
  ceilings, and duplicate/retake rules — each producing machine-readable
  reason codes.

## 3. Deliverables & Plan

1. **Working codebase** (Python 3.11, FastAPI, SQLite, pytest): a REST API
   plus a browser demo UI, organized into cleanly separated modules
   (`domain / services / infrastructure / api`), with an automated test
   suite covering policies, the FSM, RBAC, phase handover, batch
   allocation, and waitlist promotion (~37 tests).
2. **System Architecture & Complexity Report** (8–12 pages) following the
   standardized submission template: component architecture (C4-style),
   data models & formal JSON Schemas, FSM + sequence diagrams, a
   complexity-management evaluation table, scaling/trade-off analysis, and
   an end-to-end execution trace captured from a scripted demo.
3. **Live demo** (for the bonus): a scripted scenario registering several
   students through all three stages, demonstrating rejection reasons,
   lottery allocation, and automatic waitlist promotion.

## 4. Scope

The system models one active semester and in-memory point-to-point events;
multi-semester registries, distributed event brokers, and real payment/
integration with the university SIS are out of scope but discussed as
future work in the report.
