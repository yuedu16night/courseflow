"""WaitlistEngine: event-driven reactive component.

Subscribes to `section.seat_released` events published by the
EnrollmentService on a drop, and promotes the FIFO head of the
waitlist whose constraint policies still pass. This removes polling
loops and keeps enrollment logic decoupled from waitlist mechanics
(event-driven orchestration).

The engine receives a connection *factory* (not a connection) because
it executes on whatever thread publishes the event; each promotion
cycle opens a fresh SQLite connection for thread safety.
"""
from __future__ import annotations

from typing import Callable

from app import repositories as repo
from app.enums import EnrollmentState as S
from app.events import (
    EVT_SEAT_RELEASED,
    EVT_WAITLIST_PROMOTED,
    EventBus,
)
from app.services import DomainError, EnrollmentService


class WaitlistEngine:
    def __init__(self, conn_factory: Callable, bus: EventBus):
        self.conn_factory = conn_factory
        self.bus = bus

    def register(self) -> None:
        self.bus.subscribe(EVT_SEAT_RELEASED, self._on_seat_released)

    def _on_seat_released(self, event) -> None:
        section_id = event.payload["section_id"]
        conn = self.conn_factory()
        try:
            svc = EnrollmentService(conn, self.bus)
            self._promote_loop(conn, svc, section_id)
        finally:
            conn.close()

    def _promote_loop(self, conn, svc: EnrollmentService, section_id: int) -> None:
        while True:
            head = repo.waitlist_head(conn, section_id)
            if head is None:
                return
            section = repo.get_section(conn, section_id)
            if svc._free_seats(section) <= 0:
                return
            user = repo.get_user(conn, head["student_id"])
            try:
                # re-validate policies against the *current* world state
                svc._run_policies(user, section,
                                  exclude_enrollment_id=head["id"])
            except DomainError as exc:
                # head no longer eligible: expire it and keep scanning
                svc._audit_and_publish(
                    head, S.EXPIRED, "system:waitlist",
                    f"promotion failed: {exc.code}: {exc.message}")
                continue
            svc._audit_and_publish(
                head, S.ENROLLED, "system:waitlist", "promoted from waitlist")
            payload = {"enrollment_id": head["id"], "section_id": section_id,
                       "student_id": head["student_id"]}
            repo.append_event(conn, EVT_WAITLIST_PROMOTED, payload)
            self.bus.publish(EVT_WAITLIST_PROMOTED, payload)
            return
