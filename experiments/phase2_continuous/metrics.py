"""Receipt-based per-slot metrics and independent baseline cost estimate."""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from plan import phase_for_slot

ETH = 10**18
FIELDS = (
    "group", "client", "phase", "relativeSlotIndex", "slotNumber",
    "relativeBlockIndex", "blockNumber", "blockHash", "blockTimestamp",
    "blockPresent", "baseFeePerGasWei", "blockGasLimit", "blockGasUsed",
    "totalIncludedTransactions", "normalIncluded", "attackIncluded",
    "normalGasUsed", "attackGasUsed", "normalPaidFeeWei", "attackPaidFeeWei",
    "normalPaidFeeEth", "attackPaidFeeEth", "normalPriorityFeeWei",
    "attackPriorityFeeWei", "normalBaseBurnWei", "attackBaseBurnWei",
    "normalEffectiveGasPriceMeanWei", "normalEffectiveGasPriceMedianWei",
    "normalEffectiveGasPriceMinWei", "normalEffectiveGasPriceMaxWei",
    "normalEffectiveGasPriceP10Wei", "normalEffectiveGasPriceP90Wei",
    "attackEffectiveGasPriceMeanWei", "attackEffectiveGasPriceMedianWei",
    "attackEffectiveGasPriceMinWei", "attackEffectiveGasPriceMaxWei",
    "attackEffectiveGasPriceP10Wei", "attackEffectiveGasPriceP90Wei",
    "normalSubmitted", "normalAdmitted", "normalRejected", "normalUnknown",
    "normalBroadcastLagMeanMs", "normalBroadcastLagP90Ms", "normalBroadcastLateCount",
    "attackSubmitted", "attackAdmitted", "attackRejected", "attackUnknown",
    "poolPending", "poolQueued", "poolSnapshotUnixMillis", "poolSnapshotError",
    "maxIncludedGasPriceWei", "maxIncludedEffectiveGasPriceWei", "estimatedConventionalSpamCostWei",
    "estimatedConventionalSpamCostEth", "spamEstimateStatus",
)


def number(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    raise ValueError(f"Not a quantity: {value!r}")


def setup_fee_totals(entries: list[dict], config: dict) -> dict[str, int]:
    """Validate setup receipts and sum fees from gas used times effective price."""
    expected = Counter(deploy=1, fund_normal=config["normalSenders"],
                       fund_attack=config["attackSenders"],
                       authorize=config["attackSenders"])
    counts = Counter()
    totals = {}
    for entry in entries:
        stage = entry.get("stage")
        if stage not in expected:
            raise RuntimeError(f"Unexpected setup stage: {stage!r}")
        receipt = entry.get("receipt")
        # Older Phase 2 runs nested the deployment record inside "receipt".
        if stage == "deploy" and isinstance(receipt, dict) and receipt.get("kind") == "deploy":
            receipt = receipt.get("receipt")
        if (not isinstance(receipt, dict) or
                any(field not in receipt for field in ("gasUsed", "effectiveGasPrice", "status"))):
            raise RuntimeError(f"{stage}: missing complete setup receipt")
        if receipt["status"] != "0x1":
            raise RuntimeError(f"{stage}: setup receipt did not succeed")
        counts[stage] += 1
        totals[stage] = totals.get(stage, 0) + (
            number(receipt["gasUsed"]) * number(receipt["effectiveGasPrice"]))
    if counts != expected:
        raise RuntimeError(f"Setup receipt counts {dict(counts)} differ from expected {dict(expected)}")
    return totals


def quantile(values: list[int], q: float):
    if not values:
        return ""
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] * (high - pos) + ordered[high] * (pos - low))


def price_fields(prefix: str, values: list[int]) -> dict:
    return {
        prefix + "MeanWei": round(statistics.mean(values)) if values else "",
        prefix + "MedianWei": round(statistics.median(values)) if values else "",
        prefix + "MinWei": min(values) if values else "",
        prefix + "MaxWei": max(values) if values else "",
        prefix + "P10Wei": quantile(values, .1),
        prefix + "P90Wei": quantile(values, .9),
    }


