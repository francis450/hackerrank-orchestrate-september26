"""The output.csv contract: column order, the Decision record, and CSV serialisation.

Kept separate from validate.py so both stay inside the 200-line budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

COLUMNS: tuple[str, ...] = (
    "request_id", "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
    "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed",
    "decision_explanation",
)

STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
HORIZON_DAYS = 90


def money(value: Decimal) -> str:
    """Plain decimal, never scientific notation: 100 -> '100', 620.4 -> '620.40'."""
    quant = Decimal(value).quantize(Decimal("0.01"))
    if quant == quant.to_integral_value():
        return str(quant.to_integral_value())
    return f"{quant:f}"


@dataclass
class Payment:
    day: date
    amount: Decimal

    def render(self) -> str:
        return f"{self.day.isoformat()}:{money(self.amount)}"


@dataclass
class Decision:
    request_id: str
    amount_safe_to_pay: Decimal
    affordability_status: str
    recommended_payment_method: str
    payment_plan: list[Payment] = field(default_factory=list)
    earliest_date_for_full_payment: Optional[date] = None
    spending_changes_needed: list[str] = field(default_factory=list)
    decision_explanation: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "amount_safe_to_pay": money(self.amount_safe_to_pay),
            "affordability_status": self.affordability_status,
            "recommended_payment_method": self.recommended_payment_method,
            "payment_plan": "|".join(p.render() for p in self.payment_plan) or "none",
            "earliest_date_for_full_payment": (
                self.earliest_date_for_full_payment.isoformat()
                if self.earliest_date_for_full_payment else ""),
            "spending_changes_needed": "|".join(self.spending_changes_needed) or "none",
            "decision_explanation": self.decision_explanation,
        }
