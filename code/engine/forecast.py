"""90-day balance forecast and the closed-form safety queries built on it.

Paying X on request_date shifts every later balance down by X, so both
amount_safe_to_pay and earliest_date_for_full_payment fall out of one
suffix-minimum scan. That relationship is implemented here; the accuracy of
the answers depends entirely on the flows supplied by state.reconstruct.

Ordering within a day matters. A day that pays rent in the morning and receives
salary in the evening dips below its closing balance in between, and a plan that
only checks closing balances would call that day safe. With intraday_debits_first
the checked figure for each day is its intra-day minimum - the closing balance
less that day's credits - which is the conservative reading the spec asks for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from output.contract import HORIZON_DAYS, Payment
from state.reconstruct import UserState


@dataclass
class Forecast:
    """Daily projected balances over [start, start + horizon].

    `balances` holds closing balances. `credits` holds each day's incoming total,
    so `checked` can expose the intra-day minimum when debits are applied first.
    """
    start: date
    balances: list[Decimal]
    minimum_balance: Decimal
    credits: list[Decimal] = field(default_factory=list)
    intraday_debits_first: bool = True

    def __post_init__(self) -> None:
        if not self.credits:
            self.credits = [Decimal(0)] * len(self.balances)

    @property
    def checked(self) -> list[Decimal]:
        """The balance series every safety test runs against."""
        if not self.intraday_debits_first:
            return self.balances
        return [b - c for b, c in zip(self.balances, self.credits)]

    def day_index(self, day: date) -> int:
        return (day - self.start).days

    def date_at(self, index: int) -> date:
        return self.start + timedelta(days=index)

    def suffix_min(self) -> list[Decimal]:
        """suffix_min[i] = min(checked[i:]), computed right to left."""
        series = self.checked
        out = [Decimal(0)] * len(series)
        running = series[-1]
        for i in range(len(series) - 1, -1, -1):
            running = min(running, series[i])
            out[i] = running
        return out


def build_forecast(state: UserState, horizon_days: int = HORIZON_DAYS,
                   intraday_debits_first: bool = True) -> Forecast:
    """Project daily balances over the horizon.

    Starts from the opening balance and applies every reconstructed cash flow
    cumulatively from its day onward, so balances[i] is the projected closing
    balance on day i. Flows outside the window are ignored.

    When intraday_debits_first is True the safety tests read each day's
    intra-day minimum instead of its closing balance, assuming the worst
    plausible ordering: that day's debits clear before that day's credits land.
    """
    balances = [state.opening_balance] * (horizon_days + 1)
    credits = [Decimal(0)] * (horizon_days + 1)
    for flow in state.flows:
        idx = (flow.day - state.as_of).days
        if not 0 <= idx <= horizon_days:
            continue
        for i in range(idx, horizon_days + 1):
            balances[i] += flow.amount
        if flow.amount > 0:
            credits[idx] += flow.amount
    return Forecast(start=state.as_of, balances=balances, credits=credits,
                    minimum_balance=state.minimum_balance,
                    intraday_debits_first=intraday_debits_first)


def amount_safe_today(fc: Forecast, requested_amount: Decimal) -> Decimal:
    """Largest amount payable on day 0 that keeps every later balance above the minimum."""
    headroom = fc.suffix_min()[0] - fc.minimum_balance
    return max(Decimal(0), min(headroom, requested_amount))


def earliest_full_payment(fc: Forecast, requested_amount: Decimal) -> Optional[date]:
    """First day whose suffix-minimum still clears the minimum after one full payment."""
    suffix = fc.suffix_min()
    for i, floor in enumerate(suffix):
        if floor - requested_amount >= fc.minimum_balance:
            return fc.date_at(i)
    return None


def plan_is_safe(fc: Forecast, plan: Sequence[Payment]) -> bool:
    """True when the checked balance stays at or above the minimum on every forecast day.

    Each payment is a debit, so it lowers the day's intra-day minimum as well as
    every later balance.
    """
    series = list(fc.checked)
    for payment in plan:
        idx = fc.day_index(payment.day)
        if idx < 0 or idx >= len(series):
            return False
        for i in range(idx, len(series)):
            series[i] -= payment.amount
    return all(b >= fc.minimum_balance for b in series)
