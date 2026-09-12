"""Enumerate and apply permitted stop/reduce_to spending changes.

Only attempted when the user would accept paying in full today and the forecast
says they cannot. Streams are walked in ascending event_id of their most recent
settled event, and each one contributes its MILDEST permitted action - reduce
where the user allows reducing, stop only where they do not. Changes accumulate
until a full payment today becomes safe, up to three.

Walking in a fixed order rather than searching for the smallest sufficient set
is deliberate: golden request_21 stops event_1815 and reduces event_1816 even
though reducing event_1816 alone would close the gap.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Optional

from ingestion.records import Event, Profile, Request
from output.contract import Payment, money
from state.reconstruct import CashFlow, UserState

MAX_CHANGES = 3
STOPPABLE = frozenset({"stoppable", "reducible_or_stoppable"})
REDUCIBLE = frozenset({"reducible", "reducible_or_stoppable"})


@dataclass(frozen=True)
class Stream:
    """One changeable recurring outflow, addressed by its latest settled event."""
    anchor_event_id: str
    event_type: str
    category: str
    action: str                      # "reduce" | "stop"
    new_amount: Optional[Decimal]    # home currency, reduce only

    def token(self) -> str:
        if self.action == "stop":
            return f"stop:{self.anchor_event_id}"
        return f"reduce_to:{self.anchor_event_id}:{money(self.new_amount)}"


def _event_no(event_id: str) -> tuple[int, str]:
    tail = event_id.rsplit("_", 1)[-1]
    return (int(tail), event_id) if tail.isdigit() else (1 << 30, event_id)


def may_stop(ev: Event, prof: Profile) -> bool:
    return (ev.flexibility in STOPPABLE
            and ev.category not in prof.expense_categories_to_protect
            and ev.category in prof.expense_categories_user_is_willing_to_stop)


def may_reduce(ev: Event, prof: Profile) -> bool:
    return (ev.flexibility in REDUCIBLE
            and ev.category not in prof.expense_categories_to_protect
            and ev.category in prof.expense_categories_user_is_willing_to_reduce
            and ev.minimum_allowed_amount is not None)


def _mildest(ev: Event, prof: Profile, ds, home: str) -> Optional[Stream]:
    """Reduce if the user permits reducing this category, else stop, else nothing."""
    if may_reduce(ev, prof):
        floor = ev.minimum_allowed_amount
        if ev.currency != home and ev.settlement_date is not None:
            floor = ds.fx_convert(floor, ev.currency, home, ev.settlement_date)
        return Stream(ev.event_id, ev.event_type, ev.category, "reduce", floor)
    if may_stop(ev, prof):
        return Stream(ev.event_id, ev.event_type, ev.category, "stop", None)
    return None


def eligible_streams(req: Request, ds, state: UserState, prof: Profile) -> list[Stream]:
    """Changeable streams, ordered by the event_id of each stream's latest settled row."""
    home = prof.home_currency
    seen: set[str] = set()
    streams: list[Stream] = []
    for recurring in state.recurring_expenses:
        anchor_id = max(recurring.source_event_ids, key=_event_no, default="")
        anchor = ds.events_by_id.get(anchor_id)
        if anchor is None or anchor_id in seen:
            continue
        stream = _mildest(anchor, prof, ds, home)
        if stream is not None:
            seen.add(anchor_id)
            streams.append(stream)
    for flow in state.flows:
        ev = ds.events_by_id.get(flow.source_event_id)
        if ev is None or not ev.is_debit or ev.event_id in seen:
            continue
        if any(s.event_type == ev.event_type and s.category == ev.category for s in streams):
            continue
        stream = _mildest(ev, prof, ds, home)
        if stream is not None:
            seen.add(ev.event_id)
            streams.append(stream)
    return sorted(streams, key=lambda s: _event_no(s.anchor_event_id))


def apply_stream(flows: list[CashFlow], stream: Stream) -> list[CashFlow]:
    """Apply one change to EVERY future occurrence of its stream in the forecast."""
    out: list[CashFlow] = []
    for flow in flows:
        matches = (flow.amount < 0 and flow.event_type == stream.event_type
                   and flow.category == stream.category)
        if not matches:
            out.append(flow)
            continue
        if stream.action == "stop":
            continue
        out.append(replace(flow, amount=-stream.new_amount))
    return out


def find_spending_changes(req: Request, ds, fc, prof: Profile,
                          max_changes: int = MAX_CHANGES):
    """Accumulate changes until paying in full today is safe.

    Returns (tokens, adjusted_forecast) or None when no permitted combination of
    up to max_changes makes a full payment safe today.
    """
    from engine.forecast import build_forecast, plan_is_safe  # local: avoids a cycle

    state = fc.state
    if state is None:
        return None
    payment = [Payment(day=req.request_date, amount=req.requested_amount)]
    flows = list(state.flows)
    tokens: list[str] = []
    for stream in eligible_streams(req, ds, state, prof):
        if len(tokens) >= max_changes:
            break
        flows = apply_stream(flows, stream)
        tokens.append(stream.token())
        adjusted = build_forecast(replace(state, flows=flows),
                                  intraday_debits_first=fc.intraday_debits_first)
        if plan_is_safe(adjusted, payment):
            return tokens, adjusted
    return None
