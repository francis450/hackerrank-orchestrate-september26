"""Recurrence detection for expenses and semantic classification for income.

Split out of reconstruct.py to keep both files inside the line budget. Every
threshold here is a named constant so it can be tuned against the golden rows.
"""
from __future__ import annotations

import calendar
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Iterable, Literal, Optional, Sequence

from ingestion.records import Event

AggregationRule = Literal["mean", "median", "last", "p75"]

MIN_OCCURRENCES = 2
MONTHLY_GAP_LO, MONTHLY_GAP_HI = 27, 32
MAX_DISTINCT_DOM = 2
INCOME_CV_LIMIT = 0.05

#: A settled income row whose description matches any of these is a one-off:
#: it describes money that arrived once and must never be projected forward.
ONE_OFF_INCOME_TERMS = ("bonus", "arrears", "prize", "windfall", "prorated",
                        "first salary", "reimburse", "refund", "severance", "settlement")
#: A most-recent income description containing this means the stream has stopped.
ENDED_INCOME_TERM = "final"


class Cadence(Enum):
    MONTHLY = "monthly"
    INTERVAL = "interval"
    NONE = "none"


@dataclass
class RecurringExpense:
    """A detected recurring debit stream, ready to project forward."""
    event_type: str
    category: str
    cadence: Cadence
    amount: Decimal
    day_of_month: Optional[int] = None
    step_days: Optional[int] = None
    last_seen: Optional[date] = None
    source_event_ids: tuple[str, ...] = ()

    def project(self, start: date, end: date) -> list[date]:
        """Dates this stream is expected to hit within (start, end]."""
        if self.cadence is Cadence.MONTHLY and self.day_of_month:
            return _monthly_dates(self.day_of_month, start, end)
        if self.cadence is Cadence.INTERVAL and self.step_days and self.last_seen:
            out, cursor = [], self.last_seen
            while cursor <= end:
                cursor += timedelta(days=self.step_days)
                if start < cursor <= end:
                    out.append(cursor)
            return out
        return []


@dataclass
class IncomeStream:
    """The projected income picture: an optional anchor plus a monthly continuation."""
    kind: str  # anchored | recurring | ended | none
    amount: Decimal = Decimal(0)
    anchor_date: Optional[date] = None
    day_of_month: Optional[int] = None
    note: str = ""

    def project(self, start: date, end: date) -> list[tuple[date, Decimal]]:
        if self.kind in ("ended", "none") or self.amount <= 0:
            return []
        out: list[tuple[date, Decimal]] = []
        if self.anchor_date and start < self.anchor_date <= end:
            out.append((self.anchor_date, self.amount))
        if self.day_of_month:
            after = self.anchor_date or start
            out += [(d, self.amount) for d in _monthly_dates(self.day_of_month, after, end)]
        return out


def _clamp_dom(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _monthly_dates(day_of_month: int, after: date, end: date) -> list[date]:
    """Every month's day_of_month strictly after `after`, up to and including `end`."""
    out: list[date] = []
    year, month = after.year, after.month
    for _ in range(48):
        cursor = _clamp_dom(year, month, day_of_month)
        if cursor > end:
            break
        if cursor > after:
            out.append(cursor)
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def aggregate(values: Sequence[Decimal], rule: AggregationRule) -> Decimal:
    """Collapse an amount history to the figure to project. Tunable per run."""
    if not values:
        return Decimal(0)
    if rule == "last":
        return values[-1]
    if rule == "median":
        return Decimal(str(statistics.median([float(v) for v in values])))
    if rule == "p75":
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(len(ordered) * 0.75))
        return ordered[idx]
    return sum(values, Decimal(0)) / Decimal(len(values))


def _gaps(days: Sequence[date]) -> list[int]:
    return [(b - a).days for a, b in zip(days, days[1:])]


def _median_gap(days: Sequence[date]) -> Optional[float]:
    gaps = _gaps(days)
    return statistics.median(gaps) if gaps else None


