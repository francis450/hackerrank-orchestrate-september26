"""Candidate plan generation: full_payment, wait, partial_payment, installments.

Two gates, in order. Eligibility asks whether the user would even consider this
plan - their accepted payment methods, whether the request allows splitting, the
installment-length ceiling. Safety then asks whether the plan survives the
90-day forecast under the D005 hybrid intra-day rule. A plan must clear both.

Ranking and spending changes live elsewhere (engine.rank, engine.spending_changes);
this module only enumerates what is on the table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from engine.forecast import Forecast, amount_safe_today, earliest_full_payment, plan_is_safe
from ingestion.records import PaymentOption, Profile, Request
from output.contract import Payment

DAYS_PER_MONTH = Decimal(30)


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


def installment_payments(opt: PaymentOption) -> list[Payment]:
    """Expand a payment option into its exact dated schedule.

    Amounts come straight from the option so the emitted plan matches the
    supplied offer. Where n * payment_amount drifts from total_payable_amount by
    rounding, the final payment absorbs the difference rather than spreading an
    error across every instalment.
    """
    step = timedelta(days=opt.payment_frequency_days or 0)
    payments = [Payment(day=opt.first_payment_date + step * k, amount=opt.payment_amount)
                for k in range(opt.number_of_payments)]
    if payments and opt.total_payable_amount is not None:
        drift = opt.total_payable_amount - sum((p.amount for p in payments), Decimal(0))
        if drift:
            payments[-1] = Payment(day=payments[-1].day, amount=payments[-1].amount + drift)
    return payments


def installment_months(opt: PaymentOption) -> Decimal:
    """Schedule length in months, as the ceiling in max_installment_months measures it."""
    freq = Decimal(opt.payment_frequency_days or 0)
    return Decimal(opt.number_of_payments) * freq / DAYS_PER_MONTH


def _full_payment(req: Request, prof: Profile, fc: Forecast,
                  earliest: Optional[date]) -> Optional[Candidate]:
    if not prof.accepts("full_payment"):
        return None
    payments = [Payment(day=req.request_date, amount=req.requested_amount)]
    if not plan_is_safe(fc, payments):
        return None
    return Candidate(method="full_payment", status="affordable_now",
                     payments=payments, earliest_full=earliest)


def _wait(req: Request, prof: Profile, fc: Forecast,
          earliest: Optional[date]) -> Optional[Candidate]:
    """Waiting is still a full payment, so it needs full_payment to be acceptable."""
    if not prof.accepts("full_payment") or earliest is None or earliest <= req.request_date:
        return None
    payments = [Payment(day=earliest, amount=req.requested_amount)]
    if not plan_is_safe(fc, payments):
        return None
    return Candidate(method="wait", status="affordable_later",
                     payments=payments, earliest_full=earliest)


def _partial_payment(req: Request, prof: Profile, fc: Forecast, safe: Decimal,
                     earliest: Optional[date]) -> Optional[Candidate]:
    if not (req.allows_partial_payment and prof.accepts("partial_payment")):
        return None
    if not Decimal(0) < safe < req.requested_amount:
        return None
    if earliest is None or earliest > req.desired_completion_date:
        return None
    remainder = req.requested_amount - safe
    payments = [Payment(day=req.request_date, amount=safe),
                Payment(day=earliest, amount=remainder)]
    if sum((p.amount for p in payments), Decimal(0)) != req.requested_amount:
        return None
    if not plan_is_safe(fc, payments):
        return None
    return Candidate(method="partial_payment", status="affordable_with_plan",
                     payments=payments, earliest_full=earliest)


def _installments(req: Request, prof: Profile, fc: Forecast, options: list[PaymentOption],
                  earliest: Optional[date]) -> list[Candidate]:
    if not prof.accepts("installments") or prof.max_installment_months is None:
        return []
    out: list[Candidate] = []
    ceiling = Decimal(prof.max_installment_months)
    for opt in sorted(options, key=lambda o: o.payment_option_id):
        if opt.payment_method != "installments":
            continue
        if installment_months(opt) > ceiling:
            continue
        payments = installment_payments(opt)
        if not payments or not plan_is_safe(fc, payments):
            continue
        out.append(Candidate(method="installments", status="affordable_with_plan",
                             payments=payments, option_id=opt.payment_option_id,
                             earliest_full=earliest))
    return out


def build_candidates(req: Request, ds, fc: Forecast, prof: Profile) -> list[Candidate]:
    """Every eligible and safe plan for this request, unranked.

    earliest_full is carried identically on every candidate because the output
    contract defines earliest_date_for_full_payment as a measure of financial
    capacity, independent of which method the user ends up being offered.
    """
    safe = amount_safe_today(fc, req.requested_amount)
    earliest = earliest_full_payment(fc, req.requested_amount)

    candidates: list[Candidate] = []
    for maybe in (_full_payment(req, prof, fc, earliest),
                  _wait(req, prof, fc, earliest),
                  _partial_payment(req, prof, fc, safe, earliest)):
        if maybe is not None:
            candidates.append(maybe)
    candidates += _installments(req, prof, fc, ds.options_for(req.request_id), earliest)
    return candidates
