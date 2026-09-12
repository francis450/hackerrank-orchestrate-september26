"""Reconstruct a user's cash position as at a request_date.

Cash-state rules (AGENTS.md 6.3, refined by D001):
  * failed and cancelled rows never move cash
  * non_cash direction and investment_valuation rows are not cash
  * pending credits are never counted; pending and scheduled debits are, on
    settlement_date; scheduled credits are confirmed future income and count
  * duplicate rows sharing (user_id, amount, event_date, category, direction)
    collapse to the first occurrence in event_id order
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Optional

from ingestion.loader import Dataset, FxRateUnavailable
from ingestion.records import Event, Profile, Request
from state.recurrence import (INCOME_CV_LIMIT, MIN_INTERVAL_OCCURRENCES, AggregationRule,
                              Cadence, IncomeStream, RecurringExpense, classify_income,
                              detect_expense_recurrence)

DEAD_STATUSES = frozenset({"failed", "cancelled"})
NON_CASH_TYPES = frozenset({"investment_valuation"})
#: A projected instalment this close to an explicit dated row is the same payment.
DEDUPE_WINDOW_DAYS = 3


@dataclass
class CashFlow:
    """One dated, home-currency cash movement. Credits positive, debits negative."""
    day: date
    amount: Decimal
    category: str
    source_event_id: str = ""
    kind: str = "actual"
    event_type: str = ""

    @property
    def is_projected(self) -> bool:
        return self.kind == "projected"


@dataclass
class MessageOverride:
    """Phase 3 hook: a fact extracted from messages/images that amends an event."""
    event_id: str
    action: str  # cancel | amend_amount | delay | confirm
    amount: Optional[Decimal] = None
    new_date: Optional[date] = None
    source: str = ""


@dataclass
class UserState:
    user_id: str
    as_of: date
    currency: str
    opening_balance: Decimal
    minimum_balance: Decimal
    flows: list[CashFlow] = field(default_factory=list)
    recurring_expenses: list[RecurringExpense] = field(default_factory=list)
    income: Optional[IncomeStream] = None
    message_overrides: list[MessageOverride] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def flexible_events(self, ds: Dataset) -> list[Event]:
        ids = {f.source_event_id for f in self.flows if f.source_event_id}
        evs = [ds.events_by_id[i] for i in ids if i in ds.events_by_id]
        return sorted((e for e in evs if e.is_flexible), key=lambda e: e.event_id)


def _event_no(event_id: str) -> tuple[int, str]:
    tail = event_id.rsplit("_", 1)[-1]
    return (int(tail), event_id) if tail.isdigit() else (1 << 30, event_id)


def _is_cash(ev: Event) -> bool:
    return (ev.status not in DEAD_STATUSES
            and ev.direction != "non_cash"
            and ev.event_type not in NON_CASH_TYPES)


def _counts_now(ev: Event) -> bool:
    """Pending credits are speculative; pending/scheduled debits are committed."""
    if ev.status == "pending" and ev.direction == "credit":
        return False
    return True


def dedupe(events: list[Event]) -> tuple[list[Event], int]:
    """Collapse rows sharing (user_id, amount, event_date, category, direction)."""
    seen: set[tuple] = set()
    kept: list[Event] = []
    for ev in sorted(events, key=lambda e: _event_no(e.event_id)):
        key = (ev.user_id, ev.amount, ev.event_date, ev.category, ev.direction)
        if key in seen:
            continue
        seen.add(key)
        kept.append(ev)
    return kept, len(events) - len(kept)


def build_state(req: Request, ds: Dataset, aggregation: AggregationRule = "mean",
                horizon_days: int = 90, income_cv_limit: float = INCOME_CV_LIMIT,
                income_aggregation: AggregationRule = "last",
                include_request_day: bool = False,
                min_interval_occurrences: int = MIN_INTERVAL_OCCURRENCES,
                monthly_rule: Optional[AggregationRule] = None,
                interval_rule: Optional[AggregationRule] = None) -> UserState:
    """Assemble the forecastable cash position for one request.

    include_request_day decides whether flows dated on request_date itself land
    inside the window. It is expressed as an exclusive lower bound one day
    earlier, so every projector shares the same boundary.
    """
    prof: Profile = ds.profile_for(req)
    as_of, end = req.request_date, req.request_date + timedelta(days=horizon_days)
    after = as_of - timedelta(days=1) if include_request_day else as_of
    state = UserState(user_id=req.user_id, as_of=as_of, currency=prof.home_currency,
                      opening_balance=prof.current_available_balance,
                      minimum_balance=prof.minimum_balance_to_keep)

    raw = [e for e in ds.events_by_user.get(req.user_id, []) if _is_cash(e)]
    kept, dropped = dedupe(raw)
    if dropped:
        state.notes.append(f"dropped {dropped} duplicate event rows")

    def to_home(ev: Event) -> Optional[Decimal]:
        try:
            return ds.to_home(ev, prof)
        except FxRateUnavailable as exc:
            state.notes.append(f"fx unavailable for {ev.event_id}: {exc}")
            return None

    _add_dated_flows(state, kept, after, end, to_home)
    _add_recurring_expenses(state, kept, req, as_of, after, end, to_home, aggregation,
                            min_interval_occurrences, monthly_rule, interval_rule)
    _add_income(state, kept, as_of, after, end, to_home, income_aggregation, income_cv_limit)
    state.flows.sort(key=lambda f: (f.day, f.source_event_id))
    return state


def _add_dated_flows(state: UserState, events: list[Event], after: date, end: date,
                     to_home: Callable[[Event], Optional[Decimal]]) -> None:
    """Explicit rows that move cash inside the forecast window."""
    for ev in events:
        when = ev.settlement_date or ev.event_date
        if when is None or not (after < when <= end) or not _counts_now(ev):
            continue
        amount = to_home(ev)
        if amount is None:
            continue
        signed = -amount if ev.is_debit else amount
        state.flows.append(CashFlow(day=when, amount=signed, category=ev.category,
                                    source_event_id=ev.event_id, kind="actual",
                                    event_type=ev.event_type))


def _add_recurring_expenses(state: UserState, events: list[Event], req: Request, as_of: date,
                            after: date, end: date,
                            to_home: Callable[[Event], Optional[Decimal]],
                            rule: AggregationRule, min_interval_occurrences: int,
                            monthly_rule: Optional[AggregationRule],
                            interval_rule: Optional[AggregationRule]) -> None:
    """Detect recurring debit streams from settled history and project them forward."""
    groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for ev in events:
        when = ev.settlement_date or ev.event_date
        if (ev.is_debit and ev.status == "settled" and when is not None and when <= as_of
                and ev.event_type != "income"):
            groups[(ev.event_type, ev.category)].append(ev)

    committed = {(f.event_type, f.category, f.day) for f in state.flows}
    for key in sorted(groups):
        history = sorted(groups[key], key=lambda e: (e.settlement_date or e.event_date))
        amounts = [a for a in (to_home(e) for e in history) if a is not None]
        if len(amounts) != len(history):
            continue
        detected = detect_expense_recurrence(history, amounts, rule, min_interval_occurrences,
                                             monthly_rule, interval_rule)
        if detected is None or detected.cadence is Cadence.NONE or detected.amount <= 0:
            continue
        state.recurring_expenses.append(detected)
        for day in detected.project(after, end):
            if _already_committed(committed, key, day):
                continue
            state.flows.append(CashFlow(day=day, amount=-detected.amount, category=key[1],
                                        kind="projected", event_type=key[0]))


def _already_committed(committed: set[tuple[str, str, date]], key: tuple[str, str],
                       day: date) -> bool:
    """True when an explicit dated row already covers this projected instalment."""
    return any((key[0], key[1], day + timedelta(days=offset)) in committed
               for offset in range(-DEDUPE_WINDOW_DAYS, DEDUPE_WINDOW_DAYS + 1))


def _add_income(state: UserState, events: list[Event], as_of: date, after: date, end: date,
                to_home: Callable[[Event], Optional[Decimal]], rule: AggregationRule,
                cv_limit: float) -> None:
    """Classify income semantically, then project only what is genuinely recurring."""
    income = [e for e in events if e.direction == "credit" and e.event_type == "income"]
    settled = [e for e in income if e.status == "settled"
               and (e.settlement_date or e.event_date) and (e.settlement_date or e.event_date) <= as_of]
    scheduled = [e for e in income if e.status == "scheduled"]
    stream = classify_income(settled, scheduled, as_of, to_home, rule, cv_limit)
    state.income = stream
    state.notes.append(f"income: {stream.kind} ({stream.note})")

    already = {f.day for f in state.flows if f.event_type == "income"}
    for day, amount in stream.project(after, end):
        if day in already or amount <= 0:
            continue
        state.flows.append(CashFlow(day=day, amount=amount, category="salary",
                                    kind="projected", event_type="income"))
