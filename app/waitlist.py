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
            # 与「该学生自己发起的选课/退课」互斥，保证策略校验看到稳定快照。
            # 调用方（drop）已释放锁后再发布事件，因此这里不会形成锁环。
            with EnrollmentService._student_lock(user["id"]):
                head = repo.get_enrollment(conn, head["id"])
                if head is None or head["state"] != S.WAITLISTED.value:
                    continue
                try:
                    # re-validate policies against the *current* world state
                    svc._run_policies(user, section,
                                      exclude_enrollment_id=head["id"])
                except DomainError as exc:
                    # head no longer eligible: expire it and keep scanning
                    svc._audit_and_publish(
                        head, S.EXPIRED, "system:waitlist",
                        f"候补晋升失败：{exc.code}：{exc.message}")
                    continue
                # 原子占座：并发的另一个晋升/抢课先把座位占掉时，本轮退出
                if not svc._assign_seat(head, "system:waitlist",
                                        "从候补队列晋升",
                                        from_state=S.WAITLISTED):
                    return
            payload = {"enrollment_id": head["id"], "section_id": section_id,
                       "student_id": head["student_id"]}
            repo.append_event(conn, EVT_WAITLIST_PROMOTED, payload)
            self.bus.publish(EVT_WAITLIST_PROMOTED, payload)
            return
