"""Phase 4 forecast-accuracy grid.

    python code/evaluation/grid.py [--sort medape] [--top N]

Sweeps the reconstruction parameters against the 25 golden rows and reports,
per combination: amount_safe_to_pay exact count / median APE / mean APE,
earliest-date exact count, and the full seven-field exact totals.

Income aggregation is pinned to "last" and income_cv_limit to 0.25 (D005).
Nothing here writes a default - it only measures.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import statistics
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.forecast import build_forecast
from engine.plans import build_candidates
from engine.rank import best_candidate
from evaluation.main import SCORED_FIELDS, canonical
from ingestion.loader import DATASET_DIR, load_dataset
from output.contract import Decision
from output.explanation import render
from state.reconstruct import build_state

INCOME_AGGREGATION = "last"
INCOME_CV_LIMIT = 0.25
AGGREGATIONS = ("mean", "median", "last", "p75")
SPLITS = ("uniform", "interval_p75_monthly_last")


@dataclass
class Combo:
    aggregation: str
    include_request_day: bool
    min_interval_occurrences: int
    split: str

    def kwargs(self) -> dict:
        out = dict(aggregation=self.aggregation,
                   income_aggregation=INCOME_AGGREGATION,
                   income_cv_limit=INCOME_CV_LIMIT,
                   include_request_day=self.include_request_day,
                   min_interval_occurrences=self.min_interval_occurrences)
        if self.split == "interval_p75_monthly_last":
            out.update(monthly_rule="last", interval_rule="p75")
        return out

    def label(self) -> str:
        agg = "-" if self.split != "uniform" else self.aggregation
        return (f"{agg:<7} rqday={str(self.include_request_day):<5} "
                f"minint={self.min_interval_occurrences} {self.split:<26}")


@dataclass
class Score:
    combo: Combo
    exact_amount: int = 0
    median_ape: float = 0.0
    mean_ape: float = 0.0
    exact_earliest: int = 0
    fields: dict[str, int] = field(default_factory=dict)

    @property
    def field_total(self) -> int:
        return sum(self.fields.values())


def _num(raw: str) -> Optional[Decimal]:
    try:
        return Decimal((raw or "").strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def solve_with(req, ds, kwargs) -> Decision:
    """Run the whole pipeline under one parameter combination."""
    prof = ds.profile_for(req)
    fc = build_forecast(build_state(req, ds, **kwargs))
    from engine.forecast import amount_safe_today, earliest_full_payment
    safe = amount_safe_today(fc, req.requested_amount)
    earliest = earliest_full_payment(fc, req.requested_amount)
    offered = build_candidates(req, ds, fc, prof)
    winner = best_candidate(offered.candidates, req)
    if winner is None:
        status, method, payments, changes = "not_affordable", "not_recommended", [], []
    else:
        status, method = winner.status, winner.method
        payments, changes = list(winner.payments), list(winner.spending_changes)
    return Decision(
        request_id=req.request_id, amount_safe_to_pay=safe, affordability_status=status,
        recommended_payment_method=method, payment_plan=payments,
        earliest_date_for_full_payment=earliest, spending_changes_needed=changes,
        decision_explanation=render(req, prof, ds, status, method, payments, changes,
                                    earliest, safe,
                                    partial_blocked=offered.partial_blocked_by_completion()),
    )


def score_combo(combo: Combo, ds, gold) -> Score:
    kwargs = combo.kwargs()
    hits = {f: 0 for f in SCORED_FIELDS}
    apes: list[float] = []
    for req in ds.requests:
        g = gold[req.request_id]
        row = solve_with(req, ds, kwargs).as_row()
        for f in SCORED_FIELDS:
            if canonical(f, g[f]) == canonical(f, row[f]):
                hits[f] += 1
        want, got = _num(g["amount_safe_to_pay"]), _num(row["amount_safe_to_pay"])
        if want and got is not None:
            apes.append(float(abs(got - want) / abs(want) * 100))
    return Score(combo=combo, exact_amount=hits["amount_safe_to_pay"],
                 median_ape=statistics.median(apes), mean_ape=statistics.fmean(apes),
                 exact_earliest=hits["earliest_date_for_full_payment"], fields=hits)


def combos() -> list[Combo]:
    """Uniform splits sweep the aggregation axis; the mixed split overrides it."""
    out: list[Combo] = []
    for rqday, minint in itertools.product((False, True), (2, 3)):
        for agg in AGGREGATIONS:
            out.append(Combo(agg, rqday, minint, "uniform"))
        out.append(Combo("mean", rqday, minint, "interval_p75_monthly_last"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 forecast parameter grid")
    parser.add_argument("--sort", default="medape",
                        choices=("medape", "meanape", "fields", "amount", "earliest"))
    parser.add_argument("--top", type=int, default=0, help="limit rows printed")
    args = parser.parse_args()

    ds = load_dataset(requests_file="sample_requests.csv")
    with (DATASET_DIR / "sample_requests.csv").open(encoding="utf-8-sig", newline="") as fh:
        gold = {r["request_id"]: r for r in csv.DictReader(fh)}

    scores = [score_combo(c, ds, gold) for c in combos()]
    keys = {"medape": lambda s: (s.median_ape, -s.field_total),
            "meanape": lambda s: (s.mean_ape, -s.field_total),
            "fields": lambda s: (-s.field_total, s.median_ape),
            "amount": lambda s: (-s.exact_amount, s.median_ape),
            "earliest": lambda s: (-s.exact_earliest, s.median_ape)}
    scores.sort(key=keys[args.sort])
    if args.top:
        scores = scores[:args.top]

    short = ("amt", "status", "method", "plan", "earliest", "changes", "expl")
    head = (f"{'aggregation':<8}{'rqday':<11}{'minint':<8}{'split':<27}"
            f"{'medAPE':>8}{'meanAPE':>9}{'amt=':>6}{'early=':>7}{'tot7':>6}   "
            + " ".join(f"{s:>8}" for s in short))
    print(head)
    print("-" * len(head))
    for s in scores:
        cells = " ".join(f"{s.fields[f]:>8}" for f in SCORED_FIELDS)
        print(f"{s.combo.label()}{s.median_ape:>8.2f}{s.mean_ape:>9.2f}"
              f"{s.exact_amount:>6}{s.exact_earliest:>7}{s.field_total:>6}   {cells}")
    print(f"\n{len(scores)} combinations; income_aggregation={INCOME_AGGREGATION}, "
          f"income_cv_limit={INCOME_CV_LIMIT} (both fixed by D005). "
          f"Max per field 25, max tot7 = 175.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
