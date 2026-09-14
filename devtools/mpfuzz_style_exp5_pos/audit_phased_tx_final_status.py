#!/usr/bin/env python3
"""Classify the final state of every transaction from a phased experiment run.

The phased runner records every attempted normal/attack transaction in
normal_records.jsonl and attack_records.jsonl. This audit script combines those
records with receipts and the node's final txpool_content snapshot to label each
transaction as included, still pending/queued, dropped/evicted, or rejected.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


def rpc_call(rpc_url: str, method: str, params: list[Any] | None = None) -> Any:
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1}
    ).encode()
    request = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode())
    if "error" in body:
        raise RuntimeError(f"{method} failed: {body['error']}")
    return body.get("result")


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def read_block_phase_index(run_dir: Path) -> dict[int, dict[str, str]]:
    """Map observed block numbers to phased workload coordinates."""
    path = run_dir / "timeseries.csv"
    if not path.exists():
        return {}
    index: dict[int, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            block_number = as_int(row.get("blockNumber"))
            if block_number is None:
                continue
            fields = {
                "includedPhase": row.get("phase", ""),
                "includedPhaseTick": row.get("phaseTick", ""),
                "includedGlobalTick": row.get("globalTick", ""),
            }
            start = as_int(row.get("observationStartBlock")) or block_number
            for number in range(start, block_number + 1):
                index[number] = fields
    return index


def record_hash(record: dict[str, Any]) -> str:
    return str(record.get("hash") or record.get("txHash") or "").lower()


def iter_txpool_transactions(node: Any):
    if isinstance(node, dict):
        if isinstance(node.get("hash"), str):
            yield node
            return
        for value in node.values():
            yield from iter_txpool_transactions(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_txpool_transactions(value)


def txpool_locations(rpc_url: str) -> tuple[dict[str, str], str]:
    try:
        content = rpc_call(rpc_url, "txpool_content", [])
    except Exception as exc:  # noqa: BLE001 - keep audit useful if txpool_content is unavailable.
        return {}, str(exc)
    locations: dict[str, str] = {}
    if not isinstance(content, dict):
        return locations, "txpool_content returned a non-object result"
    for location in ("pending", "queued"):
        for tx in iter_txpool_transactions(content.get(location, {})):
            tx_hash = str(tx.get("hash", "")).lower()
            if tx_hash:
                locations[tx_hash] = location
    return locations, ""


def receipt_with_wait(rpc_url: str, tx_hash: str, timeout: float, poll_interval: float) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while True:
        receipt = rpc_call(rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            return receipt
        if time.time() >= deadline:
            return None
        time.sleep(poll_interval)


def classify_record(
    rpc_url: str,
    group: str,
    record: dict[str, Any],
    block_phase_index: dict[int, dict[str, str]],
    pool_locations: dict[str, str],
    pool_content_available: bool,
    receipt_timeout: float,
    poll_interval: float,
    receipt_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tx_hash = record_hash(record)
    error = record.get("error") or record.get("sendError")
    base = {
        "group": group,
        "label": record.get("label", ""),
        "sender": record.get("sender", ""),
        "nonce": record.get("nonce", ""),
        "txHash": tx_hash,
        "submittedPhase": record.get("phase", ""),
        "submittedPhaseTick": record.get("submittedPhaseTick", record.get("phaseTick", "")),
        "gasPriceWei": record.get("gasPriceWei", ""),
        "calldataPaddingBytes": record.get("calldataPaddingBytes", ""),
        "accepted": "1" if tx_hash and not error else "0",
        "sendError": error or "",
        "receiptFound": "0",
        "receiptStatus": "",
        "receiptBlockNumber": "",
        "receiptBlockHash": "",
        "includedPhase": "",
        "includedPhaseTick": "",
        "includedGlobalTick": "",
        "gasUsed": "",
        "effectiveGasPrice": "",
        "finalPoolLocation": "",
        "finalState": "",
    }
    if not tx_hash:
        base["finalState"] = "REJECTED_OR_SEND_ERROR" if error else "UNKNOWN"
        return base

    receipt = receipt_cache[tx_hash] if receipt_cache is not None else receipt_with_wait(rpc_url, tx_hash, receipt_timeout, poll_interval)
    if receipt:
        status = as_int(receipt.get("status"))
        receipt_block_number = as_int(receipt.get("blockNumber"))
        inclusion_fields = block_phase_index.get(receipt_block_number or -1, {})
        base.update(
            {
                "receiptFound": "1",
                "receiptStatus": receipt.get("status", ""),
                "receiptBlockNumber": receipt_block_number or "",
                "receiptBlockHash": receipt.get("blockHash", ""),
                "includedPhase": inclusion_fields.get("includedPhase", ""),
                "includedPhaseTick": inclusion_fields.get("includedPhaseTick", ""),
                "includedGlobalTick": inclusion_fields.get("includedGlobalTick", ""),
                "gasUsed": as_int(receipt.get("gasUsed")) or "",
                "effectiveGasPrice": as_int(receipt.get("effectiveGasPrice")) or "",
                "finalState": "INCLUDED_SUCCESS" if status == 1 else "INCLUDED_FAILED",
            }
        )
        return base

    location = pool_locations.get(tx_hash, "")
    if error:
        base["finalState"] = "REJECTED_OR_SEND_ERROR"
    elif location == "pending":
        base["finalPoolLocation"] = "pending"
        base["finalState"] = "PENDING_FINAL"
    elif location == "queued":
        base["finalPoolLocation"] = "queued"
        base["finalState"] = "QUEUED_FINAL"
    elif not pool_content_available:
        base["finalState"] = "UNKNOWN_POOL_CONTENT_UNAVAILABLE"
    else:
        base["finalState"] = "DROPPED_OR_EVICTED"
    return base


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "group",
        "label",
        "sender",
        "nonce",
        "txHash",
        "submittedPhase",
        "submittedPhaseTick",
        "gasPriceWei",
        "calldataPaddingBytes",
        "accepted",
        "sendError",
        "receiptFound",
        "receiptStatus",
        "receiptBlockNumber",
        "receiptBlockHash",
        "includedPhase",
        "includedPhaseTick",
        "includedGlobalTick",
        "gasUsed",
        "effectiveGasPrice",
        "finalPoolLocation",
        "finalState",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def audit(run_dir: Path, rpc_url: str, receipt_timeout: float, poll_interval: float) -> None:
    run_dir = run_dir.resolve()
    records_by_group = {
        "normal": read_jsonl(run_dir / "normal_records.jsonl"),
        "attack": read_jsonl(run_dir / "attack_records.jsonl"),
    }
    block_phase_index = read_block_phase_index(run_dir)
    pool_locations, pool_error = txpool_locations(rpc_url)
    pool_content_available = not pool_error
    rows = []
    for group, records in records_by_group.items():
        for record in records:
            rows.append(
                classify_record(
                    rpc_url,
                    group,
                    record,
                    block_phase_index,
                    pool_locations,
                    pool_content_available,
                    receipt_timeout,
                    poll_interval,
                )
            )

    write_csv(run_dir / "tx_final_status.csv", rows)
    summary = {
        "runDir": str(run_dir),
        "rpc": rpc_url,
        "txpoolContentError": pool_error,
        "total": len(rows),
        "byGroup": {},
    }
    for group in records_by_group:
        group_rows = [row for row in rows if row["group"] == group]
        summary["byGroup"][group] = {
            "total": len(group_rows),
            "accepted": sum(row["accepted"] == "1" for row in group_rows),
            "states": dict(Counter(row["finalState"] for row in group_rows)),
            "includedByPhase": dict(
                Counter(
                    row["includedPhase"] or "unknown"
                    for row in group_rows
                    if row["receiptFound"] == "1"
                )
            ),
            "includedByBlock": dict(
                Counter(
                    str(row["receiptBlockNumber"])
                    for row in group_rows
                    if row["receiptFound"] == "1" and row["receiptBlockNumber"]
                )
            ),
            "poolLocations": dict(Counter(row["finalPoolLocation"] for row in group_rows if row["finalPoolLocation"])),
        }
    (run_dir / "tx_final_status_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"tx_final_status={run_dir / 'tx_final_status.csv'}")
    print(f"summary={run_dir / 'tx_final_status_summary.json'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--receipt-timeout", type=float, default=0)
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()
    audit(args.run_dir, args.rpc, args.receipt_timeout, args.poll_interval)
