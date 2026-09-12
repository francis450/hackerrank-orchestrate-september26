"""Mechanical validator for the output contract.

Implements the rules derived from problem_statement.md ("Allowed values",
"Choosing Between Safe Plans") and AGENTS.md 6.2/6.3. validate_decision()
returns a list of human-readable violations; an empty list means the row is
schema-valid. The record types themselves live in output.contract.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from typing import Optional, Sequence

from ingestion.loader import Dataset, Profile, Request
from output.contract import (COLUMNS, HORIZON_DAYS, METHODS, STATUSES, Decision,
                             Payment, money)

_PAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d+(?:\.\d+)?)$")
_STOP_RE = re.compile(r"^stop:([A-Za-z0-9_]+)$")
_REDUCE_RE = re.compile(r"^reduce_to:([A-Za-z0-9_]+):(\d+(?:\.\d+)?)$")


def validate_decision(dec: Decision, req: Request, ds: Optional[Dataset] = None) -> list[str]:
    """Return every mechanical contract violation for one decision."""
    bad: list[str] = []
    row = dec.as_row()
    plan = dec.payment_plan
    total = sum((p.amount for p in plan), Decimal(0))
    prof: Optional[Profile] = ds.profile_for(req) if ds else None

    if dec.request_id != req.request_id:
        bad.append(f"request_id mismatch {dec.request_id} != {req.request_id}")
    if dec.affordability_status not in STATUSES:
        bad.append(f"bad affordability_status {dec.affordability_status!r}")
    if dec.recommended_payment_method not in METHODS:
        bad.append(f"bad recommended_payment_method {dec.recommended_payment_method!r}")
    if not dec.decision_explanation.strip():
        bad.append("empty decision_explanation")
    if not Decimal(0) <= dec.amount_safe_to_pay <= req.requested_amount:
        bad.append(f"amount_safe_to_pay {dec.amount_safe_to_pay} outside [0, {req.requested_amount}]")
    if row["payment_plan"] != "none":
        for token in row["payment_plan"].split("|"):
            if not _PAY_RE.match(token):
                bad.append(f"malformed payment token {token!r}")
    if any(b.day < a.day for a, b in zip(plan, plan[1:])):
        bad.append("payment_plan not in chronological order")
    if any(p.amount <= 0 for p in plan):
        bad.append("payment_plan contains a non-positive amount")
    if any(p.day < req.request_date for p in plan):
        bad.append("payment_plan contains a date before request_date")
    if plan and plan[-1].day > req.desired_completion_date:
        bad.append("payment_plan finishes after desired_completion_date")

    bad += _validate_changes(dec, req, ds)
    bad += _validate_dates(dec, req)
    bad += _validate_coupling(dec, req, plan, total)
    if prof is not None:
        bad += _validate_preferences(dec, req, plan, prof, ds)
    return bad


def _validate_dates(dec: Decision, req: Request) -> list[str]:
    bad: list[str] = []
    earliest = dec.earliest_date_for_full_payment
    if earliest is not None:
        if earliest < req.request_date:
            bad.append("earliest_date_for_full_payment before request_date")
        if earliest > req.request_date + timedelta(days=HORIZON_DAYS):
            bad.append("earliest_date_for_full_payment beyond the 90-day horizon")
    if dec.affordability_status == "affordable_now" and earliest != req.request_date:
        bad.append("affordable_now requires earliest_date_for_full_payment == request_date")
    if (earliest is None) != (dec.affordability_status == "not_affordable"):
        bad.append("empty earliest_date_for_full_payment must coincide with not_affordable")
    return bad


def _validate_coupling(dec: Decision, req: Request, plan: Sequence[Payment],
                       total: Decimal) -> list[str]:
    bad: list[str] = []
    status, method = dec.affordability_status, dec.recommended_payment_method
    if (status == "not_affordable") != (method == "not_recommended"):
        bad.append("not_affordable and not_recommended must coincide")
    if method == "not_recommended":
        if plan or dec.earliest_date_for_full_payment or dec.spending_changes_needed:
            bad.append("not_recommended requires plan=none, empty earliest, changes=none")
    elif method == "full_payment":
        if len(plan) != 1 or plan[0].day != req.request_date or plan[0].amount != req.requested_amount:
            bad.append("full_payment requires one payment of requested_amount on request_date")
        expected = "affordable_with_plan" if dec.spending_changes_needed else "affordable_now"
        if status != expected:
            bad.append(f"full_payment with changes={bool(dec.spending_changes_needed)} requires {expected}")
    elif method == "wait":
        if status != "affordable_later":
            bad.append("wait requires affordable_later")
        if len(plan) != 1 or plan[0].amount != req.requested_amount:
            bad.append("wait requires one payment of the full requested_amount")
        elif plan[0].day != dec.earliest_date_for_full_payment or plan[0].day <= req.request_date:
            bad.append("wait payment must fall on earliest_date_for_full_payment, after request_date")
        if dec.spending_changes_needed:
            bad.append("wait must not require spending changes")
    elif method == "partial_payment":
        if status != "affordable_with_plan":
            bad.append("partial_payment requires affordable_with_plan")
        if not req.allows_partial_payment:
            bad.append("partial_payment on a request that disallows it")
        if not Decimal(0) < dec.amount_safe_to_pay < req.requested_amount:
            bad.append("partial_payment requires 0 < amount_safe_to_pay < requested_amount")
        if len(plan) != 2:
            bad.append("partial_payment requires exactly two payments")
        else:
            if plan[0].day != req.request_date or plan[0].amount != dec.amount_safe_to_pay:
                bad.append("first partial payment must be amount_safe_to_pay on request_date")
            if plan[1].day != dec.earliest_date_for_full_payment:
                bad.append("second partial payment must fall on earliest_date_for_full_payment")
            if total != req.requested_amount:
                bad.append(f"partial payments sum to {total}, not {req.requested_amount}")
        if dec.earliest_date_for_full_payment and dec.earliest_date_for_full_payment > req.desired_completion_date:
            bad.append("partial_payment completion date is past desired_completion_date")
    elif method == "installments":
        if status != "affordable_with_plan":
            bad.append("installments requires affordable_with_plan")
    return bad


def _validate_changes(dec: Decision, req: Request, ds: Optional[Dataset]) -> list[str]:
    bad: list[str] = []
    changes = dec.spending_changes_needed
    if len(changes) > 3:
        bad.append(f"{len(changes)} spending changes exceeds the maximum of 3")
    seen: set[str] = set()
    for token in changes:
        stop, reduce = _STOP_RE.match(token), _REDUCE_RE.match(token)
        if not stop and not reduce:
            bad.append(f"malformed spending change {token!r}")
            continue
        event_id = (stop or reduce).group(1)
        if event_id in seen:
            bad.append(f"event {event_id} referenced by more than one spending change")
        seen.add(event_id)
        if ds is None:
            continue
        ev = ds.events_by_id.get(event_id)
        prof = ds.profile_for(req)
        if ev is None or ev.user_id != req.user_id:
            bad.append(f"spending change references unknown or foreign event {event_id}")
            continue
        if stop and not ev.can_stop:
            bad.append(f"event {event_id} is {ev.flexibility}, not stoppable")
        if reduce and not ev.can_reduce:
            bad.append(f"event {event_id} is {ev.flexibility}, not reducible")
        if ev.category in prof.expense_categories_to_protect:
            bad.append(f"event {event_id} is in protected category {ev.category}")
        if stop and ev.category not in prof.expense_categories_user_is_willing_to_stop:
            bad.append(f"user will not stop category {ev.category}")
        if reduce:
            if ev.category not in prof.expense_categories_user_is_willing_to_reduce:
                bad.append(f"user will not reduce category {ev.category}")
            new_amount = Decimal(reduce.group(2))
            if ev.minimum_allowed_amount is not None and new_amount < ev.minimum_allowed_amount:
                bad.append(f"reduce_to {new_amount} is below minimum_allowed_amount for {event_id}")
            if ev.amount is not None and new_amount >= ev.amount:
                bad.append(f"reduce_to {new_amount} is not a reduction of {ev.amount}")
    if dec.spending_changes_needed and dec.affordability_status != "affordable_with_plan":
        bad.append("spending changes require affordable_with_plan")
    return bad


def _validate_preferences(dec: Decision, req: Request, plan: Sequence[Payment],
                          prof: Profile, ds: Optional[Dataset]) -> list[str]:
    bad: list[str] = []
    method = dec.recommended_payment_method
    allowed = prof.payment_methods_user_will_consider
    if method in {"full_payment", "partial_payment", "installments"} and method not in allowed:
        bad.append(f"{method} is not in payment_methods_user_will_consider")
    if method == "wait" and "full_payment" not in allowed:
        bad.append("wait requires the user to accept full_payment")
    if method != "installments":
        return bad
    if prof.max_installment_months is None:
        bad.append("installments recommended but max_installment_months is blank")
    elif len(plan) > prof.max_installment_months:
        bad.append(f"{len(plan)} installments exceeds max_installment_months={prof.max_installment_months}")
    if ds is None:
        return bad
    if not any(_matches_option(plan, opt) for opt in ds.options_for(req.request_id)
               if opt.payment_method == "installments"):
        bad.append("installment plan does not match any supplied payment option")
    return bad


def _matches_option(plan: Sequence[Payment], opt) -> bool:
    if len(plan) != opt.number_of_payments:
        return False
    step = timedelta(days=opt.payment_frequency_days or 0)
    return all(p.amount == opt.payment_amount and p.day == opt.first_payment_date + step * i
               for i, p in enumerate(plan))
