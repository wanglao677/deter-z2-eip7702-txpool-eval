"""Evidence-conservative Erigon 3.5.0 transaction classifier and Phase 1 report."""
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

BESU = Path(__file__).resolve().parents[1] / "besu26_layertrace"
sys.path.insert(0, str(BESU))
from normal_fee import bins as fee_bins, statistics as fee_statistics

HASH = re.compile(r"\b(?:idHash|IdHash)[=:\s]+(?:0x)?([0-9a-fA-F]{64})", re.I)
LAYER = re.compile(r"(?:added to|removed from) subpool (Pending|BaseFee|Queued)\b", re.I)
CAPACITY = {"pending sub-pool is full", "basefee sub-pool is full", "queued sub-pool is full"}
INVALID = ("insufficient funds", "nonce too low", "nonce gap too large", "queued sender dormant")
STATES = ("REJECTED", "POOL_EVICTED", "INCLUDED", "STATE_INVALIDATED",
          "PENDING", "BASEFEE", "QUEUED", "UNKNOWN")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def parse_trace(path):
    """Parse positive per-hash observations in source order from docker logs."""
    events = {}
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if "TX TRACING:" not in line:
                continue
            found = HASH.search(line)
            if not found:
                continue
            key = "0x" + found.group(1).lower()
            message = line.split("TX TRACING:", 1)[1]
            layer = LAYER.search(message)
            if "Inserted txn by nonce" in message:
                action = "INSERT"
            elif "Deleted txn by nonce" in message:
                action = "DELETE"
            elif "Replaced txn by nonce" in message:
                action = "REPLACE"
            elif "added to subpool" in message and layer:
                action = "ADD"
            elif "removed from subpool" in message and layer:
                action = "REMOVE"
            else:
                continue
            reason = None
            if action == "DELETE":
                suffix = re.search(r"\breason(?:=|:|\s+)(.+)$", message)
                if suffix:
                    reason = suffix.group(1).strip().strip('"').strip("'").strip()
            events.setdefault(key, []).append(dict(line=line_number, action=action,
                                                       layer=layer.group(1).upper() if layer else None,
                                                       reason=reason))
    return events


def trace_location(events):
    """Last explicit subpool move, provided no later deletion invalidated it."""
    location = None
    for event in events:
        if event["action"] == "ADD":
            location = event["layer"]
        elif event["action"] in ("REMOVE", "DELETE", "REPLACE"):
            location = None
    return location


def classify(record, receipt, events, checkpoint, trace_ok):
    """One mutually exclusive state at a checkpoint. Absence alone stays UNKNOWN."""
    if record.get("error") or not record.get("hash"):
        return "REJECTED", record.get("error") or "no hash returned", "submission"
    block = checkpoint["block"]
    if receipt and int(receipt["blockNumber"], 16) <= block:
        return "INCLUDED", "canonical receipt", "receipt"
    location = checkpoint.get("locations", {}).get(record["hash"].lower())
    if location in ("PENDING", "QUEUED"):
        return location, "txpool_content", "rpc"
    if not trace_ok:
        return "UNKNOWN", "trace unavailable", "none"
    if checkpoint.get("errors", {}).get("content"):
        return "UNKNOWN", "pool snapshot unavailable", "none"
    last = events[-1] if events else None
    admitted = any(e["action"] in ("INSERT", "ADD") for e in events)
    if last and last["action"] == "DELETE" and admitted:
        reason = (last.get("reason") or "").lower()
        if reason in CAPACITY:
            return "POOL_EVICTED", reason, "trace"
        if any(reason.startswith(prefix) for prefix in INVALID):
            return "STATE_INVALIDATED", reason, "trace"
    if admitted and trace_location(events) == "BASEFEE":
        return "BASEFEE", "last positive subpool trace", "trace"
    return "UNKNOWN", "no decisive evidence", "none"


def admitted(record, events, receipt, checkpoint):
    if record.get("error") or not record.get("hash"):
        return False
    key = record["hash"].lower()
    return bool((receipt and int(receipt["blockNumber"], 16) <= checkpoint["block"]) or
                key in checkpoint.get("locations", {}) or
                any(e["action"] in ("INSERT", "ADD") for e in events))


def fee_row(record, receipt):
    gas_used = int(receipt["gasUsed"], 16)
    effective = int(receipt["effectiveGasPrice"], 16)
    return dict(kind=record["kind"], label=record["label"], hash=record["hash"],
                configuredGasPriceWei=str(record["gasPriceWei"]),
                effectiveGasPriceWei=str(effective), gasUsed=gas_used,
                actualPaidFeeWei=str(gas_used * effective),
                receiptStatus=receipt["status"], blockNumber=int(receipt["blockNumber"], 16))


