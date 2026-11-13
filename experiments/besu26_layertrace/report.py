#!/usr/bin/env python3
"""Correlate native layer evidence, canonical receipts and timing for Phase 1."""
import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from audit import csv_write


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def phase_at(unix_ms, normal_timing, attack_timing):
    try:
        t = int(unix_ms)
    except (ValueError, TypeError):
        return "unknown"
    normal_start = normal_timing["before"]["unixMillis"]
    normal_end = normal_timing["afterUnixMillis"]
    if t < normal_start:
        return "before_normal"
    if t <= normal_end:
        return "normal_broadcast"
    if attack_timing:
        attack_start = attack_timing["before"]["unixMillis"]
        attack_end = attack_timing["afterUnixMillis"]
        if t < attack_start:
            return "between_normal_attack"
        if t <= attack_end:
            return "attack_broadcast"
        return "after_attack"
    return "after_normal"


def receipt_fee(receipt):
    return int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)


def classify(record, state, receipt, capacity_hashes, trusted):
    if receipt:
        return "CONFIRMATION"
    if record.get("error") or not record.get("hash"):
        return "REJECTION"
    if not trusted:
        return "UNKNOWN"
    observed = state.get("observedState", "UNOBSERVED")
    if observed == "POOL_DROPPED" and record["hash"].lower() in capacity_hashes:
        return "CAPACITY_EVICTION"
    if observed.startswith("REMOVED_INVALIDATED"):
        return "INVALIDATION"
    if observed == "IN_LAYER":
        return "IN_POOL"
    if observed.startswith("REMOVED_REJECTED"):
        return "REJECTION"
    if observed.startswith("REMOVED_") and observed != "REMOVED_CONFIRMED":
        return "OTHER_REMOVAL"
    return "UNKNOWN"


