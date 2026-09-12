"""decision_explanation rendering: pure string templates, no model call.

Every explanation is a template over solver inputs, keyed by
(affordability_status, recommended_payment_method, has_spending_changes). The
trailing figure is always the user's minimum_balance_to_keep, which holds for
all 25 rows of dataset/sample_requests.csv.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from ingestion.records import Profile, Request
from output.contract import Payment

_STOP_RE = re.compile(r"^stop:([A-Za-z0-9_]+)$")
_REDUCE_RE = re.compile(r"^reduce_to:([A-Za-z0-9_]+):(\d+(?:\.\d+)?)$")


def money(currency: str, amount: Decimal) -> str:
    """Render 25256 as 'ZAR 25,256' and 620.4 as 'EUR 620.40'."""
    quant = Decimal(amount).quantize(Decimal("0.01"))
    if quant == quant.to_integral_value():
        return f"{currency} {int(quant):,}"
    return f"{currency} {quant:,.2f}"


def long_date(day: date) -> str:
    """Render 2025-08-08 as '8 August 2025'."""
    return f"{day.day} {day.strftime('%B')} {day.year}"


def _leaves_at_least(prof: Profile) -> str:
    return f"This leaves at least {money(prof.home_currency, prof.minimum_balance_to_keep)} available."


def change_phrases(changes: Sequence[str], ds, currency: str) -> str:
    """Turn spending-change tokens into prose, e.g. 'Stop the family streaming plan'.

    Joined with ' and '; only the first phrase is capitalised, matching the
    golden rows ('Stop the X and reduce the Y to Z').
    """
    phrases: list[str] = []
    for token in changes:
        stop, reduce = _STOP_RE.match(token), _REDUCE_RE.match(token)
        match = stop or reduce
        if not match:
            continue
        event = ds.events_by_id.get(match.group(1))
        label = (event.description if event else match.group(1)).strip().lower()
        if stop:
            phrases.append(f"stop the {label}")
        else:
            phrases.append(f"reduce the {label} to {money(currency, Decimal(reduce.group(2)))}")
    if not phrases:
        return ""
    joined = " and ".join(phrases)
    return joined[0].upper() + joined[1:]


def render(req: Request, prof: Profile, ds, status: str, method: str,
           payments: Sequence[Payment], changes: Sequence[str],
           earliest: Optional[date], amount_safe: Decimal,
           partial_blocked: bool = False) -> str:
    """Select and fill the template for this decision."""
    cur = prof.home_currency
    if method == "not_recommended":
        return _not_recommended(req, prof, amount_safe, partial_blocked)
    if method == "full_payment":
        if changes:
            return (f"{change_phrases(changes, ds, cur)}, then pay "
                    f"{money(cur, req.requested_amount)} today. {_leaves_at_least(prof)}")
        return (f"Pay {money(cur, req.requested_amount)} today. This leaves at least "
                f"{money(cur, prof.minimum_balance_to_keep)} available "
                f"over the next 90 days.")
    if method == "wait":
        when = payments[0].day if payments else earliest
        return (f"Pay {money(cur, req.requested_amount)} in full on {long_date(when)}. "
                f"Paying earlier would take the balance below the "
                f"{money(cur, prof.minimum_balance_to_keep)} minimum.")
    if method == "partial_payment":
        first, second = payments[0], payments[1]
        return (f"Pay {money(cur, first.amount)} today and the remaining "
                f"{money(cur, second.amount)} on {long_date(second.day)}. This completes "
                f"the full request and keeps the "
                f"{money(cur, prof.minimum_balance_to_keep)} minimum protected.")
    if method == "installments":
        return (f"Use {len(payments)} installments of {money(cur, payments[0].amount)}, "
                f"starting {long_date(payments[0].day)}. {_leaves_at_least(prof)}")
    return _not_recommended(req, prof, amount_safe, partial_blocked)


def _not_recommended(req: Request, prof: Profile, amount_safe: Decimal,
                     partial_blocked: bool) -> str:
    """Two variants, keyed on the mechanism that defeated the request.

    Variant B is reserved for the case where a partial payment was actually
    constructed - the request allows it, the user accepts it, and a non-trivial
    amount is safe today - and then failed only because no completion date
    exists inside the deadline. That is precisely "available today, but cannot
    be completed". Anything else means no option ever cleared the minimum.
    """
    cur = prof.home_currency
    if partial_blocked:
        return (f"Do not proceed with the {money(cur, req.requested_amount)} request. "
                f"Although {money(cur, amount_safe)} is available today, the full amount "
                f"cannot be completed safely within 90 days.")
    return (f"Do not make this payment by {long_date(req.desired_completion_date)}. "
            f"None of the available options keeps the "
            f"{money(cur, prof.minimum_balance_to_keep)} minimum protected.")
