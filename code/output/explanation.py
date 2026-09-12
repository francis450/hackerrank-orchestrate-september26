"""decision_explanation rendering.

Every explanation is a template over solver inputs; no model is called. The
trailing figure is always the user's minimum_balance_to_keep, which holds for
all 25 rows of dataset/sample_requests.csv.

STUB: only the two not_recommended variants are wired up. The remaining
templates land with the plan builder.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ingestion.loader import Profile, Request


def money(currency: str, amount: Decimal) -> str:
    """Render 25256 as 'ZAR 25,256' and 620.4 as 'EUR 620.40'."""
    quant = Decimal(amount).quantize(Decimal("0.01"))
    if quant == quant.to_integral_value():
        return f"{currency} {int(quant):,}"
    return f"{currency} {quant:,.2f}"


def long_date(day: date) -> str:
    """Render 2024-03-20 as '20 March 2024'."""
    return f"{day.day} {day.strftime('%B')} {day.year}"


def not_recommended_no_option(req: Request, prof: Profile) -> str:
    """Used when nothing clears the minimum balance at all."""
    return (f"Do not make this payment by {long_date(req.desired_completion_date)}. "
            f"None of the available options keeps the "
            f"{money(prof.home_currency, prof.minimum_balance_to_keep)} minimum protected.")


def not_recommended_incomplete(req: Request, prof: Profile, amount_safe: Decimal) -> str:
    """Used when some cash is available today but the full amount never completes in 90 days."""
    return (f"Do not proceed with the {money(prof.home_currency, req.requested_amount)} request. "
            f"Although {money(prof.home_currency, amount_safe)} is available today, "
            f"the full amount cannot be completed safely within 90 days.")
