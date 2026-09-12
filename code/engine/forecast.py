"""90-day balance forecast and the closed-form safety queries built on it.

Paying X on request_date shifts every later balance down by X, so both
amount_safe_to_pay and earliest_date_for_full_payment fall out of one
suffix-minimum scan. That relationship is implemented here; the accuracy of
the answers depends entirely on the flows supplied by state.reconstruct.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from output.contract import HORIZON_DAYS, Payment
from state.reconstruct import UserState


@dataclass
class Forecast:
    """Daily projected closing balances over [start, start + horizon]."""
    start: date
    balances: list[Decimal]
    minimum_balance: Decimal

    def day_index(self, day: date) -> int:
        return (day - self.start).days

    def date_at(self, index: int) -> date:
        return self.start + timedelta(days=index)

    def suffix_min(self) -> list[Decimal]:
        """suffix_min[i] = min(balances[i:]), computed right to left."""
        out = [Decimal(0)] * len(self.balances)
        running = self.balances[-1]
        for i in range(len(self.balances) - 1, -1, -1):
            running = min(running, self.balances[i])
            out[i] = running
        return out


def build_forecast(state: UserState, horizon_days: int = HORIZON_DAYS) -> Forecast:
    """Project daily closing balances from the state's flows. STUB: flat balance."""
    balances = [state.opening_balance] * (horizon_days + 1)
    for flow in state.flows:
        idx = (flow.day - state.as_of).days
        if 0 <= idx <= horizon_days:
            for i in range(idx, horizon_days + 1):
                balances[i] += flow.amount
    return Forecast(start=state.as_of, balances=balances,
                    minimum_balance=state.minimum_balance)


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
    """True when the balance stays at or above the minimum on every forecast day."""
    running = list(fc.balances)
    for payment in plan:
        idx = fc.day_index(payment.day)
        if idx < 0 or idx >= len(running):
            return False
        for i in range(idx, len(running)):
            running[i] -= payment.amount
    return all(b >= fc.minimum_balance for b in running)