def jsonl(path: Path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict]):
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def classify_submission(record: dict) -> str:
    if record.get("errorKind") == "ambiguous":
        return "unknown"
    if record.get("error") or record.get("errorKind") == "rejected" or not record.get("hash"):
        return "rejected"
    return "admitted"


def summarize_slots(config: dict, client: str, group: str, genesis_timestamp: int,
                    anchor_block: int, anchor_slot: int, anchor_unix_ms: int,
                    blocks: list[dict], receipts: dict[str, dict],
                    submissions: list[dict], snapshots: list[dict]) -> list[dict]:
    phase = config["phase2"]
    total = phase["preSlots"] + phase["attackSlots"] + phase["postSlots"]
    seconds = config["secondsPerSlot"]
    by_slot = {}
    for block in blocks:
        timestamp = number(block["timestamp"])
        delta = timestamp - genesis_timestamp
        if delta < 0 or delta % seconds:
            raise RuntimeError("Block timestamp cannot be assigned to a PoS slot")
        slot = delta // seconds
        index = slot - anchor_slot
        if 0 <= index < total:
            if index in by_slot:
                raise RuntimeError("Multiple blocks in one PoS slot")
            by_slot[index] = block
    kinds = {}
    for record in submissions:
        tx_hash = record.get("hash")
        if tx_hash:
            key = tx_hash.lower()
            if key in kinds:
                raise RuntimeError(f"Duplicate submitted hash: {key}")
            kinds[key] = record["kind"]
    sent = defaultdict(lambda: Counter())
    lag_by_slot = defaultdict(list)
    for record in submissions:
        attempt = record.get("attemptUnixMillis") or record.get("actualUnixMillis")
        if attempt is None:
            raise RuntimeError("Submission record lacks a broadcast timestamp")
        index = (int(attempt) - anchor_unix_ms) // (seconds * 1000)
        if 0 <= index < total:
            sent[index][(record["kind"], "submitted")] += 1
            sent[index][(record["kind"], classify_submission(record))] += 1
            if record["kind"] == "normal" and record.get("plannedUnixMillis") is not None:
                lag_by_slot[index].append(int(attempt) - int(record["plannedUnixMillis"]))
    pool = {number(row["blockNumber"]): row for row in snapshots if row.get("blockNumber") is not None}
    rows = []
    for index in range(total):
        block = by_slot.get(index)
        row = {key: "" for key in FIELDS}
        row.update(group=group, client=client, phase=phase_for_slot(index, phase),
                   relativeSlotIndex=index, slotNumber=anchor_slot + index,
                   blockPresent=bool(block))
        if block:
            bn = number(block["number"])
            row.update(relativeBlockIndex=bn - anchor_block, blockNumber=bn,
                       blockHash=block["hash"], blockTimestamp=number(block["timestamp"]),
                       baseFeePerGasWei=number(block["baseFeePerGas"]) if block.get("baseFeePerGas") else "",
                       blockGasLimit=number(block["gasLimit"]), blockGasUsed=number(block["gasUsed"]),
                       totalIncludedTransactions=len(block["transactions"]))
        else:
            row.update(normalIncluded=0, attackIncluded=0, normalGasUsed=0, attackGasUsed=0,
                       normalPaidFeeWei=0, attackPaidFeeWei=0, normalPaidFeeEth=0,
                       attackPaidFeeEth=0, normalPriorityFeeWei=0, attackPriorityFeeWei=0,
                       normalBaseBurnWei=0, attackBaseBurnWei=0)
        prices = {"normal": [], "attack": [], "all": []}
        included_gas_prices = []
        gas_price_complete = True
        for kind in ("normal", "attack"):
            row[kind + "Included"] = 0
            row[kind + "GasUsed"] = 0
            row[kind + "PaidFeeWei"] = 0
            row[kind + "PriorityFeeWei"] = 0
            row[kind + "BaseBurnWei"] = 0
            for status in ("submitted", "admitted", "rejected", "unknown"):
                row[kind + status.capitalize()] = sent[index][(kind, status)]
        if block:
            total_receipt_gas = 0
            for tx in block["transactions"]:
                key = tx["hash"].lower()
                receipt = receipts.get(key)
                if not receipt:
                    raise RuntimeError(f"Missing receipt for included transaction {key}")
                if receipt["blockHash"].lower() != block["hash"].lower():
                    raise RuntimeError(f"Noncanonical receipt for {key}")
                gas_used = number(receipt["gasUsed"])
                effective = number(receipt["effectiveGasPrice"])
                fee = gas_used * effective
                total_receipt_gas += gas_used
                prices["all"].append(effective)
                if tx.get("gasPrice") is None:
                    gas_price_complete = False
                else:
                    offered = number(tx["gasPrice"])
                    included_gas_prices.append(offered)
                    if number(tx.get("type", "0x0")) == 0 and offered != effective:
                        raise RuntimeError(f"Legacy gasPrice differs from effective receipt price for {key}")
                kind = kinds.get(key)
                if kind in ("normal", "attack"):
                    row[kind + "Included"] += 1
                    row[kind + "GasUsed"] += gas_used
                    row[kind + "PaidFeeWei"] += fee
                    prices[kind].append(effective)
                    if row["baseFeePerGasWei"] != "":
                        burned = gas_used * min(effective, row["baseFeePerGasWei"])
                        row[kind + "BaseBurnWei"] += burned
                        row[kind + "PriorityFeeWei"] += fee - burned
                    else:
                        row[kind + "BaseBurnWei"] = ""
                        row[kind + "PriorityFeeWei"] = ""
            if total_receipt_gas != row["blockGasUsed"]:
                raise RuntimeError(f"Receipt gas sum differs from block header at {row['blockNumber']}")
        for kind in ("normal", "attack"):
            row[kind + "PaidFeeEth"] = row[kind + "PaidFeeWei"] / ETH
            row.update(price_fields(kind + "EffectiveGasPrice", prices[kind]))
        lag = lag_by_slot[index]
        row["normalBroadcastLagMeanMs"] = round(statistics.mean(lag)) if lag else ""
        row["normalBroadcastLagP90Ms"] = quantile(lag, .9)
        row["normalBroadcastLateCount"] = sum(value > 1000 for value in lag)
        if block and number(block["number"]) in pool:
            snap = pool[number(block["number"])]
            row["poolPending"] = snap.get("pending", "")
            row["poolQueued"] = snap.get("queued", "")
            row["poolSnapshotUnixMillis"] = snap.get("unixMillis", "")
            row["poolSnapshotError"] = snap.get("error", "")
        else:
            row["poolSnapshotError"] = "NO_SNAPSHOT" if block else "MISSED_SLOT"
        if not block:
            row["spamEstimateStatus"] = "NO_BLOCK"
        elif not prices["all"]:
            row["spamEstimateStatus"] = "EMPTY_BLOCK"
        else:
            row["maxIncludedEffectiveGasPriceWei"] = max(prices["all"])
            if gas_price_complete:
                row["maxIncludedGasPriceWei"] = max(included_gas_prices)
            # MPFUZZ Appendix D.1 style estimate, not observed attack expenditure.
            if group == "G0" and gas_price_complete:
                estimate = row["maxIncludedGasPriceWei"] * row["blockGasLimit"]
                row["estimatedConventionalSpamCostWei"] = estimate
                row["estimatedConventionalSpamCostEth"] = estimate / ETH
                row["spamEstimateStatus"] = "ESTIMATED_FROM_G0_BLOCK"
            elif group == "G0":
                row["spamEstimateStatus"] = "MISSING_INCLUDED_GAS_PRICE"
            else:
                row["spamEstimateStatus"] = "G0_ONLY"
        rows.append(row)
    return rows


