#!/usr/bin/env python3
"""Follow unresolved normal transactions until their RPC-visible state is pinned."""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

import audit_phased_tx_final_status as audit


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def rpc_batch(rpc: str, method: str, params_list: list[list[Any]], batch_size: int) -> list[Any]:
    ordered: list[Any] = []
    for start in range(0, len(params_list), batch_size):
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
            for i, params in enumerate(params_list[start:start + batch_size])
        ]
        request = urllib.request.Request(
            rpc,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            rows = json.load(response)
        if not isinstance(rows, list):
            raise RuntimeError(f"{method} 批量响应不是列表")
        indexed = {row.get("id"): row for row in rows}
        if set(indexed) != set(range(len(payload))):
            raise RuntimeError(f"{method} 批量响应不完整")
        for i in range(len(payload)):
            row = indexed[i]
            if "error" in row:
                raise RuntimeError(f"{method} 请求失败：{row['error']}")
            ordered.append(row.get("result"))
    return ordered


def latest_block(rpc: str) -> dict[str, Any]:
    return audit.rpc_call(rpc, "eth_getBlockByNumber", ["latest", False])


def wait_for_next_block(rpc: str, current_number: int, timeout: float) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        block = latest_block(rpc)
        number = audit.as_int(block["number"]) or 0
        if number > current_number:
            return number
        time.sleep(2)
    raise RuntimeError(f"等待新区块超时：当前区块 {current_number}")


def target_normal_records(run_dir: Path, only_unresolved: bool) -> list[dict[str, Any]]:
    records = [row for row in audit.read_jsonl(run_dir / "normal_records.jsonl") if audit.record_hash(row)]
    if not only_unresolved:
        return records
    unresolved = {
        row["txHash"].lower()
        for row in read_csv(run_dir / "tx_final_status.csv")
        if row.get("group") == "normal" and row.get("receiptFound") != "1"
    }
    return [row for row in records if audit.record_hash(row) in unresolved]


def txpool_locations_for_targets(rpc: str, targets: set[str]) -> tuple[dict[str, str], Counter[str]]:
    content = audit.rpc_call(rpc, "txpool_content", [])
    if not isinstance(content, dict):
        raise RuntimeError("txpool_content 响应不是对象")
    locations: dict[str, str] = {}
    pool_counts: Counter[str] = Counter()
    for location in ("pending", "queued"):
        for tx in audit.iter_txpool_transactions(content.get(location, {})):
            tx_hash = str(tx.get("hash", "")).lower()
            if not tx_hash:
                continue
            pool_counts[location] += 1
            if tx_hash in targets:
                locations[tx_hash] = location
    return locations, pool_counts


def receipt_state(receipt: dict[str, Any] | None) -> str:
    if not receipt:
        return ""
    return "INCLUDED_SUCCESS" if audit.as_int(receipt.get("status")) == 1 else "INCLUDED_FAILED"


def finalize(run_dir: Path, rpc: str, only_unresolved: bool, observations: int,
             confirm_absent: int, batch_size: int, wait_timeout: float) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    records = target_normal_records(run_dir, only_unresolved)
    if not records:
        raise RuntimeError("没有需要复核的 normal 交易")
    block_index = audit.read_block_phase_index(run_dir)
    last_window_block = max(block_index, default=-1)
    states: dict[str, dict[str, Any]] = {}
    for record in records:
        tx_hash = audit.record_hash(record)
        states[tx_hash] = {
            "record": record,
            "receipt": None,
            "lastPoolLocation": "",
            "lastSeenPoolBlock": "",
            "absentObservations": 0,
            "firstAbsentBlock": "",
            "lastAbsentBlock": "",
            "observedBlocks": [],
        }

    observations_path = run_dir / "normal_finality_observations.jsonl"
    start_block = audit.as_int(latest_block(rpc)["number"]) or 0
    current_block = start_block
    with observations_path.open("a", encoding="utf-8") as obs_file:
        for index in range(1, observations + 1):
            block = latest_block(rpc)
            current_block = audit.as_int(block["number"]) or current_block
            pending_hashes = [h for h, value in states.items() if value["receipt"] is None]
            receipts = rpc_batch(rpc, "eth_getTransactionReceipt", [[h] for h in pending_hashes], batch_size)
            for tx_hash, receipt in zip(pending_hashes, receipts):
                if receipt and str(receipt.get("transactionHash", "")).lower() != tx_hash:
                    raise RuntimeError("回执交易哈希与请求不一致")
                if receipt:
                    states[tx_hash]["receipt"] = receipt

            remaining = {h for h, value in states.items() if value["receipt"] is None}
            locations, pool_counts = txpool_locations_for_targets(rpc, remaining)
            located = Counter()
            absent = 0
            for tx_hash in remaining:
                state = states[tx_hash]
                location = locations.get(tx_hash, "")
                state["observedBlocks"].append(current_block)
                if location:
                    state["lastPoolLocation"] = location
                    state["lastSeenPoolBlock"] = current_block
                    state["absentObservations"] = 0
                    located[location] += 1
                else:
                    state["absentObservations"] += 1
                    if not state["firstAbsentBlock"]:
                        state["firstAbsentBlock"] = current_block
                    state["lastAbsentBlock"] = current_block
                    absent += 1

            included = sum(1 for value in states.values() if value["receipt"])
            confirmed_absent = sum(
                1
                for value in states.values()
                if value["receipt"] is None
                and not value["lastPoolLocation"]
                and value["absentObservations"] >= confirm_absent
            )
            line = {
                "observation": index,
                "blockNumber": current_block,
                "targetNormalTxs": len(states),
                "includedSoFar": included,
                "remainingNoReceipt": len(remaining),
                "targetPending": located.get("pending", 0),
                "targetQueued": located.get("queued", 0),
                "targetAbsentThisObservation": absent,
                "confirmedAbsentNoReceipt": confirmed_absent,
                "nodePoolPending": pool_counts.get("pending", 0),
                "nodePoolQueued": pool_counts.get("queued", 0),
            }
            obs_file.write(json.dumps(line, ensure_ascii=False) + "\n")
            obs_file.flush()
            print(json.dumps(line, ensure_ascii=False), flush=True)
            if index < observations:
                current_block = wait_for_next_block(rpc, current_block, wait_timeout)

    output_rows: list[dict[str, Any]] = []
    for tx_hash, value in sorted(states.items(), key=lambda item: (str(item[1]["record"].get("phase", "")),
                                                                   int(item[1]["record"].get("nonce", 0)),
                                                                   item[0])):
        record = value["record"]
        receipt = value["receipt"]
        final_state = receipt_state(receipt)
        final_location = ""
        if not final_state:
            final_location = value["lastPoolLocation"]
            if final_location == "pending":
                final_state = "STILL_PENDING_AT_FINAL_AUDIT"
            elif final_location == "queued":
                final_state = "STILL_QUEUED_AT_FINAL_AUDIT"
            elif value["absentObservations"] >= confirm_absent:
                final_state = "ABSENT_NO_RECEIPT_CONFIRMED"
            else:
                final_state = "UNKNOWN_NEEDS_MORE_OBSERVATIONS"
        receipt_block_number = audit.as_int(receipt.get("blockNumber")) if receipt else None
        included_phase = ""
        included_tick = ""
        included_global_tick = ""
        if receipt_block_number is not None:
            fields = block_index.get(receipt_block_number, {})
            included_phase = fields.get("includedPhase", "")
            included_tick = fields.get("includedPhaseTick", "")
            included_global_tick = fields.get("includedGlobalTick", "")
            if not included_phase:
                included_phase = "after_window" if receipt_block_number > last_window_block else "unknown"
        gas_used = audit.as_int(receipt.get("gasUsed")) if receipt else None
        effective_price = audit.as_int(receipt.get("effectiveGasPrice")) if receipt else None
        fee_wei = (gas_used or 0) * (effective_price or 0)
        output_rows.append({
            "group": "normal",
            "normalRole": record.get("normalRole", ""),
            "label": record.get("label", ""),
            "sender": record.get("sender", ""),
            "nonce": record.get("nonce", ""),
            "txHash": tx_hash,
            "submittedPhase": record.get("phase", ""),
            "submittedPhaseTick": record.get("submittedPhaseTick", record.get("phaseTick", "")),
            "submittedGlobalTick": record.get("globalTick", ""),
            "submittedAtBlock": record.get("submittedAtBlock", ""),
            "gasPriceWei": record.get("gasPriceWei", ""),
            "receiptFound": "1" if receipt else "0",
            "receiptStatus": receipt.get("status", "") if receipt else "",
            "receiptBlockNumber": receipt_block_number or "",
            "receiptBlockHash": receipt.get("blockHash", "") if receipt else "",
            "includedPhase": included_phase,
            "includedPhaseTick": included_tick,
            "includedGlobalTick": included_global_tick,
            "gasUsed": gas_used or "",
            "effectiveGasPrice": effective_price or "",
            "lastPoolLocation": final_location,
            "lastSeenPoolBlock": value["lastSeenPoolBlock"],
            "absentObservations": value["absentObservations"],
            "firstAbsentBlock": value["firstAbsentBlock"],
            "lastAbsentBlock": value["lastAbsentBlock"],
            "finalState": final_state,
            "feeWei": fee_wei,
        })

    fields = [
        "group", "normalRole", "label", "sender", "nonce", "txHash", "submittedPhase", "submittedPhaseTick",
        "submittedGlobalTick", "submittedAtBlock", "gasPriceWei", "receiptFound", "receiptStatus",
        "receiptBlockNumber", "receiptBlockHash", "includedPhase", "includedPhaseTick",
        "includedGlobalTick", "gasUsed", "effectiveGasPrice", "lastPoolLocation",
        "lastSeenPoolBlock", "absentObservations", "firstAbsentBlock", "lastAbsentBlock",
        "finalState", "feeWei",
    ]
    write_csv(run_dir / "normal_finality_audit.csv", output_rows, fields)
    summary = {
        "runDir": str(run_dir),
        "rpc": rpc,
        "onlyUnresolved": only_unresolved,
        "observations": observations,
        "confirmAbsentObservations": confirm_absent,
        "startBlock": start_block,
        "endBlock": current_block,
        "targetNormalTxs": len(output_rows),
        "states": dict(Counter(row["finalState"] for row in output_rows)),
        "submittedPhaseStates": {
            phase: dict(Counter(row["finalState"] for row in output_rows if row["submittedPhase"] == phase))
            for phase in sorted({row["submittedPhase"] for row in output_rows})
        },
        "normalRoleStates": {
            role: dict(Counter(row["finalState"] for row in output_rows if row["normalRole"] == role))
            for role in sorted({row["normalRole"] for row in output_rows})
        },
        "includedByPhase": dict(Counter(row["includedPhase"] for row in output_rows if row["receiptFound"] == "1")),
        "feeWei": sum(int(row["feeWei"]) for row in output_rows),
        "outputCsv": str(run_dir / "normal_finality_audit.csv"),
        "observationLog": str(observations_path),
    }
    save_json(run_dir / "normal_finality_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--only-unresolved", action="store_true")
    parser.add_argument("--observations", type=int, default=4)
    parser.add_argument("--confirm-absent", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--wait-timeout", type=float, default=180)
    args = parser.parse_args()
    if args.observations < 1:
        raise ValueError("observations 必须大于 0")
    if args.confirm_absent < 1:
        raise ValueError("confirm-absent 必须大于 0")
    finalize(args.run_dir, args.rpc, args.only_unresolved, args.observations,
             args.confirm_absent, args.batch_size, args.wait_timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
