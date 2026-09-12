"""Candidate plan generation: full, partial, installments, wait.

STUB: generates nothing, so the pipeline falls through to not_recommended.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from engine.forecast import Forecast
from ingestion.loader import Dataset, Profile, Request
from output.contract import Payment


@dataclass
class Candidate:
    """One eligible, safe way to proceed, ready for ranking."""
    method: str
    status: str
    payments: list[Payment] = field(default_factory=list)
    spending_changes: list[str] = field(default_factory=list)
    option_id: str = ""
    earliest_full: Optional[date] = None

    @property
    def total_paid(self) -> Decimal:
        return sum((p.amount for p in self.payments), Decimal(0))

    @property
    def completion_date(self) -> Optional[date]:
        return self.payments[-1].day if self.payments else None

    def completes_by(self, deadline: date) -> bool:
        end = self.completion_date
        return end is not None and end <= deadline


def build_candidates(req: Request, ds: Dataset, fc: Forecast, prof: Profile,
                     amount_safe: Decimal, earliest: Optional[date]) -> list[Candidate]:
    """Enumerate every eligible, safe plan for this request. STUB: none."""
    return []


def installment_payments(opt) -> list[Payment]:
    """Expand a payment option into its exact dated schedule."""
    from datetime import timedelta
    step = timedelta(days=opt.payment_frequency_days or 0)
    return [Payment(day=opt.first_payment_date + step * i, amount=opt.payment_amount)
            for i in range(opt.number_of_payments)]
