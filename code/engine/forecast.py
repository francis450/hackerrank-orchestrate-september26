"""90-day balance forecast and the closed-form safety queries built on it.

D005 fixes the intra-day semantics as a hybrid: the day a payment is made is
judged at its closing balance (the payment clears after that day's credits
land), while every later day is judged at its conservative intra-day minimum.

Paying X on request_date shifts every later balance down by X, so both
amount_safe_to_pay and earliest_date_for_full_payment fall out of one
suffix-minimum scan. That relationship is implemented here; the accuracy of
the answers depends entirely on the flows supplied by state.reconstruct.

Ordering within a day matters. A day that pays rent in the morning and receives
salary in the evening dips below its closing balance in between, and a plan that
only checks closing balances would call that day safe. Setting
intraday_debits_first=False disables that conservatism entirely and reverts every
test to closing balances.
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
    #: The state this forecast was projected from, so spending changes can be
    #: applied to the underlying flows and the forecast rebuilt.
    state: Optional[UserState] = None

    def __post_init__(self) -> None:
        if not self.credits:
            self.credits = [Decimal(0)] * len(self.balances)

    @property
    def checked(self) -> list[Decimal]:
        """Conservative series for days the user is NOT paying on (D005 hybrid)."""
        if not self.intraday_debits_first:
            return self.balances
        return [b - c for b, c in zip(self.balances, self.credits)]

    def day_index(self, day: date) -> int:
        return (day - self.start).days

    def date_at(self, index: int) -> date:
        return self.start + timedelta(days=index)

    def tail_min(self) -> list[Decimal]:
        """tail_min[i] = min(checked[i+1:]), i.e. every day strictly after i.

        Paired with balances[i], this expresses the D005 hybrid: the paying day
        may spend that day's credits, every later day may not.
        """
        series = self.checked
        n = len(series)
        out = [Decimal("Infinity")] * n
        running = Decimal("Infinity")
        for i in range(n - 1, -1, -1):
            out[i] = running
            running = min(running, series[i])
        return out

    def floor_for_payment_on(self, index: int) -> Decimal:
        """Worst balance a single payment on `index` must survive."""
        return min(self.balances[index], self.tail_min()[index])


def build_forecast(state: UserState, horizon_days: int = HORIZON_DAYS,
                   intraday_debits_first: bool = True) -> Forecast:
    """Project daily balances over the horizon.

    Starts from the opening balance and applies every reconstructed cash flow
    cumulatively from its day onward, so balances[i] is the projected closing
    balance on day i. Flows outside the window are ignored.

    When intraday_debits_first is True, days the user is not paying on are
    tested at their intra-day minimum rather than their closing balance,
    assuming the worst plausible ordering: that day's debits clear before that
    day's credits land. Paying days keep their closing balance (D005 hybrid).
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
                    intraday_debits_first=intraday_debits_first, state=state)


def amount_safe_today(fc: Forecast, requested_amount: Decimal) -> Decimal:
    """Largest amount payable on day 0 that keeps every later balance above the minimum."""
    headroom = fc.floor_for_payment_on(0) - fc.minimum_balance
    return max(Decimal(0), min(headroom, requested_amount))


def earliest_full_payment(fc: Forecast, requested_amount: Decimal) -> Optional[date]:
    """First day on which one full payment survives that day and every later day."""
    tail = fc.tail_min()
    for i, closing in enumerate(fc.balances):
        if min(closing, tail[i]) - requested_amount >= fc.minimum_balance:
            return fc.date_at(i)
    return None


def plan_is_safe(fc: Forecast, plan: Sequence[Payment]) -> bool:
    """True when the balance stays at or above the minimum on every forecast day.

    Days the user pays on are judged at their closing balance, since the payment
    clears after that day's credits land; every other day is judged at its
    conservative intra-day minimum (D005 hybrid). Each payment is a debit, so it
    lowers its own day and every later day.
    """
    paying = {fc.day_index(p.day) for p in plan}
    series = [fc.balances[i] if i in paying else value
              for i, value in enumerate(fc.checked)]
    for payment in plan:
        idx = fc.day_index(payment.day)
        if idx < 0 or idx >= len(series):
            return False
        for i in range(idx, len(series)):
            series[i] -= payment.amount
    return all(b >= fc.minimum_balance for b in series)