def _write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize_group(folder):
    folder = Path(folder)
    config = load_json(folder.parent / "config.json")
    workload = load_json(folder / "workload_manifest.json")
    records = []
    for kind in ("normal", "attack"):
        path = folder / (kind + ".jsonl")
        if path.exists():
            records += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    expected_normal = config["normalSenders"] * config["txsPerSender"]
    expected_attack = config["attackSenders"] * config["txsPerSender"] if workload["mode"] == "attack" else 0
    if sum(r["kind"] == "normal" for r in records) != expected_normal or sum(r["kind"] == "attack" for r in records) != expected_attack:
        raise RuntimeError("workload attempt count differs from locked config")
    receipt_rows = load_json(folder / "receipts.json")
    if len(receipt_rows) != len(records):
        raise RuntimeError("receipt/attempt conservation failed")
    receipts = {(r["kind"], r["label"]): r.get("receipt") for r in receipt_rows}
    if len(receipts) != len(records):
        raise RuntimeError("duplicate workload label")
    fee_plan = load_json(folder / "normal_fee_plan.json")
    with (folder / "normal_fee_plan.csv").open(newline="", encoding="utf-8") as handle:
        fee_csv = list(csv.DictReader(handle))
    planned = {r["label"]: int(r["gasPriceWei"]) for r in fee_csv}
    if len(planned) != config["normalSenders"] * config["txsPerSender"]:
        raise RuntimeError("normal fee plan incomplete")
    if any(int(r["gasPriceWei"]) != planned[r["label"]] for r in records if r["kind"] == "normal"):
        raise RuntimeError("broadcast normal prices differ from fee plan")
    fee_rows = [fee_row(r, receipts[(r["kind"], r["label"])]) for r in records
                if receipts[(r["kind"], r["label"])]]
    _write_csv(folder / "fee_receipts.csv", fee_rows,
               ("kind", "label", "hash", "configuredGasPriceWei", "effectiveGasPriceWei",
                "gasUsed", "actualPaidFeeWei", "receiptStatus", "blockNumber"))
    windows = [config["commonWindowBlocks"], config["fullWindowBlocks"], config["followupWindowBlocks"]]
    names = ["T0", "T1"] + (["T2", "T3"] if workload["mode"] == "attack" else ["G0_end"]) + ["window" + str(w) for w in windows]
    result = dict(client="erigon", version=workload["clientVersion"], group=workload["group"],
                  mode=workload["mode"], delegateCase=workload["delegateCase"],
                  configName=config["name"], feePlanSha256=fee_plan["sequenceSha256"],
                  normalConfiguredGasPrice=fee_statistics([int(x["gasPriceWei"]) for x in fee_csv]),
                  windows={}, checkpoints={}, phaseMarkers={},
                  broadcastTimings={"normal": load_json(folder / "normal_timing.json") if (folder / "normal_timing.json").exists() else None,
                                    "attack": load_json(folder / "attack_timing.json") if (folder / "attack_timing.json").exists() else None},
                  setupFeeByStageWei=load_json(folder / "summary.json")["setupFeeByStageWei"],
                  receiverBalanceDeltaWei=load_json(folder / "summary.json")["receiverBalanceDeltaWei"],
                  limitations=["Erigon 3.5.0 JSON-RPC omits BaseFee global count; inferred cohort BaseFee needs positive trace.",
                               "Pool absence without deletion trace is UNKNOWN."])
    for name in names:
        checkpoint = load_json(folder / ("pool_" + name + ".json"))
        path = folder / checkpoint["traceFile"]
        trace_ok = (checkpoint["traceComplete"] and path.exists() and
                    hashlib.sha256(path.read_bytes()).hexdigest() == checkpoint["traceSha256"])
        trace = parse_trace(path) if trace_ok else {}
        result["phaseMarkers"][name] = dict(block=checkpoint["block"], startedUnixMillis=checkpoint.get("startedUnixMillis"),
                                            finishedUnixMillis=checkpoint.get("finishedUnixMillis"))
        result["checkpoints"][name] = dict(block=checkpoint["block"], counts=checkpoint["counts"],
                                           traceComplete=trace_ok, snapshotStable=checkpoint.get("snapshotStable"),
                                           errors=checkpoint["errors"],
                                           observedWorkloadTraceHashes=sum(bool(r.get("hash") and
                                               r["hash"].lower() in trace) for r in records))
        if name not in ("T0", "T1", "T2", "T3", "G0_end"):
            window = int(name.removeprefix("window"))
            marker = load_json(folder / (name + "_marker.json"))
            state_rows = []
            for record in records:
                receipt = receipts[(record["kind"], record["label"])]
                tx_events = trace.get((record.get("hash") or "").lower(), [])
                state, reason, provenance = classify(record, receipt, tx_events, checkpoint, trace_ok)
                state_rows.append(dict(kind=record["kind"], label=record["label"],
                                       hash=record.get("hash"), nonce=record["nonce"],
                                       configuredGasPriceWei=str(record["gasPriceWei"]),
                                       state=state, reason=reason, provenance=provenance,
                                       admitted=admitted(record, tx_events, receipt, checkpoint)))
            counts = {}
            for kind in ("normal", "attack"):
                cohort = [r for r in state_rows if r["kind"] == kind]
                by_state = Counter(r["state"] for r in cohort)
                if sum(by_state.values()) != len(cohort):
                    raise RuntimeError("state conservation failed")
                admit_count = sum(r["admitted"] for r in cohort)
                evicted = by_state["POOL_EVICTED"]
                paid = [x for x in fee_rows if x["kind"] == kind and x["blockNumber"] <= checkpoint["block"]]
                counts[kind] = dict(attempted=len(cohort), admitted=admit_count,
                                    accepted=sum(bool(r.get("hash")) and not r.get("error") for r in records if r["kind"] == kind),
                                    rejected=by_state["REJECTED"],
                                    states={state: by_state[state] for state in STATES},
                                    capacityEvictionRate=(evicted / admit_count if admit_count else "UNAVAILABLE"),
                                    totalGasUsed=sum(x["gasUsed"] for x in paid),
                                    actualPaidFeeWei=str(sum(int(x["actualPaidFeeWei"]) for x in paid)),
                                    effectiveGasPriceWeiMean=(str(sum(int(x["effectiveGasPriceWei"]) for x in paid) // len(paid))
                                                              if paid else "UNAVAILABLE"))
            if not marker["comparable"] or not checkpoint.get("snapshotStable", False):
                counts["windowComparable"] = False
                counts["note"] = "Observed after target block or snapshot crossed blocks; rate cannot be used for paired comparison"
            else:
                counts["windowComparable"] = True
            if workload["mode"] == "attack":
                first = [r for r in state_rows if r["kind"] == "attack" and r["label"].endswith("-x1")]
                tail = [r for r in state_rows if r["kind"] == "attack" and not r["label"].endswith("-x1")]
                counts["attackFirstIncluded"] = sum(r["state"] == "INCLUDED" for r in first)
                counts["attackTailIncluded"] = sum(r["state"] == "INCLUDED" for r in tail)
            edges = fee_bins(config)
            fee_breakdown = []
            for edge in edges:
                cohort = [r for r in state_rows if r["kind"] == "normal" and
                          edge["lowerWei"] <= int(r["configuredGasPriceWei"]) < edge["upperExclusiveWei"]]
                denom = sum(r["admitted"] for r in cohort)
                evicted = sum(r["state"] == "POOL_EVICTED" for r in cohort)
                fee_breakdown.append(dict(**edge, attempted=len(cohort), admitted=denom,
                                          confirmedCapacityEvicted=evicted,
                                          unknown=sum(r["state"] == "UNKNOWN" for r in cohort),
                                          evictionRate=(evicted / denom if denom else "UNAVAILABLE")))
            counts["normalFeeBins"] = fee_breakdown
            result["windows"][str(window)] = counts
            _write_csv(folder / ("tx_states_" + name + ".csv"), state_rows,
                       ("kind", "label", "hash", "nonce", "configuredGasPriceWei",
                        "state", "reason", "provenance", "admitted"))
    first_receipts = [r for r in fee_rows if r["kind"] == "attack" and r["label"].endswith("-x1")]
    first_block = min((r["blockNumber"] for r in first_receipts), default=None)
    result["T4"] = dict(firstAttackIncludedBlock=first_block,
                        poolCounts="UNAVAILABLE" if first_block is not None else None)
    result["phaseMarkers"]["T4"] = result["T4"]
    if (folder / "T2_marker.json").exists():
        result["phaseMarkers"]["T2"] = load_json(folder / "T2_marker.json")
    result["phaseMarkers"]["T5"] = result["phaseMarkers"]["window" + str(config["fullWindowBlocks"])]
    result["phaseMarkers"]["T6"] = result["phaseMarkers"]["window" + str(config["followupWindowBlocks"])]
    result["preFirstInclusionCapacityEvictionRate"] = "UNAVAILABLE"
    if first_block is not None:
        t3 = load_json(folder / "pool_T3.json")
        if t3["block"] < first_block:
            # T3 is a measured pre-first checkpoint only if the first inclusion happens later.
            trace_ok = t3["traceComplete"] and hashlib.sha256((folder / t3["traceFile"]).read_bytes()).hexdigest() == t3["traceSha256"]
            events = parse_trace(folder / t3["traceFile"]) if trace_ok else {}
            normal = [r for r in records if r["kind"] == "normal"]
            pre = [(r, events.get((r.get("hash") or "").lower(), []),
                    receipts[(r["kind"], r["label"])]) for r in normal]
            denom = sum(admitted(r, e, receipt, t3) for r, e, receipt in pre)
            numer = sum(classify(r, receipt, e, t3, trace_ok)[0] == "POOL_EVICTED"
                        for r, e, receipt in pre)
            result["preFirstInclusionCapacityEvictionRate"] = numer / denom if denom else "UNAVAILABLE"
            result["preFirstCheckpoint"] = "T3"
    result["finalConfirmedCapacityEvictionRate"] = result["windows"][str(config["followupWindowBlocks"])]["normal"]["capacityEvictionRate"]
    result["attackBroadcastOverranWindow"] = load_json(folder / "summary.json")["attackBroadcastOverranWindow"]
    t1 = result["checkpoints"]["T1"]
    result["T1SnapshotAtAnchor"] = (t1["block"] == load_json(folder / "summary.json")["windowAnchorBlock"]
                                    and t1["snapshotStable"])
    if not result["T1SnapshotAtAnchor"]:
        result["limitations"].append("T1 pool snapshot missed the normal-broadcast anchor block.")
        for window_result in result["windows"].values():
            window_result["windowComparable"] = False
            window_result["note"] = "T1 pool snapshot missed anchor block"

    save_json(folder / "erigon_summary.json", result)
    return result


def summarize_evaluation(root):
    root = Path(root)
    groups = {group: load_json(root / group / "erigon_summary.json") for group in ("G0", "G1", "G2")
              if (root / group / "erigon_summary.json").exists()}
    hashes = {g["feePlanSha256"] for g in groups.values()}
    if len(hashes) > 1:
        raise RuntimeError("G0/G1/G2 normal fee sequences differ")
    window = str(load_json(root / "config.json")["fullWindowBlocks"])
    comparison = {}
    for label, item in groups.items():
        row = item["windows"][window]
        comparison[label] = dict(normalAdmitted=row["normal"]["admitted"],
                                 normalCapacityEvicted=row["normal"]["states"]["POOL_EVICTED"],
                                 normalIncluded=row["normal"]["states"]["INCLUDED"],
                                 normalUnknown=row["normal"]["states"]["UNKNOWN"],
                                 attackAccepted=row["attack"]["accepted"],
                                 attackRejected=row["attack"]["rejected"],
                                 attackFirstIncluded=row.get("attackFirstIncluded", 0),
                                 attackTailIncluded=row.get("attackTailIncluded", 0),
                                 attackRemaining=sum(row["attack"]["states"][x] for x in ("PENDING", "BASEFEE", "QUEUED")),
                                 attackWorkloadGasUsed=row["attack"]["totalGasUsed"],
                                 attackWorkloadGasCostWei=row["attack"]["actualPaidFeeWei"],
                                 setupGasCostWei=str(sum(int(v) for v in item["setupFeeByStageWei"].values())),
                                 deploymentGasCostWei=item["setupFeeByStageWei"].get("deploy", "UNAVAILABLE"),
                                 delegationSetupGasCostWei=item["setupFeeByStageWei"].get("authorize", "UNAVAILABLE"),
                                 comparable=row["windowComparable"] and not item["attackBroadcastOverranWindow"])
    result = dict(client="erigon", groups=comparison,
                  sameNormalFeeSequence=len(hashes) <= 1,
                  drainCostComparisonValid=all(g in comparison and comparison[g]["comparable"] for g in ("G1", "G2")))
    save_json(root / "erigon_evaluation_summary.json", result)
    return result
