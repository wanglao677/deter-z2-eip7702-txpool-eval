#!/usr/bin/env python3
"""Summarize a paired phased baseline/attack run using receipt-backed data."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


ETHER = 10**18


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--attack-dir", required=True, type=Path)
    parser.add_argument("--out-prefix", required=True, type=Path)
    return parser.parse_args()


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text == "":
        return 0
    return int(text, 16) if text.startswith("0x") else int(text)


def eth(value_wei: int) -> float:
    return value_wei / ETHER


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_metrics(run_dir: Path) -> dict[str, str]:
    rows = read_csv(run_dir / "metrics.csv")
    return rows[0] if rows else {}


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def audit_group(run_dir: Path, group: str) -> dict[str, Any]:
    audit = read_json(run_dir / "fee_receipt_audit.json")
    return audit.get("groups", {}).get(group, {}) if isinstance(audit, dict) else {}


def row_at_phase_end(rows: list[dict[str, str]], phase: str) -> dict[str, str]:
    selected = [row for row in rows if row.get("phase") == phase]
    return selected[-1] if selected else {}


def metric_or_audit_count(metrics: dict[str, str], run_dir: Path, group: str, key: str) -> int:
    audited = audit_group(run_dir, group).get(key)
    if audited not in (None, ""):
        return as_int(audited)
    return as_int(metrics.get(f"{group}{key[:1].upper()}{key[1:]}"))


def pool_gas_m(row: dict[str, str], metrics: dict[str, str], *, include_queued: bool) -> float:
    normal_gas = as_int(metrics.get("normalGas")) or 150000
    attack_gas = as_int(metrics.get("attackGas")) or 500000
    pending = as_int(row.get("txpoolPendingAfterBlock"))
    queued = as_int(row.get("txpoolQueuedAfterBlock")) if include_queued else 0
    normal_backlog = as_int(row.get("normalBacklog"))
    attack_backlog = as_int(row.get("attackBacklog"))
    total_backlog = normal_backlog + attack_backlog
    if total_backlog > 0:
        avg_gas = (normal_backlog * normal_gas + attack_backlog * attack_gas) / total_backlog
    else:
        avg_gas = normal_gas
    return (pending + queued) * avg_gas / 1_000_000


def fee_wei(run_dir: Path, group: str) -> int:
    return as_int(audit_group(run_dir, group).get("feeWei"))


def write_one_row_csv(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def workload_plan_same(baseline_dir: Path, attack_dir: Path) -> bool:
    baseline = baseline_dir / "normal_workload_plan.csv"
    attack = attack_dir / "normal_workload_plan.csv"
    if not baseline.exists() or not attack.exists():
        return False
    baseline_rows = read_csv(baseline)
    attack_rows = read_csv(attack)
    baseline_plan = [
        (as_int(row.get("globalTick")), str(row.get("normalEnabled")), as_int(row.get("normalCount")))
        for row in baseline_rows
    ]
    attack_plan = [
        (as_int(row.get("globalTick")), str(row.get("normalEnabled")), as_int(row.get("normalCount")))
        for row in attack_rows
    ]
    return baseline_plan == attack_plan


def summarize(baseline_dir: Path, attack_dir: Path) -> dict[str, Any]:
    baseline_metrics = read_metrics(baseline_dir)
    attack_metrics = read_metrics(attack_dir)
    baseline_rows = read_csv(baseline_dir / "timeseries.csv")
    attack_rows = read_csv(attack_dir / "timeseries.csv")
    attack_end = row_at_phase_end(attack_rows, "attack_on")
    recovery_end = row_at_phase_end(attack_rows, "recovery")
    final_attack_row = attack_rows[-1] if attack_rows else {}
    pending_pool_values = [pool_gas_m(row, attack_metrics, include_queued=False) for row in attack_rows]
    total_pool_values = [pool_gas_m(row, attack_metrics, include_queued=True) for row in attack_rows]

    baseline_normal_fee = fee_wei(baseline_dir, "normal")
    attack_normal_fee = fee_wei(attack_dir, "normal")
    attack_fee = fee_wei(attack_dir, "attack")
    attack_included = metric_or_audit_count(attack_metrics, attack_dir, "attack", "included")
    attack_pending_final = as_int(attack_metrics.get("txpoolPendingFinal"))
    attack_queued_final = as_int(attack_metrics.get("txpoolQueuedFinal"))
    attack_resident_final = attack_pending_final + attack_queued_final

    return {
        "client": attack_metrics.get("client") or baseline_metrics.get("client"),
        "workloadSeed": attack_metrics.get("workloadSeed") or baseline_metrics.get("workloadSeed"),
        "normalRateJitter": attack_metrics.get("normalRateJitter") or baseline_metrics.get("normalRateJitter"),
        "normalSenderShuffle": attack_metrics.get("normalSenderShuffle") or baseline_metrics.get("normalSenderShuffle"),
        "normalWorkloadPlanSame": workload_plan_same(baseline_dir, attack_dir),
        "baselineNormalSubmitted": as_int(baseline_metrics.get("normalSubmitted")),
        "baselineNormalAccepted": as_int(baseline_metrics.get("normalAccepted")),
        "baselineNormalIncludedTimeseries": as_int(baseline_metrics.get("normalIncluded")),
        "baselineNormalIncludedReceipt": metric_or_audit_count(baseline_metrics, baseline_dir, "normal", "included"),
        "baselineNormalSuccessfulReceipt": metric_or_audit_count(baseline_metrics, baseline_dir, "normal", "successful"),
        "baselineNormalFeeEth": f"{eth(baseline_normal_fee):.12f}",
        "attackRunNormalSubmitted": as_int(attack_metrics.get("normalSubmitted")),
        "attackRunNormalAccepted": as_int(attack_metrics.get("normalAccepted")),
        "attackRunNormalIncludedTimeseries": as_int(attack_metrics.get("normalIncluded")),
        "attackRunNormalIncludedReceipt": metric_or_audit_count(attack_metrics, attack_dir, "normal", "included"),
        "attackRunNormalSuccessfulReceipt": metric_or_audit_count(attack_metrics, attack_dir, "normal", "successful"),
        "attackRunNormalFeeEth": f"{eth(attack_normal_fee):.12f}",
        "pairedNormalFeeDeltaEth": f"{eth(attack_normal_fee - baseline_normal_fee):.12f}",
        "attackSubmitted": as_int(attack_metrics.get("attackSubmitted")),
        "attackAccepted": as_int(attack_metrics.get("attackAccepted")),
        "attackIncludedTimeseries": as_int(attack_metrics.get("attackIncluded")),
        "attackIncludedReceipt": attack_included,
        "attackSuccessfulReceipt": metric_or_audit_count(attack_metrics, attack_dir, "attack", "successful"),
        "attackUniqueSendersReceipt": metric_or_audit_count(attack_metrics, attack_dir, "attack", "uniqueSenders"),
        "attackFeeEth": f"{eth(attack_fee):.12f}",
        "normalBacklogAtAttackEnd": as_int(attack_end.get("normalBacklog")),
        "attackBacklogAtAttackEnd": as_int(attack_end.get("attackBacklog")),
        "normalBacklogAtRecoveryEnd": as_int(recovery_end.get("normalBacklog")),
        "attackBacklogAtRecoveryEnd": as_int(recovery_end.get("attackBacklog")),
        "normalBacklogFinal": as_int(final_attack_row.get("normalBacklog")),
        "attackBacklogFinal": as_int(final_attack_row.get("attackBacklog")),
        "txpoolPendingFinal": attack_pending_final,
        "txpoolQueuedFinal": attack_queued_final,
        "txpoolResidentFinal": attack_resident_final,
        "peakTxpoolPendingGasM": f"{max(pending_pool_values) if pending_pool_values else 0.0:.6f}",
        "finalTxpoolPendingGasM": f"{pending_pool_values[-1] if pending_pool_values else 0.0:.6f}",
        "peakTxpoolGasM": f"{max(total_pool_values) if total_pool_values else 0.0:.6f}",
        "finalTxpoolGasM": f"{total_pool_values[-1] if total_pool_values else 0.0:.6f}",
        "txpoolGasAucMBlocks": f"{sum(total_pool_values):.6f}",
        "pendingPoolAmplification": f"{attack_pending_final / attack_included:.6f}" if attack_included else "",
        "residentPoolAmplification": f"{attack_resident_final / attack_included:.6f}" if attack_included else "",
        "acceptedBacklogAmplification": f"{as_int(final_attack_row.get('attackBacklog')) / attack_included:.6f}" if attack_included else "",
        "baselineDir": str(baseline_dir),
        "attackDir": str(attack_dir),
    }


def write_markdown(path: Path, row: dict[str, Any]) -> None:
    lines = [
        "# Paired Randomized Phased Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Client | {row['client']} |",
        f"| Workload seed | {row['workloadSeed']} |",
        f"| Normal workload plan same | {row['normalWorkloadPlanSame']} |",
        f"| Baseline normal included, receipt | {row['baselineNormalIncludedReceipt']} |",
        f"| Attack-run normal included, receipt | {row['attackRunNormalIncludedReceipt']} |",
        f"| Baseline normal fee | {row['baselineNormalFeeEth']} ETH |",
        f"| Attack-run normal fee | {row['attackRunNormalFeeEth']} ETH |",
        f"| Paired normal fee delta | {row['pairedNormalFeeDeltaEth']} ETH |",
        f"| Attack submitted / accepted / included | {row['attackSubmitted']} / {row['attackAccepted']} / {row['attackIncludedReceipt']} |",
        f"| Attack fee | {row['attackFeeEth']} ETH |",
        f"| Normal backlog at attack end | {row['normalBacklogAtAttackEnd']} |",
        f"| Normal backlog at recovery end | {row['normalBacklogAtRecoveryEnd']} |",
        f"| Final txpool pending / queued | {row['txpoolPendingFinal']} / {row['txpoolQueuedFinal']} |",
        f"| Final txpool resident total | {row['txpoolResidentFinal']} |",
        f"| Peak pending-only txpool gas pressure | {row['peakTxpoolPendingGasM']} M gas |",
        f"| Peak resident txpool gas pressure | {row['peakTxpoolGasM']} M gas |",
        f"| Final resident txpool gas pressure | {row['finalTxpoolGasM']} M gas |",
        f"| Pending-only pool amplification | {row['pendingPoolAmplification']}x |",
        f"| Resident pool amplification | {row['residentPoolAmplification']}x |",
        "",
        "Notes: inclusion and fee totals prefer `fee_receipt_audit.json`; resident txpool pressure counts pending plus queued transactions from `timeseries.csv`.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    row = summarize(args.baseline_dir, args.attack_dir)
    args.out_prefix.parent.mkdir(parents=True, exist_ok=True)
    write_one_row_csv(args.out_prefix.with_suffix(".csv"), row)
    args.out_prefix.with_suffix(".json").write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    write_markdown(args.out_prefix.with_suffix(".md"), row)
    print(args.out_prefix.with_suffix(".csv"))
    print(args.out_prefix.with_suffix(".json"))
    print(args.out_prefix.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
