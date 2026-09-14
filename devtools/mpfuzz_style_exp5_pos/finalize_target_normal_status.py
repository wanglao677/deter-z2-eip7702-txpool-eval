#!/usr/bin/env python3
"""Pin final visibility for a filtered set of normal transactions.

This audit is intentionally lighter than txpool_content-based auditing. It only
queries the target normal transaction hashes with eth_getTransactionReceipt and
eth_getTransactionByHash, so it remains usable when the node txpool is too large
to serialize in one txpool_content response.
"""

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


def rpc_batch(rpc: str, method: str, params_list: list[list[Any]], batch_size: int) -> list[Any]:
    ordered: list[Any] = []
    for start in range(0, len(params_list), batch_size):
        chunk = params_list[start:start + batch_size]
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
            for i, params in enumerate(chunk)
        ]
        request = urllib.request.Request(
            rpc,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            rows = json.load(response)
        if not isinstance(rows, list):
            raise RuntimeError(f"{method} batch response is not a list")
        indexed = {row.get("id"): row for row in rows}
        if set(indexed) != set(range(len(payload))):
            raise RuntimeError(f"{method} batch response is incomplete")
        for i in range(len(payload)):
            row = indexed[i]
            if "error" in row:
                raise RuntimeError(f"{method} request failed: {row['error']}")
            ordered.append(row.get("result"))
    return ordered


def latest_block_number(rpc: str) -> int:
    block = audit.rpc_call(rpc, "eth_getBlockByNumber", ["latest", False])
    return audit.as_int(block.get("number")) or 0


def wait_for_next_block(rpc: str, current_number: int, timeout: float) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        number = latest_block_number(rpc)
        if number > current_number:
            return number
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for a block after {current_number}")


def read_target_records(
    run_dir: Path,
    submitted_phase: str,
    normal_role: str,
    labels: set[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    records = []
    for record in audit.read_jsonl(run_dir / "normal_records.jsonl"):
        tx_hash = audit.record_hash(record)
        if not tx_hash:
            continue
        if submitted_phase and record.get("phase") != submitted_phase:
            continue
        if normal_role and record.get("normalRole", "") != normal_role:
            continue
        if labels and str(record.get("label", "")) not in labels:
            continue
        records.append(record)
        if limit is not None and len(records) >= limit:
            break
    return records


def included_phase_for_receipt(
    receipt: dict[str, Any],
    block_index: dict[int, dict[str, str]],
    last_window_block: int,
) -> tuple[str, str, str]:
    block_number = audit.as_int(receipt.get("blockNumber"))
    fields = block_index.get(block_number or -1, {})
    phase = fields.get("includedPhase", "")
    if not phase and block_number is not None:
        phase = "after_window" if block_number > last_window_block else "unknown"
    return (
        phase,
        fields.get("includedPhaseTick", ""),
        fields.get("includedGlobalTick", ""),
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def finalize(
    run_dir: Path,
    rpc: str,
    submitted_phase: str,
    normal_role: str,
    labels: set[str],
    limit: int | None,
    observations: int,
    confirm_absent: int,
    batch_size: int,
    wait_timeout: float,
    output_prefix: str,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    records = read_target_records(run_dir, submitted_phase, normal_role, labels, limit)
    if not records:
        raise RuntimeError("no matching normal transactions found")

    block_index = audit.read_block_phase_index(run_dir)
    last_window_block = max(block_index, default=-1)
    states: dict[str, dict[str, Any]] = {}
    for record in records:
        tx_hash = audit.record_hash(record)
        states[tx_hash] = {
            "record": record,
            "receipt": None,
            "txObject": None,
            "visibleObservations": 0,
            "absentObservations": 0,
            "firstAbsentBlock": "",
            "lastAbsentBlock": "",
            "lastVisibleBlock": "",
        }

    observations_path = run_dir / f"{output_prefix}_observations.jsonl"
    start_block = latest_block_number(rpc)
    current_block = start_block
    with observations_path.open("a", encoding="utf-8") as obs_file:
        for obs_index in range(1, observations + 1):
            current_block = latest_block_number(rpc)
            pending_hashes = [h for h, value in states.items() if value["receipt"] is None]
            receipts = rpc_batch(rpc, "eth_getTransactionReceipt", [[h] for h in pending_hashes], batch_size)
            for tx_hash, receipt in zip(pending_hashes, receipts):
                if receipt and str(receipt.get("transactionHash", "")).lower() != tx_hash:
                    raise RuntimeError("receipt hash does not match request hash")
                if receipt:
                    states[tx_hash]["receipt"] = receipt

            no_receipt_hashes = [h for h, value in states.items() if value["receipt"] is None]
            tx_objects = rpc_batch(rpc, "eth_getTransactionByHash", [[h] for h in no_receipt_hashes], batch_size)
            visible = 0
            absent = 0
            for tx_hash, tx_object in zip(no_receipt_hashes, tx_objects):
                state = states[tx_hash]
                if tx_object:
                    state["txObject"] = tx_object
                    state["visibleObservations"] += 1
                    state["absentObservations"] = 0
                    state["lastVisibleBlock"] = current_block
                    visible += 1
                else:
                    state["absentObservations"] += 1
                    if not state["firstAbsentBlock"]:
                        state["firstAbsentBlock"] = current_block
                    state["lastAbsentBlock"] = current_block
                    absent += 1

            included = sum(1 for value in states.values() if value["receipt"] is not None)
            confirmed_absent = sum(
                1
                for value in states.values()
                if value["receipt"] is None and value["absentObservations"] >= confirm_absent
            )
            line = {
                "observation": obs_index,
                "blockNumber": current_block,
                "targetNormalTxs": len(states),
                "includedSoFar": included,
                "remainingNoReceipt": len(no_receipt_hashes),
                "nodeVisibleNoReceipt": visible,
                "absentThisObservation": absent,
                "confirmedAbsentNoReceipt": confirmed_absent,
            }
            obs_file.write(json.dumps(line, ensure_ascii=False) + "\n")
            obs_file.flush()
            print(json.dumps(line, ensure_ascii=False), flush=True)
            if obs_index < observations:
                current_block = wait_for_next_block(rpc, current_block, wait_timeout)

    output_rows: list[dict[str, Any]] = []
    for tx_hash, value in sorted(
        states.items(),
        key=lambda item: (
            str(item[1]["record"].get("phase", "")),
            int(item[1]["record"].get("globalTick", 0) or 0),
            str(item[1]["record"].get("label", "")),
        ),
    ):
        record = value["record"]
        receipt = value["receipt"]
        tx_object = value["txObject"]
        if receipt:
            status = audit.as_int(receipt.get("status"))
            final_state = "INCLUDED_SUCCESS" if status == 1 else "INCLUDED_FAILED"
            included_phase, included_tick, included_global_tick = included_phase_for_receipt(
                receipt, block_index, last_window_block
            )
        elif value["absentObservations"] >= confirm_absent:
            final_state = "ABSENT_NO_RECEIPT_CONFIRMED"
            included_phase = included_tick = included_global_tick = ""
        elif tx_object:
            final_state = "NODE_VISIBLE_NO_RECEIPT"
            included_phase = included_tick = included_global_tick = ""
        else:
            final_state = "UNKNOWN_NEEDS_MORE_OBSERVATIONS"
            included_phase = included_tick = included_global_tick = ""

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
            "receiptBlockNumber": audit.as_int(receipt.get("blockNumber")) if receipt else "",
            "receiptBlockHash": receipt.get("blockHash", "") if receipt else "",
            "includedPhase": included_phase,
            "includedPhaseTick": included_tick,
            "includedGlobalTick": included_global_tick,
            "gasUsed": gas_used or "",
            "effectiveGasPrice": effective_price or "",
            "nodeVisibleNoReceipt": "1" if tx_object and not receipt else "0",
            "txObjectBlockHash": tx_object.get("blockHash", "") if tx_object else "",
            "lastVisibleBlock": value["lastVisibleBlock"],
            "absentObservations": value["absentObservations"],
            "firstAbsentBlock": value["firstAbsentBlock"],
            "lastAbsentBlock": value["lastAbsentBlock"],
            "finalState": final_state,
            "feeWei": fee_wei,
        })

    fields = [
        "group", "normalRole", "label", "sender", "nonce", "txHash", "submittedPhase",
        "submittedPhaseTick", "submittedGlobalTick", "submittedAtBlock", "gasPriceWei",
        "receiptFound", "receiptStatus", "receiptBlockNumber", "receiptBlockHash",
        "includedPhase", "includedPhaseTick", "includedGlobalTick", "gasUsed",
        "effectiveGasPrice", "nodeVisibleNoReceipt", "txObjectBlockHash",
        "lastVisibleBlock", "absentObservations", "firstAbsentBlock", "lastAbsentBlock",
        "finalState", "feeWei",
    ]
    output_csv = run_dir / f"{output_prefix}.csv"
    output_json = run_dir / f"{output_prefix}_summary.json"
    write_csv(output_csv, output_rows, fields)

    summary = {
        "runDir": str(run_dir),
        "rpc": rpc,
        "submittedPhaseFilter": submitted_phase,
        "normalRoleFilter": normal_role,
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
        "outputCsv": str(output_csv),
        "observationLog": str(observations_path),
    }
    save_json(output_json, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--submitted-phase", default="attack_on")
    parser.add_argument("--normal-role", default="victim")
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--observations", type=int, default=8)
    parser.add_argument("--confirm-absent", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--wait-timeout", type=float, default=240)
    parser.add_argument("--output-prefix", default="target_normal_finality_audit")
    args = parser.parse_args()
    if args.observations < 1:
        raise ValueError("observations must be > 0")
    if args.confirm_absent < 1:
        raise ValueError("confirm-absent must be > 0")
    finalize(
        args.run_dir,
        args.rpc,
        args.submitted_phase,
        args.normal_role,
        set(args.label),
        args.limit,
        args.observations,
        args.confirm_absent,
        args.batch_size,
        args.wait_timeout,
        args.output_prefix,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
