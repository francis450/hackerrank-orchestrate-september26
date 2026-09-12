"""Enumerate permitted stop/reduce_to spending changes.

Only non-protected, flexible events in a category the user allows may be touched,
at most three per decision, and one event may never be both stopped and reduced.

STUB: yields no change sets beyond the empty one.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ingestion.loader import Dataset, Event, Profile, Request

MAX_CHANGES = 3


@dataclass(frozen=True)
class ChangeSet:
    """A candidate bundle of spending changes plus the monthly cash it frees."""
    tokens: tuple[str, ...]
    freed_per_event: tuple[tuple[str, Decimal], ...]

    def __len__(self) -> int:
        return len(self.tokens)


def may_stop(ev: Event, prof: Profile) -> bool:
    return (ev.can_stop
            and ev.category not in prof.expense_categories_to_protect
            and ev.category in prof.expense_categories_user_is_willing_to_stop)


def may_reduce(ev: Event, prof: Profile) -> bool:
    return (ev.can_reduce
            and ev.category not in prof.expense_categories_to_protect
            and ev.category in prof.expense_categories_user_is_willing_to_reduce)


def eligible_events(req: Request, ds: Dataset) -> list[Event]:
    """Events this user is willing to stop or reduce, in stable event_id order."""
    prof = ds.profile_for(req)
    return sorted(
        (ev for ev in ds.events_by_user.get(req.user_id, [])
         if may_stop(ev, prof) or may_reduce(ev, prof)),
        key=lambda ev: ev.event_id,
    )


def enumerate_change_sets(req: Request, ds: Dataset) -> list[ChangeSet]:
    """All permitted change bundles of size 0..3. STUB: the empty set only."""
    return [ChangeSet(tokens=(), freed_per_event=())]
