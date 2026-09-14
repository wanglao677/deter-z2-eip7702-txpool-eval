#!/usr/bin/env python3
"""Aggregate paired phased summary CSVs across workload seeds."""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path
from typing import Any


METRICS: list[tuple[str, str]] = [
    ("baselineNormalIncludedReceipt", "baselineNormalIncluded"),
    ("attackRunNormalIncludedReceipt", "attackRunNormalIncluded"),
    ("baselineNormalFeeEth", "baselineNormalFeeEth"),
    ("attackRunNormalFeeEth", "attackRunNormalFeeEth"),
    ("pairedNormalFeeDeltaEth", "pairedNormalFeeDeltaEth"),
    ("attackSubmitted", "attackSubmitted"),
    ("attackAccepted", "attackAccepted"),
    ("attackIncludedReceipt", "attackIncluded"),
    ("attackFeeEth", "attackFeeEth"),
    ("normalBacklogAtAttackEnd", "normalBacklogAtAttackEnd"),
    ("normalBacklogAtRecoveryEnd", "normalBacklogAtRecoveryEnd"),
    ("txpoolPendingFinal", "finalTxpoolPending"),
    ("txpoolQueuedFinal", "finalTxpoolQueued"),
    ("txpoolResidentFinal", "finalTxpoolResident"),
    ("peakTxpoolPendingGasM", "peakTxpoolPendingGasM"),
    ("peakTxpoolGasM", "peakTxpoolGasM"),
    ("finalTxpoolGasM", "finalTxpoolGasM"),
    ("pendingPoolAmplification", "pendingPoolAmplification"),
    ("residentPoolAmplification", "residentPoolAmplification"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        metavar="SEED=CSV",
        help="One seed summary CSV, e.g. seed01=path/to/summary.csv.",
    )
    parser.add_argument("--out-prefix", required=True, type=Path)
    parser.add_argument("--title", default="Paired Randomized 3-Seed Summary")
    return parser.parse_args()


def read_one_row(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 1:
        raise RuntimeError(f"{path} must contain exactly one data row")
    return rows[0]


def parse_input(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise RuntimeError(f"--input must be SEED=CSV, got {value!r}")
    seed, path = value.split("=", 1)
    seed = seed.strip()
    if not seed:
        raise RuntimeError(f"empty seed label in {value!r}")
    return seed, Path(path)


def as_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def value_for(row: dict[str, str], source_key: str) -> float:
    if source_key == "txpoolResidentFinal":
        existing = row.get(source_key)
        if existing not in (None, ""):
            return as_number(existing)
        return as_number(row.get("txpoolPendingFinal")) + as_number(row.get("txpoolQueuedFinal"))
    if source_key == "peakTxpoolPendingGasM":
        existing = row.get(source_key)
        if existing not in (None, ""):
            return as_number(existing)
        return as_number(row.get("peakTxpoolGasM"))
    if source_key == "pendingPoolAmplification":
        existing = row.get(source_key)
        if existing not in (None, ""):
            return as_number(existing)
        included = as_number(row.get("attackIncludedReceipt"))
        return as_number(row.get("txpoolPendingFinal")) / included if included else 0.0
    if source_key == "residentPoolAmplification":
        existing = row.get(source_key)
        if existing not in (None, ""):
            return as_number(existing)
        included = as_number(row.get("attackIncludedReceipt"))
        resident = value_for(row, "txpoolResidentFinal")
        return resident / included if included else 0.0
    return as_number(row.get(source_key))


def fmt(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.12f}".rstrip("0").rstrip(".")


def aggregate(inputs: list[tuple[str, Path]]) -> list[dict[str, str]]:
    seed_rows = [(seed, read_one_row(path)) for seed, path in inputs]
    out_rows: list[dict[str, str]] = []
    for source_key, metric_name in METRICS:
        values = [value_for(row, source_key) for _seed, row in seed_rows]
        out: dict[str, str] = {"metric": metric_name}
        for (seed, _row), value in zip(seed_rows, values):
            out[seed] = fmt(value)
        mean = statistics.mean(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        out["mean"] = fmt(mean)
        out["stdev"] = fmt(stdev)
        out_rows.append(out)
    return out_rows


def write_csv(path: Path, rows: list[dict[str, str]], seeds: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["metric", *seeds, "mean", "stdev"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: list[dict[str, str]], seeds: list[str], title: str) -> None:
    headers = ["Metric", *seeds, "Mean", "Std"]
    lines = [f"# {title}", "", "| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        values = [row["metric"], *(row[seed] for seed in seeds), row["mean"], row["stdev"]]
        lines.append("| " + " | ".join(values) + " |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    inputs = [parse_input(item) for item in args.input]
    seeds = [seed for seed, _path in inputs]
    rows = aggregate(inputs)
    csv_path = args.out_prefix.with_suffix(".csv")
    md_path = args.out_prefix.with_suffix(".md")
    write_csv(csv_path, rows, seeds)
    write_markdown(md_path, rows, seeds, args.title)
    print(csv_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
