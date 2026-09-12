"""The spec's 6-key ranking sort over safe candidate plans.

Ordering (problem_statement.md, "Choosing Between Safe Plans"):
  1. completes the full request by desired_completion_date
  2. requires no spending changes
  3. minimises the total amount paid
  4. starts payment earlier
  5. uses fewer payments
  6. lowest payment_option_id
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from engine.plans import Candidate
from ingestion.loader import Request

_FAR_FUTURE = date(9999, 12, 31)


def sort_key(cand: Candidate, req: Request) -> tuple:
    """Total order implementing the six tie-breakers. Lower sorts better."""
    return (
        0 if cand.completes_by(req.desired_completion_date) else 1,
        1 if cand.spending_changes else 0,
        cand.total_paid,
        cand.payments[0].day if cand.payments else _FAR_FUTURE,
        len(cand.payments),
        cand.option_id or "￿",
    )


def best_candidate(candidates: Sequence[Candidate], req: Request) -> Optional[Candidate]:
    """Pick the winning plan, or None when nothing is eligible and safe."""
    if not candidates:
        return None
    return min(candidates, key=lambda c: sort_key(c, req))
