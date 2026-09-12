"""Buy or Wait? entry point.

Usage:
    python code/main.py [--requests requests.csv] [--out dataset/output.csv]

Reads dataset/ and writes the 8-column submission CSV. Secrets come from the
environment only (ANTHROPIC_API_KEY), never from arguments or source.
"""
from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.forecast import amount_safe_today, build_forecast, earliest_full_payment
from engine.plans import build_candidates
from engine.rank import best_candidate
from ingestion.loader import DATASET_DIR, Dataset, Request, load_dataset, load_env
from output.explanation import render
from output.contract import COLUMNS, Decision
from state.reconstruct import build_state


def solve(req: Request, ds: Dataset) -> Decision:
    """Produce one decision for one request. Pure and deterministic."""
    prof = ds.profile_for(req)
    state = build_state(req, ds)
    forecast = build_forecast(state)
    amount_safe = amount_safe_today(forecast, req.requested_amount)
    earliest = earliest_full_payment(forecast, req.requested_amount)

    candidates = build_candidates(req, ds, forecast, prof)
    winner = best_candidate(candidates, req)

    if winner is None:
        status, method = "not_affordable", "not_recommended"
        payments, changes = [], []
    else:
        status, method = winner.status, winner.method
        payments, changes = list(winner.payments), list(winner.spending_changes)

    # amount_safe_to_pay and earliest_date_for_full_payment measure financial
    # capacity, which the spec defines independently of the recommendation, so
    # both come from the forecast even when nothing is recommended.
    return Decision(
        request_id=req.request_id,
        amount_safe_to_pay=amount_safe,
        affordability_status=status,
        recommended_payment_method=method,
        payment_plan=payments,
        earliest_date_for_full_payment=earliest,
        spending_changes_needed=changes,
        decision_explanation=render(req, prof, ds, status, method, payments,
                                    changes, earliest, amount_safe),
    )


def run(requests_file: str = "requests.csv") -> tuple[Dataset, list[Decision]]:
    """Load the dataset and solve every request in it."""
    load_env()
    ds = load_dataset(requests_file=requests_file)
    return ds, [solve(req, ds) for req in ds.requests]


def write_output(decisions: list[Decision], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        for dec in decisions:
            writer.writerow(dec.as_row())


def main() -> int:
    parser = argparse.ArgumentParser(description="Buy or Wait? decision agent")
    parser.add_argument("--requests", default="requests.csv",
                        help="filename inside dataset/ to solve")
    parser.add_argument("--out", default=str(DATASET_DIR / "output.csv"),
                        help="path to write the submission CSV")
    args = parser.parse_args()

    ds, decisions = run(args.requests)
    write_output(decisions, Path(args.out))
    print(f"wrote {len(decisions)} decisions to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