def collect(rpc, folder: Path, config: dict, client: str, group: str,
            genesis_timestamp: int, anchor_block: int, anchor_slot: int,
            anchor_unix_ms: int, end_block: int):
    folder = Path(folder)
    normal = jsonl(folder / "normal.jsonl")
    attack = jsonl(folder / "attack.jsonl")
    submissions = normal + attack
    blocks = [rpc.call("eth_getBlockByNumber", [hex(n), True])
              for n in range(anchor_block, end_block + 1)]
    if any(block is None for block in blocks):
        raise RuntimeError("Missing canonical block during Phase 2 collection")
    receipt_by_hash = {}
    for block in blocks:
        hashes = [tx["hash"] for tx in block["transactions"]]
        for offset in range(0, len(hashes), 100):
            batch = hashes[offset:offset + 100]
            results = rpc.batch("eth_getTransactionReceipt", [[h] for h in batch])
            for tx_hash, receipt in zip(batch, results):
                if receipt is None:
                    raise RuntimeError(f"Missing receipt for included tx {tx_hash}")
                receipt_by_hash[tx_hash.lower()] = receipt
    snapshots = jsonl(folder / "pool_snapshots.jsonl")
    rows = summarize_slots(config, client, group, genesis_timestamp,
                           anchor_block, anchor_slot, anchor_unix_ms,
                           blocks, receipt_by_hash, submissions, snapshots)
    write_jsonl(folder / "blocks.jsonl", blocks)
    write_jsonl(folder / "block_receipts.jsonl", list(receipt_by_hash.values()))
    with (folder / "per_block_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    outcomes = []
    for record in submissions:
        tx_hash = record.get("hash")
        receipt = receipt_by_hash.get(tx_hash.lower()) if tx_hash else None
        outcomes.append(dict(**record, receipt=receipt, observedThroughBlock=end_block))
    write_jsonl(folder / "transaction_outcomes.jsonl", outcomes)
    horizon_ms = (config["phase2"]["preSlots"] + config["phase2"]["attackSlots"] +
                  config["phase2"]["postSlots"]) * config["secondsPerSlot"] * 1000
    cutoff_ms = anchor_unix_ms + horizon_ms
    summary = dict(client=client, group=group, anchorBlock=anchor_block, anchorSlot=anchor_slot,
                   anchorUnixMillis=anchor_unix_ms, observedThroughBlock=end_block,
                   totalScheduledNormal=sum(1 for _ in jsonl(folder / "normal_schedule.jsonl")),
                   normalAttempts=len(normal), attackAttempts=len(attack),
                   normalOutsideObservation=sum(int(r.get("attemptUnixMillis", r.get("actualUnixMillis", 0))) >= cutoff_ms for r in normal),
                   attackOutsideObservation=sum(int(r.get("attemptUnixMillis", r.get("actualUnixMillis", 0))) >= cutoff_ms for r in attack),
                   normalAdmitted=sum(classify_submission(r) == "admitted" for r in normal),
                   normalRejected=sum(classify_submission(r) == "rejected" for r in normal),
                   attackAdmitted=sum(classify_submission(r) == "admitted" for r in attack),
                   attackRejected=sum(classify_submission(r) == "rejected" for r in attack),
                   normalIncluded=sum(row["normalIncluded"] for row in rows),
                   attackIncluded=sum(row["attackIncluded"] for row in rows),
                   normalPaidFeeWei=str(sum(row["normalPaidFeeWei"] for row in rows)),
                   attackPaidFeeWei=str(sum(row["attackPaidFeeWei"] for row in rows)),
                   missedSlots=sum(not row["blockPresent"] for row in rows),
                   normalUnknown=sum(classify_submission(row) == "unknown" for row in normal),
                   attackUnknown=sum(classify_submission(row) == "unknown" for row in attack),
                   spamBaselineAvailableSlots=sum(row["spamEstimateStatus"] == "ESTIMATED_FROM_G0_BLOCK" for row in rows))
    (folder / "phase2_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
