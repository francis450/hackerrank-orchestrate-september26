"""Load the participant-facing dataset in dataset/, convert currency, and join by request.

Record types live in ingestion.records. Nothing here reads organizer-only files,
and secrets are read from the environment only.
"""
from __future__ import annotations

import csv
import logging
import os
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ingestion.records import (DATASET_DIR, REPO_ROOT, Event, ImageRef, Message,
                               PaymentOption, Profile, Request, RequestContext)

log = logging.getLogger("buyorwait.loader")

__all__ = ["DATASET_DIR", "REPO_ROOT", "Dataset", "FxRateUnavailable", "Event", "ImageRef",
           "Message", "PaymentOption", "Profile", "Request", "RequestContext",
           "load_dataset", "load_env"]


class FxRateUnavailable(LookupError):
    """Raised when no rate exists for a currency pair, in either date direction."""


def _dec(raw: str) -> Optional[Decimal]:
    raw = (raw or "").strip().replace(",", "")
    return Decimal(raw) if raw else None


def _day(raw: str) -> Optional[date]:
    raw = (raw or "").strip()
    return date.fromisoformat(raw) if raw else None


def _int(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    return int(raw) if raw else None


def _pipe(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split("|") if p.strip()]


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass
class Dataset:
    requests: list[Request] = field(default_factory=list)
    profiles: dict[str, Profile] = field(default_factory=dict)
    events_by_user: dict[str, list[Event]] = field(default_factory=dict)
    events_by_id: dict[str, Event] = field(default_factory=dict)
    options_by_request: dict[str, list[PaymentOption]] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    rates: dict[tuple[str, str], dict[date, Decimal]] = field(default_factory=dict)
    fx_warnings: list[str] = field(default_factory=list)
    _rate_days: dict[tuple[str, str], list[date]] = field(default_factory=dict)
    _by_request_id: dict[str, Request] = field(default_factory=dict)

    def profile_for(self, req: Request) -> Profile:
        return self.profiles[req.user_id]

    def options_for(self, request_id: str) -> list[PaymentOption]:
        return self.options_by_request.get(request_id, [])

    def request(self, request_id: str) -> Request:
        return self._by_request_id[request_id]

    # ---- currency -------------------------------------------------------

    def fx_convert(self, amount: Decimal, from_ccy: str, to_ccy: str,
                   settlement_date: date) -> Decimal:
        """Convert amount using the rate row for settlement_date in the stated direction.

        AGENTS.md 6.1 pins conversion to the row matching the settlement date and the
        stated from -> to direction. When no exact row exists we fall back to the
        nearest strictly-prior date for the same pair and record a warning. We never
        invert a rate to manufacture a missing direction, and we never return the
        amount unconverted: with no usable row at all this raises FxRateUnavailable.
        """
        if from_ccy == to_ccy:
            return amount
        pair = (from_ccy, to_ccy)
        by_day = self.rates.get(pair)
        if not by_day:
            raise FxRateUnavailable(
                f"no {from_ccy}->{to_ccy} rate in exchange_rates.csv (rates are "
                f"directional; inverting a supplied pair would invent a fact)")
        exact = by_day.get(settlement_date)
        if exact is not None:
            return amount * exact
        days = self._rate_days[pair]
        idx = bisect_right(days, settlement_date)
        if idx == 0:
            raise FxRateUnavailable(
                f"no {from_ccy}->{to_ccy} rate on or before {settlement_date}; "
                f"earliest supplied is {days[0]}")
        prior = days[idx - 1]
        warning = (f"no exact {from_ccy}->{to_ccy} rate for {settlement_date}; "
                   f"fell back to nearest prior date {prior}")
        self.fx_warnings.append(warning)
        log.warning(warning)
        return amount * by_day[prior]

    def to_home(self, ev: Event, profile: Profile) -> Optional[Decimal]:
        """Event amount in the user's home currency, or None when the amount is blank."""
        if ev.amount is None:
            return None
        if ev.currency == profile.home_currency:
            return ev.amount
        when = ev.settlement_date or ev.event_date
        if when is None:
            raise FxRateUnavailable(f"event {ev.event_id} has no date to price FX against")
        return self.fx_convert(ev.amount, ev.currency, profile.home_currency, when)

    # ---- joins ----------------------------------------------------------

    def context_for(self, request_id: str) -> RequestContext:
        """Join a request to its profile, user events, options, messages and images.

        Messages and images attach either directly to the request or to the user with
        no request of their own; request-scoped records for *other* requests are excluded.
        """
        req = self.request(request_id)
        return RequestContext(
            request=req,
            profile=self.profiles[req.user_id],
            events=list(self.events_by_user.get(req.user_id, [])),
            options=sorted(self.options_for(request_id), key=lambda o: o.payment_option_id),
            messages=[m for m in self.messages if self._attaches(m.user_id, m.request_id, req)],
            images=[i for i in self.images if self._attaches(i.user_id, i.request_id, req)],
        )

    @staticmethod
    def _attaches(user_id: str, request_id: str, req: Request) -> bool:
        if request_id:
            return request_id == req.request_id
        return user_id == req.user_id


def load_dataset(dataset_dir: Path = DATASET_DIR, requests_file: str = "requests.csv") -> Dataset:
    ds = Dataset()
    for r in _rows(dataset_dir / requests_file):
        req = Request(
            request_id=r["request_id"], user_id=r["user_id"],
            request_date=_day(r["request_date"]), request_type=r["request_type"],
            requested_amount=_dec(r["requested_amount"]),
            desired_completion_date=_day(r["desired_completion_date"]),
            allows_partial_payment=r["allows_partial_payment"].strip().lower() == "true",
            request_text=r["request_text"],
        )
        ds.requests.append(req)
        ds._by_request_id[req.request_id] = req
    for r in _rows(dataset_dir / "financial_profiles.csv"):
        ds.profiles[r["user_id"]] = Profile(
            user_id=r["user_id"], home_currency=r["home_currency"],
            current_available_balance=_dec(r["current_available_balance"]),
            minimum_balance_to_keep=_dec(r["minimum_balance_to_keep"]),
            financial_priorities=_pipe(r["financial_priorities"]),
            expense_categories_to_protect=_pipe(r["expense_categories_to_protect"]),
            expense_categories_user_is_willing_to_reduce=_pipe(
                r["expense_categories_user_is_willing_to_reduce"]),
            expense_categories_user_is_willing_to_stop=_pipe(
                r["expense_categories_user_is_willing_to_stop"]),
            payment_methods_user_will_consider=_pipe(r["payment_methods_user_will_consider"]),
            max_installment_months=_int(r["max_installment_months"]),
        )
    for r in _rows(dataset_dir / "financial_events.csv"):
        ev = Event(
            event_id=r["event_id"], user_id=r["user_id"], event_type=r["event_type"],
            description=r["description"], category=r["category"], direction=r["direction"],
            amount=_dec(r["amount"]), currency=r["currency"], event_date=_day(r["event_date"]),
            settlement_date=_day(r["settlement_date"]), status=r["status"],
            linked_event_id=r["linked_event_id"].strip(), flexibility=r["flexibility"],
            minimum_allowed_amount=_dec(r["minimum_allowed_amount"]),
        )
        ds.events_by_user.setdefault(ev.user_id, []).append(ev)
        ds.events_by_id[ev.event_id] = ev
    for r in _rows(dataset_dir / "request_payment_options.csv"):
        opt = PaymentOption(
            payment_option_id=r["payment_option_id"], request_id=r["request_id"],
            payment_method=r["payment_method"], payment_amount=_dec(r["payment_amount"]),
            number_of_payments=_int(r["number_of_payments"]),
            first_payment_date=_day(r["first_payment_date"]),
            payment_frequency_days=_int(r["payment_frequency_days"]),
            financing_fee=_dec(r["financing_fee"]) or Decimal(0),
            total_payable_amount=_dec(r["total_payable_amount"]),
        )
        ds.options_by_request.setdefault(opt.request_id, []).append(opt)
    for r in _rows(dataset_dir / "messages.csv"):
        ds.messages.append(Message(
            message_id=r["message_id"], user_id=r["user_id"], request_id=r["request_id"].strip(),
            related_event_id=r["related_event_id"].strip(), sent_at=r["sent_at"],
            source_type=r["source_type"], message_text=r["message_text"]))
    for r in _rows(dataset_dir / "images.csv"):
        ds.images.append(ImageRef(
            image_id=r["image_id"], user_id=r["user_id"], request_id=r["request_id"].strip(),
            related_event_id=r["related_event_id"].strip()))
    for r in _rows(dataset_dir / "exchange_rates.csv"):
        pair = (r["from_currency"], r["to_currency"])
        ds.rates.setdefault(pair, {})[_day(r["rate_date"])] = _dec(r["rate"])
    for pair, by_day in ds.rates.items():
        ds._rate_days[pair] = sorted(by_day)
    for evs in ds.events_by_user.values():
        evs.sort(key=lambda e: (e.settlement_date or e.event_date or date.min, e.event_id))
    return ds


def load_env(env_path: Path = REPO_ROOT / ".env") -> None:
    """Populate os.environ from .env, tolerating UTF-16/BOM encodings. Never logs values."""
    if not env_path.exists():
        return
    raw = env_path.read_bytes()
    text = None
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
