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
from output.explanation import not_recommended_incomplete, not_recommended_no_option
from output.contract import COLUMNS, Decision
from state.reconstruct import build_state


def solve(req: Request, ds: Dataset) -> Decision:
    """Produce one decision for one request. Pure and deterministic."""
    prof = ds.profile_for(req)
    state = build_state(req, ds)
    forecast = build_forecast(state)
    amount_safe = amount_safe_today(forecast, req.requested_amount)
    earliest = earliest_full_payment(forecast, req.requested_amount)

    candidates = build_candidates(req, ds, forecast, prof, amount_safe, earliest)
    winner = best_candidate(candidates, req)

    if winner is None:
        explanation = (not_recommended_incomplete(req, prof, amount_safe)
                       if amount_safe > Decimal(0)
                       else not_recommended_no_option(req, prof))
        return Decision(
            request_id=req.request_id,
            amount_safe_to_pay=amount_safe,
            affordability_status="not_affordable",
            recommended_payment_method="not_recommended",
            payment_plan=[],
            earliest_date_for_full_payment=None,
            spending_changes_needed=[],
            decision_explanation=explanation,
        )

    return Decision(
        request_id=req.request_id,
        amount_safe_to_pay=amount_safe,
        affordability_status=winner.status,
        recommended_payment_method=winner.method,
        payment_plan=list(winner.payments),
        earliest_date_for_full_payment=winner.earliest_full,
        spending_changes_needed=list(winner.spending_changes),
        decision_explanation="",
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
