"""Typed records for every participant-facing dataset file.

Split out of loader.py so that module stays within the line budget once the
FX and join helpers are included.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "dataset"

#: Cash states that represent money that has actually moved or is committed to move.
SETTLED = "settled"
PENDING = "pending"
SCHEDULED = "scheduled"


@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass(frozen=True)
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: list[str]
    expense_categories_to_protect: list[str]
    expense_categories_user_is_willing_to_reduce: list[str]
    expense_categories_user_is_willing_to_stop: list[str]
    payment_methods_user_will_consider: list[str]
    max_installment_months: Optional[int]

    def accepts(self, method: str) -> bool:
        return method in self.payment_methods_user_will_consider


@dataclass(frozen=True)
class Event:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Optional[Decimal]
    currency: str
    event_date: Optional[date]
    settlement_date: Optional[date]
    status: str
    linked_event_id: str
    flexibility: str
    minimum_allowed_amount: Optional[Decimal]

    @property
    def is_debit(self) -> bool:
        return self.direction == "debit"

    @property
    def can_stop(self) -> bool:
        """flexibility vocabulary is fixed | reducible | stoppable | reducible_or_stoppable."""
        return self.flexibility in ("stoppable", "reducible_or_stoppable")

    @property
    def can_reduce(self) -> bool:
        return self.flexibility in ("reducible", "reducible_or_stoppable")

    @property
    def is_flexible(self) -> bool:
        return self.can_stop or self.can_reduce

    @property
    def needs_image_amount(self) -> bool:
        """A blank amount must be resolved from an image, never treated as zero."""
        return self.amount is None


@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]
    financing_fee: Decimal
    total_payable_amount: Decimal


@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: str
    request_id: str
    related_event_id: str
    sent_at: str
    source_type: str
    message_text: str


@dataclass(frozen=True)
class ImageRef:
    image_id: str
    user_id: str
    request_id: str
    related_event_id: str

    @property
    def path(self) -> Path:
        return DATASET_DIR / "media" / "images" / f"{self.image_id}.png"

    def exists(self) -> bool:
        """Never invent evidence when the file is absent."""
        return self.path.is_file()


@dataclass
class RequestContext:
    """Everything joined to one request_id, ready for state reconstruction."""
    request: Request
    profile: Profile
    events: list[Event]
    options: list[PaymentOption]
    messages: list[Message]
    images: list[ImageRef]

    @property
    def currency(self) -> str:
        return self.profile.home_currency

    def installment_options(self) -> list[PaymentOption]:
        return [o for o in self.options if o.payment_method == "installments"]

    def messages_for_event(self, event_id: str) -> list[Message]:
        return [m for m in self.messages if m.related_event_id == event_id]

    def images_for_event(self, event_id: str) -> list[ImageRef]:
        return [i for i in self.images if i.related_event_id == event_id]
