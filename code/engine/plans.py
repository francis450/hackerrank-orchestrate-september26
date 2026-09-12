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
from engine.spending_changes import find_spending_changes
from ingestion.records import PaymentOption, Profile, Request
from output.contract import Payment

DAYS_PER_MONTH = Decimal(30)

#: Rejection codes meaning "this partial plan was fully constructed and would
#: have been offered, but the completion date defeated it". These are the only
#: reasons that justify the 'cannot be completed safely' explanation variant.
PARTIAL_BLOCKED_BY_COMPLETION = frozenset({"earliest_missing", "earliest_after_deadline"})


@dataclass(frozen=True)
class Rejection:
    """Why one candidate was not offered. Recorded so downstream layers read a
    fact instead of re-deriving a guess from correlated fields."""
    method: str
    reason: str
    detail: str = ""
    option_id: str = ""


@dataclass
class CandidateSet:
    """Everything build_candidates concluded: what survived, and why the rest did not."""
    candidates: list["Candidate"] = field(default_factory=list)
    rejected_reasons: list[Rejection] = field(default_factory=list)

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def partial_blocked_by_completion(self) -> bool:
        """True when a partial plan was constructed and lost only to the deadline."""
        return any(r.method == "partial_payment" and r.reason in PARTIAL_BLOCKED_BY_COMPLETION
                   for r in self.rejected_reasons)


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


def _full_payment(req: Request, ds, prof: Profile, fc: Forecast, earliest: Optional[date],
                  rejected: list[Rejection]) -> Optional[Candidate]:
    if not prof.accepts("full_payment"):
        rejected.append(Rejection("full_payment", "method_not_accepted"))
        return None
    payments = [Payment(day=req.request_date, amount=req.requested_amount)]
    if plan_is_safe(fc, payments):
        return Candidate(method="full_payment", status="affordable_now",
                         payments=payments, earliest_full=earliest)
    # Eligible but unsafe: permitted spending changes may still close the gap.
    found = find_spending_changes(req, ds, fc, prof)
    if found is None:
        rejected.append(Rejection("full_payment", "unsafe_on_request_date"))
        return None
    tokens, _ = found
    return Candidate(method="full_payment", status="affordable_with_plan",
                     payments=payments, spending_changes=tokens, earliest_full=earliest)


def _wait(req: Request, prof: Profile, fc: Forecast, earliest: Optional[date],
          rejected: list[Rejection]) -> Optional[Candidate]:
    """Waiting is still a full payment, so it needs full_payment to be acceptable."""
    if not prof.accepts("full_payment"):
        rejected.append(Rejection("wait", "method_not_accepted"))
        return None
    if earliest is None:
        rejected.append(Rejection("wait", "earliest_missing"))
        return None
    if earliest <= req.request_date:
        rejected.append(Rejection("wait", "already_safe_today"))
        return None
    payments = [Payment(day=earliest, amount=req.requested_amount)]
    if not plan_is_safe(fc, payments):
        rejected.append(Rejection("wait", "unsafe_plan"))
        return None
    return Candidate(method="wait", status="affordable_later",
                     payments=payments, earliest_full=earliest)


def _partial_payment(req: Request, prof: Profile, fc: Forecast, safe: Decimal,
                     earliest: Optional[date], rejected: list[Rejection]) -> Optional[Candidate]:
    """Construction gates first, then the completion gates.

    The order matters to the explanation layer: only a plan that cleared every
    construction gate and then failed on the completion date counts as "the full
    amount cannot be completed safely".
    """
    if not req.allows_partial_payment:
        rejected.append(Rejection("partial_payment", "request_disallows_partial"))
        return None
    if not prof.accepts("partial_payment"):
        rejected.append(Rejection("partial_payment", "method_not_accepted"))
        return None
    if not Decimal(0) < safe < req.requested_amount:
        rejected.append(Rejection("partial_payment", "safe_not_strictly_between",
                                  f"safe={safe} requested={req.requested_amount}"))
        return None
    # Constructed: the user would take this plan if a completion date existed.
    if earliest is None:
        rejected.append(Rejection("partial_payment", "earliest_missing"))
        return None
    if earliest > req.desired_completion_date:
        rejected.append(Rejection("partial_payment", "earliest_after_deadline",
                                  f"earliest={earliest} due={req.desired_completion_date}"))
        return None
    remainder = req.requested_amount - safe
    payments = [Payment(day=req.request_date, amount=safe),
                Payment(day=earliest, amount=remainder)]
    if sum((p.amount for p in payments), Decimal(0)) != req.requested_amount:
        rejected.append(Rejection("partial_payment", "payments_do_not_sum"))
        return None
    if not plan_is_safe(fc, payments):
        rejected.append(Rejection("partial_payment", "unsafe_plan"))
        return None
    return Candidate(method="partial_payment", status="affordable_with_plan",
                     payments=payments, earliest_full=earliest)


def _installments(req: Request, prof: Profile, fc: Forecast, options: list[PaymentOption],
                  earliest: Optional[date], rejected: list[Rejection]) -> list[Candidate]:
    offers = [o for o in sorted(options, key=lambda o: o.payment_option_id)
              if o.payment_method == "installments"]
    if not prof.accepts("installments"):
        rejected += [Rejection("installments", "method_not_accepted", option_id=o.payment_option_id)
                     for o in offers]
        return []
    if prof.max_installment_months is None:
        rejected += [Rejection("installments", "no_installment_ceiling",
                               option_id=o.payment_option_id) for o in offers]
        return []
    out: list[Candidate] = []
    ceiling = Decimal(prof.max_installment_months)
    for opt in offers:
        months = installment_months(opt)
        if months > ceiling:
            rejected.append(Rejection("installments", "exceeds_max_installment_months",
                                      f"{months} > {ceiling}", opt.payment_option_id))
            continue
        payments = installment_payments(opt)
        if not payments:
            rejected.append(Rejection("installments", "empty_schedule", "", opt.payment_option_id))
            continue
        if not plan_is_safe(fc, payments):
            rejected.append(Rejection("installments", "unsafe_plan", "", opt.payment_option_id))
            continue
        out.append(Candidate(method="installments", status="affordable_with_plan",
                             payments=payments, option_id=opt.payment_option_id,
                             earliest_full=earliest))
    return out


def build_candidates(req: Request, ds, fc: Forecast, prof: Profile) -> CandidateSet:
    """Every eligible and safe plan for this request, unranked, plus why the rest failed.

    earliest_full is carried identically on every candidate because the output
    contract defines earliest_date_for_full_payment as a measure of financial
    capacity, independent of which method the user ends up being offered.
    """
    safe = amount_safe_today(fc, req.requested_amount)
    earliest = earliest_full_payment(fc, req.requested_amount)
    rejected: list[Rejection] = []

    candidates: list[Candidate] = []
    for maybe in (_full_payment(req, ds, prof, fc, earliest, rejected),
                  _wait(req, prof, fc, earliest, rejected),
                  _partial_payment(req, prof, fc, safe, earliest, rejected)):
        if maybe is not None:
            candidates.append(maybe)
    candidates += _installments(req, prof, fc, ds.options_for(req.request_id),
                                earliest, rejected)
    return CandidateSet(candidates=candidates, rejected_reasons=rejected)