def summarize_group(folder):
    folder = Path(folder)
    workload = read_json(folder / "workload_manifest.json")
    config = workload["config"]
    audit = read_json(folder / "layer_audit.json")
    summary = read_json(folder / "summary.json")
    records = read_jsonl(folder / "normal.jsonl") + read_jsonl(folder / "attack.jsonl")
    states = {row["hash"].lower(): row for row in read_csv(folder / "tx_layers.csv")}
    capacity = {row["hash"].lower() for row in read_csv(folder / "capacity_choices.csv") if row.get("hash")}
    receipts = {row["hash"].lower(): row["receipt"] for row in read_json(folder / "receipts.json")}
    normal_timing = read_json(folder / "normal_timing.json")
    attack_timing = read_json(folder / "attack_timing.json") if (folder / "attack_timing.json").exists() else None
    windows = {str(i): read_json(folder / f"window{i}_marker.json")
               for i in (config["commonWindowBlocks"], config["fullWindowBlocks"])}
    full_marker = windows[str(config["fullWindowBlocks"])]
    full_target = full_marker["targetBlock"]
    trusted = audit["evidenceComplete"] and audit["checkpoint"].get("unixMillis", 0) >= full_marker["unixMillis"]
    trace_issues = list(audit["issues"])
    if not trusted and audit["evidenceComplete"]:
        trace_issues.append("stale_trace_checkpoint")
    history = read_csv(folder / "layer_history.csv")
    normal_hashes = {r["hash"].lower() for r in records if r["kind"] == "normal" and r.get("hash")}
    ever_layers = defaultdict(set)
    layer_events = []
    for event in history:
        tx_hash = event.get("hash", "").lower()
        if tx_hash not in normal_hashes:
            continue
        if event.get("event") == "enter":
            ever_layers[event.get("layer", "")].add(tx_hash)
        layer_events.append(dict(hash=tx_hash, seq=event.get("seq", ""),
            unixMillis=event.get("unixMillis", ""), phase=phase_at(event.get("unixMillis"), normal_timing, attack_timing),
            event=event.get("event", ""), layer=event.get("layer", ""), targetLayer=event.get("targetLayer", ""),
            reason=event.get("reason", ""), scope=event.get("scope", ""),
            poolBlockNumber=event.get("poolBlockNumber", "")))
    csv_write(folder / "normal_layer_events.csv", layer_events)
    phase_counts = Counter((e["phase"], e["event"], e["layer"]) for e in layer_events)
    phase_rows = [dict(phase=phase, event=event, layer=layer, count=count)
                  for (phase, event, layer), count in sorted(phase_counts.items())]
    csv_write(folder / "normal_layer_phase_summary.csv", phase_rows)
    outcome_counts = defaultdict(Counter)
    final_layers = defaultdict(Counter)
    attack_roles = defaultdict(lambda: dict(included=0, includedWithinFullWindow=0,
        successful=0, failed=0, feeWei=0, feeWeiWithinFullWindow=0, gasUsed=[]))
    tx_rows, attack_receipts = [], []
    first_attack_blocks = []
    normal_drop_times = []
    normal_drop_phases = Counter()
    unattributed_drops = Counter()
    for record in records:
        tx_hash = record.get("hash", "").lower()
        kind = record["kind"]
        role = ("first" if record["label"].endswith("-x1") else "tail") if kind == "attack" else "normal"
        state = states.get(tx_hash, {})
        receipt = receipts.get(tx_hash)
        outcome = classify(record, state, receipt, capacity, trusted)
        outcome_counts[kind][outcome] += 1
        if outcome == "IN_POOL":
            final_layers[kind][state.get("layer", "")] += 1
        if state.get("observedState") == "POOL_DROPPED" and outcome != "CAPACITY_EVICTION":
            unattributed_drops[kind] += 1
        block = int(receipt["blockNumber"], 16) if receipt else None
        fee = receipt_fee(receipt) if receipt else 0
        included_in_window = block is not None and block <= full_target
        if kind == "attack" and receipt:
            stats = attack_roles[role]
            stats["included"] += 1
            stats["successful" if receipt["status"] == "0x1" else "failed"] += 1
            stats["feeWei"] += fee
            if included_in_window:
                stats["includedWithinFullWindow"] += 1
                stats["feeWeiWithinFullWindow"] += fee
            stats["gasUsed"].append(int(receipt["gasUsed"], 16))
            first_attack_blocks.append(block)
        if kind == "normal" and outcome == "CAPACITY_EVICTION":
            t = state.get("lastUnixMillis", "")
            normal_drop_times.append(dict(hash=tx_hash, unixMillis=t,
                phase=phase_at(t, normal_timing, attack_timing), lastReason=state.get("lastReason", "")))
            normal_drop_phases[phase_at(t, normal_timing, attack_timing)] += 1
        row = dict(kind=kind, role=role, label=record["label"], sender=record.get("sender", ""),
            nonce=record.get("nonce", ""), hash=record.get("hash", ""),
            calldataPaddingBytes=record.get("calldataPaddingBytes", ""),
            observedState=state.get("observedState", "UNOBSERVED"), layer=state.get("layer", ""),
            layerHistory=state.get("history", ""), lastReason=state.get("lastReason", ""),
            lastUnixMillis=state.get("lastUnixMillis", ""), outcome=outcome,
            receiptStatus=receipt["status"] if receipt else "",
            receiptBlock=block if block is not None else "",
            includedWithinFullWindow=included_in_window, gasUsed=int(receipt["gasUsed"], 16) if receipt else "",
            effectiveGasPriceWei=int(receipt["effectiveGasPrice"], 16) if receipt else "",
            feeWei=fee if receipt else "")
        tx_rows.append(row)
        if kind == "attack" and receipt:
            attack_receipts.append(row)
    csv_write(folder / "tx_outcomes.csv", tx_rows)
    csv_write(folder / "attack_receipts.csv", attack_receipts)
    csv_write(folder / "normal_capacity_drops.csv", normal_drop_times)
    balances = read_json(folder / "first_attack_balances.json")
    csv_write(folder / "first_attack_balances.csv", balances)
    role_summary = {}
    for role in ("first", "tail"):
        stats = attack_roles[role]
        gas = stats.pop("gasUsed")
        stats["feeWei"] = str(stats["feeWei"])
        stats["feeWeiWithinFullWindow"] = str(stats["feeWeiWithinFullWindow"])
        stats["meanGasUsed"] = sum(gas) / len(gas) if gas else None
        role_summary[role] = stats
    normal_count = outcome_counts["normal"]["CAPACITY_EVICTION"]
    normal_within_window = sum(int(row["unixMillis"]) <= full_marker["unixMillis"]
                               for row in normal_drop_times if row["unixMillis"])
    result = dict(group=workload.get("group"), mode=workload["mode"], delegateCase=workload["delegateCase"],
        seed=workload["seed"], configName=config["name"], normalSubmitted=sum(r["kind"] == "normal" for r in records),
        normalRpcAccepted=sum(r["kind"] == "normal" and bool(r.get("hash")) and not r.get("error") for r in records),
        attackSubmitted=sum(r["kind"] == "attack" for r in records),
        attackRpcAccepted=sum(r["kind"] == "attack" and bool(r.get("hash")) and not r.get("error") for r in records),
        layerEvidenceComplete=trusted, layerIssues=trace_issues,
        traceCheckpoint=audit["checkpoint"], traceSha256=audit["sourceSha256"],
        outcomes={kind: dict(outcome_counts[kind]) for kind in ("normal", "attack")},
        normalCapacityEvictedConfirmed=normal_count if trusted else None,
        normalCapacityEvictedByFullWindow=normal_within_window if trusted else None,
        normalCapacityEvictedByPhase=dict(normal_drop_phases),
        unattributedPoolDrops=dict(unattributed_drops),
        finalLayerCounts={kind: dict(final_layers[kind]) for kind in ("normal", "attack")},
        normalEverEnteredLayer={layer: len(hashes) for layer, hashes in ever_layers.items()},
        normalLayerEventsByPhase=phase_rows,
        attackByRole=role_summary, firstAttackIncludedBlock=min(first_attack_blocks) if first_attack_blocks else None,
        firstAttackIncludedPoint=summary.get("firstAttackIncludedPoint"),
        setupFeeWei=summary["setupFeeWei"], setupFeeByStageWei=summary["setupFeeByStageWei"],
        attackerSetupFeeWei=str(sum(int(summary["setupFeeByStageWei"].get(stage, 0))
            for stage in ("deploy", "fund_attack", "authorize"))),
        normalSetupFeeWei=summary["setupFeeByStageWei"].get("fund_normal", "0"),
        receiverBalanceDeltaWei=summary["receiverBalanceDeltaWei"],
        fullWindowTargetBlock=full_target, windows=windows, windowSummary=summary["windows"],
        normalTiming=normal_timing, attackTiming=attack_timing,
        firstAttackBalances=balances)
    (folder / "group_summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def summarize_evaluation(root):
    root = Path(root)
    groups = {}
    for group in ("G0", "G1", "G2"):
        path = root / group / "group_summary.json"
        if path.exists():
            groups[group] = read_json(path)
    rows = []
    for group, result in groups.items():
        full = str(read_json(root / "config.json")["fullWindowBlocks"])
        window = result["windowSummary"][full]
        rows.append(dict(group=group, seed=result["seed"], evidenceComplete=result["layerEvidenceComplete"],
            normalSubmitted=result["normalSubmitted"], normalRpcAccepted=result["normalRpcAccepted"],
            normalCapacityEvictedConfirmed=result["normalCapacityEvictedConfirmed"],
            normalCapacityEvictedByFullWindow=result["normalCapacityEvictedByFullWindow"],
            normalIncludedWithinWindow=window["normal"]["included"], attackSubmitted=result["attackSubmitted"],
            attackRpcAccepted=result["attackRpcAccepted"],
            firstAttackIncluded=result["attackByRole"]["first"]["includedWithinFullWindow"],
            tailAttackIncluded=result["attackByRole"]["tail"]["includedWithinFullWindow"],
            attackReceiptSuccess=window["attack"]["successful"], attackReceiptFailure=window["attack"]["failed"],
            attackGasFeeWei=window["attack"]["feeWei"], setupGasFeeWei=result["setupFeeWei"],
            attackerSetupGasFeeWei=result["attackerSetupFeeWei"],
            receiverBalanceDeltaWei=result["receiverBalanceDeltaWei"],
            firstAttackIncludedBlock=result["firstAttackIncludedBlock"]))
    csv_write(root / "evaluation_summary.csv", rows)
    (root / "evaluation_summary.json").write_text(json.dumps(dict(groups=groups, table=rows), indent=2) + "\n", encoding="utf-8")
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Evaluation run directory")
    parser.add_argument("--refresh", action="store_true", help="Rebuild each available group summary from audited evidence")
    args = parser.parse_args()
    if args.refresh:
        for group in ("G0", "G1", "G2"):
            folder = args.root / group
            if (folder / "layer_audit.json").exists() and (folder / "summary.json").exists():
                summarize_group(folder)
    print(json.dumps(summarize_evaluation(args.root), indent=2))