def detect_expense_recurrence(events: Sequence[Event], amounts: Sequence[Decimal],
                              rule: AggregationRule) -> Optional[RecurringExpense]:
    """Classify one (event_type, category) group of settled debits.

    Monthly when the day-of-month is stable (at most two distinct values) and the
    median gap looks like a month; otherwise a fixed interval stepped from the last
    occurrence. Fewer than MIN_OCCURRENCES rows is not evidence of recurrence.
    """
    if len(events) < MIN_OCCURRENCES:
        return None
    days = [e.settlement_date or e.event_date for e in events]
    if any(d is None for d in days):
        return None
    days = sorted(days)
    gap = _median_gap(days)
    if gap is None:
        return None
    doms = {d.day for d in days}
    amount = aggregate(amounts, rule)
    common = dict(event_type=events[0].event_type, category=events[0].category,
                  amount=amount, last_seen=days[-1],
                  source_event_ids=tuple(e.event_id for e in events))
    if len(doms) <= MAX_DISTINCT_DOM and MONTHLY_GAP_LO <= gap <= MONTHLY_GAP_HI:
        return RecurringExpense(cadence=Cadence.MONTHLY, day_of_month=days[-1].day, **common)
    step = max(1, int(round(gap)))
    return RecurringExpense(cadence=Cadence.INTERVAL, step_days=step, **common)


def is_one_off_income(description: str) -> bool:
    text = (description or "").lower()
    return any(term in text for term in ONE_OFF_INCOME_TERMS)


def dominant_stream(history: Sequence[Event]) -> list[Event]:
    """Isolate the user's main income stream by description.

    A user can hold several income streams at once - a constant monthly salary
    alongside variable commission or gig payouts. Judging them as one blended
    series destroys the cadence and amount-stability signals that identify the
    salary, so each description is assessed separately and the stream with the
    most occurrences wins (ties break toward the most recent).
    """
    groups: dict[str, list[Event]] = {}
    for ev in history:
        groups.setdefault((ev.description or "").strip().lower(), []).append(ev)
    if not groups:
        return []
    def rank(item: tuple[str, list[Event]]) -> tuple:
        evs = item[1]
        return (len(evs), max((e.settlement_date or e.event_date) for e in evs))
    return max(groups.items(), key=rank)[1]


def coefficient_of_variation(values: Sequence[Decimal]) -> float:
    floats = [float(v) for v in values]
    if len(floats) < 2 or statistics.fmean(floats) == 0:
        return float("inf")
    return statistics.pstdev(floats) / abs(statistics.fmean(floats))


def classify_income(settled: Sequence[Event], scheduled: Sequence[Event],
                    as_of: date, to_home: Callable[[Event], Optional[Decimal]],
                    rule: AggregationRule,
                    cv_limit: float = INCOME_CV_LIMIT) -> IncomeStream:
    """Decide what income, if any, to project past as_of.

    Order matters: an explicit scheduled credit anchors the forecast, a most-recent
    'final' payroll ends it, and everything else must prove regular monthly cadence
    with near-constant amounts before it is projected at all.
    """
    history = sorted([e for e in settled if (e.settlement_date or e.event_date)],
                     key=lambda e: (e.settlement_date or e.event_date, e.event_id))
    future = sorted([e for e in scheduled if (e.settlement_date or e.event_date)
                     and (e.settlement_date or e.event_date) > as_of],
                    key=lambda e: (e.settlement_date or e.event_date, e.event_id))
    if history and ENDED_INCOME_TERM in (history[-1].description or "").lower():
        return IncomeStream(kind="ended", note="most recent income marked final")
    if future:
        anchor = future[0]
        when = anchor.settlement_date or anchor.event_date
        amount = to_home(anchor) or Decimal(0)
        return IncomeStream(kind="anchored", amount=amount, anchor_date=when,
                            day_of_month=when.day, note=f"anchored on {anchor.event_id}")
    stream = dominant_stream([e for e in history if not is_one_off_income(e.description)])
    if len(stream) < MIN_OCCURRENCES:
        return IncomeStream(kind="none", note="no repeated regular income")
    days = [e.settlement_date or e.event_date for e in stream]
    gap = _median_gap(days)
    if gap is None or not MONTHLY_GAP_LO <= gap <= MONTHLY_GAP_HI:
        return IncomeStream(kind="none", note="income cadence is not monthly")
    amounts = [a for a in (to_home(e) for e in stream) if a is not None]
    if coefficient_of_variation(amounts) >= cv_limit:
        return IncomeStream(kind="none", note="income amounts too variable to project")
    return IncomeStream(kind="recurring", amount=aggregate(amounts, rule),
                        day_of_month=days[-1].day, note=f"{len(stream)} regular payments")
