#!/usr/bin/env python3
"""Archive receipt-backed per-block fees from an existing local phased run."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from plot_phased_tx_fee_paper_like import (
    as_int, block_to_tick, read_csv, read_metrics, read_records, receipt_fee_wei, rpc_call,
)


def collect(run_dir: Path, rpc_url: str) -> None:
    rows = read_csv(run_dir / "timeseries.csv")
    mapping = block_to_tick(rows)
    metrics = read_metrics(run_dir)
    chain_id = as_int(rpc_call(rpc_url, "eth_chainId", []))
    if chain_id != as_int(metrics["chainId"]):
        raise RuntimeError("RPC chainId differs from the saved run")
    records = {kind: read_records(run_dir / f"{kind}_records.jsonl") for kind in ("normal", "attack")}
    receipts = {kind: {} for kind in records}
    block_rows = []
    for number, tick in sorted(mapping.items()):
        block = rpc_call(rpc_url, "eth_getBlockByNumber", [hex(number), False])
        if not block:
            raise RuntimeError(f"Missing block {number}")
        block_receipts = rpc_call(rpc_url, "eth_getBlockReceipts", [hex(number)])
        if not isinstance(block_receipts, list):
            raise RuntimeError(f"Missing block receipts at {number}")
        if {r["transactionHash"].lower() for r in block_receipts} != {h.lower() for h in block["transactions"]}:
            raise RuntimeError(f"Incomplete block receipts at {number}")
        row = {"globalTick": tick, "blockNumber": number, "blockHash": block["hash"]}
        for kind in records:
            matched = []
            for receipt in block_receipts:
                key = receipt["transactionHash"].lower()
                if receipt["blockHash"] != block["hash"]:
                    raise RuntimeError(f"Block changed during receipt collection: {number}")
                if key in records[kind]:
                    if as_int(receipt["effectiveGasPrice"]) != as_int(records[kind][key]["gasPriceWei"]):
                        raise RuntimeError(f"Receipt price differs from recorded transaction {key}")
                    receipts[kind][key] = receipt
                    matched.append(receipt)
            row[f"{kind}Included"] = len(matched)
            row[f"{kind}GasUsed"] = sum(as_int(r["gasUsed"]) for r in matched)
            row[f"{kind}FeeWei"] = sum(receipt_fee_wei(r) for r in matched)
        block_rows.append(row)
        print(f"block={number} tick={tick} normal={row['normalIncluded']} attack={row['attackIncluded']}", flush=True)

    report = {"chainId": chain_id, "firstBlock": min(mapping), "lastBlock": max(mapping), "groups": {}}
    for kind, matched in receipts.items():
        expected = as_int(metrics.get(f"{kind}Included"))
        if len(matched) < expected:
            raise RuntimeError(f"{kind}: only {len(matched)} matching receipts, expected at least {expected}; no cache written")
        if records[kind] and not matched:
            raise RuntimeError(f"No {kind} receipts match this run; RPC may point at a different chain")
        report["groups"][kind] = {
            "included": len(matched),
            "successful": sum(as_int(r["status"]) == 1 for r in matched.values()),
            "uniqueSenders": len({r["from"].lower() for r in matched.values()}),
            "gasUsedHistogram": dict(Counter(as_int(r["gasUsed"]) for r in matched.values())),
            "paddingBytesHistogram": dict(Counter(str(records[kind][h].get("calldataPaddingBytes", "unspecified")) for h in matched)),
            "feeWei": sum(receipt_fee_wei(r) for r in matched.values()),
        }
    for kind, matched in receipts.items():
        (run_dir / f"{kind}_receipts_rpc.json").write_text(json.dumps(matched, indent=2) + "\n", encoding="utf-8")
    with (run_dir / "fee_receipt_blocks.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(block_rows[0]))
        writer.writeheader()
        writer.writerows(block_rows)
    (run_dir / "fee_receipt_audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--rpc", required=True)
    args = parser.parse_args()
    collect(args.run_dir, args.rpc)
