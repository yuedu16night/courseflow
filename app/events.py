"""In-process publish/subscribe event bus.

The bus decouples orchestration services (publishers) from reactive
components such as the WaitlistEngine (subscribers). Events are typed,
carry an immutable payload, and every publication is persisted to the
event log for traceability (event sourcing style audit of system
behaviour).
"""
from __future__ import annotations

import itertools
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------
EVT_ENROLLMENT_STATE = "enrollment.state_changed"
EVT_SEAT_RELEASED = "section.seat_released"
EVT_WAITLIST_PROMOTED = "waitlist.promoted"
EVT_PHASE_CHANGED = "phase.changed"
EVT_CATALOG_VERSIONED = "catalog.versioned"


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any]
    ts: float = field(default_factory=time.time)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._ids = itertools.count(1)
        # ring of recent events, useful for tests and the /admin/events view
        self.history: list[Event] = []

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subscribers[event_type].append(handler)

    def publish(self, event_type: str, payload: dict[str, Any]) -> Event:
        event = Event(type=event_type, payload=dict(payload), ts=time.time())
        self.history.append(event)
        for handler in self._subscribers[event_type]:
            handler(event)
        return event

    def reset(self) -> None:
        self._subscribers.clear()
        self.history.clear()
