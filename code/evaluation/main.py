"""Backtest the pipeline against the 25 golden rows in dataset/sample_requests.csv.

    python code/evaluation/main.py [--requests sample_requests.csv] [--quiet-table]

Reports per-field accuracy over all seven predicted fields, median/mean absolute
percentage error for amount_safe_to_pay, and a per-row expected-vs-predicted table.
Exits non-zero when any produced row violates the output schema, so this doubles
as the pre-submission gate.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from facts.store import FactStore
from ingestion.loader import DATASET_DIR, Dataset, Request, load_dataset, load_env
from main import solve
from output.contract import COLUMNS, Decision
from output.validate import validate_decision

SCORED_FIELDS: tuple[str, ...] = COLUMNS[1:]


def _num(raw: str) -> Optional[Decimal]:
    try:
        return Decimal((raw or "").strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def canonical(field: str, raw: str) -> str:
    """Normalise a field so formatting differences do not count as errors."""
    raw = (raw or "").strip()
    if field == "amount_safe_to_pay":
        value = _num(raw)
        return "" if value is None else str(value.quantize(Decimal("0.01")))
    if field == "payment_plan":
        if raw.lower() in {"", "none"}:
            return "none"
        parts = []
        for token in raw.split("|"):
            day, _, amount = token.partition(":")
            value = _num(amount)
            parts.append(f"{day.strip()}:{value.quantize(Decimal('0.01')) if value else amount}")
        return "|".join(parts)
    if field == "spending_changes_needed":
        if raw.lower() in {"", "none"}:
            return "none"
        parts = []
        for token in raw.split("|"):
            bits = token.split(":")
            if len(bits) == 3:
                value = _num(bits[2])
                bits[2] = str(value.quantize(Decimal("0.01"))) if value is not None else bits[2]
            parts.append(":".join(b.strip() for b in bits))
        return "|".join(parts)
    if field == "decision_explanation":
        return " ".join(raw.lower().split())
    return raw


def load_golden(requests_file: str) -> dict[str, dict[str, str]]:
    path = DATASET_DIR / requests_file
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    missing = [f for f in SCORED_FIELDS if f not in (rows[0] if rows else {})]
    if missing:
        raise SystemExit(f"{path} has no golden columns: missing {missing}")
    return {r["request_id"]: r for r in rows}


def ape(expected: Optional[Decimal], predicted: Optional[Decimal]) -> Optional[float]:
    """Absolute percentage error, with a defined answer at expected == 0."""
    if expected is None or predicted is None:
        return None
    if expected == 0:
        return 0.0 if predicted == 0 else 100.0
    return float(abs(predicted - expected) / abs(expected) * 100)


def print_row_table(rows: list[dict[str, object]]) -> None:
    head = f"{'request_id':<12} {'expected':>16} {'predicted':>16} {'abs err':>14} {'APE %':>8}  ok"
    print(head)
    print("-" * len(head))
    for r in rows:
        err = "-" if r["abs_err"] is None else f"{r['abs_err']:,.2f}"
        pct = "-" if r["ape"] is None else f"{r['ape']:.2f}"
        print(f"{r['request_id']:<12} {r['expected']:>16} {r['predicted']:>16} "
              f"{err:>14} {pct:>8}  {'Y' if r['exact'] else 'n'}")


def print_field_accuracy(hits: dict[str, int], total: int, similarity: list[float]) -> None:
    width = max(len(f) for f in SCORED_FIELDS)
    print(f"{'field':<{width}} {'exact':>8} {'accuracy':>10}")
    print("-" * (width + 20))
    for f in SCORED_FIELDS:
        print(f"{f:<{width}} {hits[f]:>4}/{total:<3} {hits[f] / total * 100:>9.1f}%")
    if similarity:
        print(f"\ndecision_explanation mean text similarity: "
              f"{statistics.mean(similarity) * 100:.1f}%")


def evaluate(requests_file: str, show_table: bool) -> int:
    load_env()
    ds: Dataset = load_dataset(requests_file=requests_file)
    facts = FactStore.load().index_by_user(ds)
    golden = load_golden(requests_file)

    hits = {f: 0 for f in SCORED_FIELDS}
    similarity: list[float] = []
    table: list[dict[str, object]] = []
    apes: list[float] = []
    violations: list[tuple[str, list[str]]] = []
    errors: list[tuple[str, str]] = []

    for req in ds.requests:
        gold = golden.get(req.request_id)
        if gold is None:
            errors.append((req.request_id, "no golden row"))
            continue
        try:
            dec: Decision = solve(req, ds, facts)
        except Exception as exc:  # noqa: BLE001 - a crash is a reportable result
            errors.append((req.request_id, f"{type(exc).__name__}: {exc}"))
            continue

        problems = validate_decision(dec, req, ds)
        if problems:
            violations.append((req.request_id, problems))

        row = dec.as_row()
        for f in SCORED_FIELDS:
            want, got = canonical(f, gold[f]), canonical(f, row[f])
            if want == got:
                hits[f] += 1
            if f == "decision_explanation":
                similarity.append(SequenceMatcher(None, want, got).ratio())

        want_amt, got_amt = _num(gold["amount_safe_to_pay"]), _num(row["amount_safe_to_pay"])
        pct = ape(want_amt, got_amt)
        if pct is not None:
            apes.append(pct)
        table.append({
            "request_id": req.request_id,
            "expected": f"{want_amt:,.2f}" if want_amt is not None else "-",
            "predicted": f"{got_amt:,.2f}" if got_amt is not None else "-",
            "abs_err": float(abs(got_amt - want_amt)) if None not in (want_amt, got_amt) else None,
            "ape": pct,
            "exact": canonical("amount_safe_to_pay", gold["amount_safe_to_pay"])
            == canonical("amount_safe_to_pay", row["amount_safe_to_pay"]),
        })

    total = len(table)
    if not total:
        print("no rows evaluated", file=sys.stderr)
        return 2

    print(f"\n=== amount_safe_to_pay: expected vs predicted ({total} rows) ===\n")
    if show_table:
        print_row_table(table)
    print(f"\n  median APE: {statistics.median(apes):.2f}%")
    print(f"  mean APE:   {statistics.mean(apes):.2f}%")
    print(f"  exact:      {hits['amount_safe_to_pay']}/{total}")

    print(f"\n=== per-field accuracy ({total} rows) ===\n")
    print_field_accuracy(hits, total, similarity)

    print("\n=== schema validation ===\n")
    if violations:
        for request_id, problems in violations:
            for p in problems:
                print(f"  INVALID {request_id}: {p}")
        print(f"\n  {len(violations)}/{total} rows violate the output schema")
    else:
        print(f"  all {total} rows are schema-valid")

    if errors:
        print("\n=== pipeline errors ===\n")
        for request_id, msg in errors:
            print(f"  ERROR {request_id}: {msg}")

    return 1 if (violations or errors) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest against the golden sample rows")
    parser.add_argument("--requests", default="sample_requests.csv",
                        help="filename inside dataset/ carrying golden columns")
    parser.add_argument("--quiet-table", action="store_true",
                        help="suppress the per-row amount_safe_to_pay table")
    args = parser.parse_args()
    return evaluate(args.requests, show_table=not args.quiet_table)


if __name__ == "__main__":
    raise SystemExit(main())
