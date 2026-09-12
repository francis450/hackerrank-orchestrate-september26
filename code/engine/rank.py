"""The spec's six-key ranking sort over safe candidate plans.

Ordering (problem_statement.md, "Choosing Between Safe Plans"):
  1. completes the full request by desired_completion_date
  2. requires no spending changes
  3. minimises the total amount paid
  4. starts payment earlier
  5. uses fewer payments
  6. lowest payment_option_id

Key 1 is a penalty, never a disqualifier: a plan that overruns the deadline
still ranks, it just ranks behind every plan that does not.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from engine.plans import Candidate
from ingestion.records import Request


def sort_key(cand: Candidate, req: Request) -> tuple:
    """Total order implementing the six tie-breakers. Lower sorts better.

    option_id falls back to the empty string, so a plan carrying no option
    (full_payment, wait, partial_payment) wins a tie against an installment
    option rather than losing to it.
    """
    return (
        not cand.completes_by(req.desired_completion_date),
        bool(cand.spending_changes),
        cand.total_paid,
        cand.payments[0].day,
        len(cand.payments),
        cand.option_id or "",
    )


def best_candidate(candidates: Sequence[Candidate], req: Request) -> Optional[Candidate]:
    """Pick the winning plan, or None when nothing is eligible and safe."""
    if not candidates:
        return None
    return min(candidates, key=lambda c: sort_key(c, req))
