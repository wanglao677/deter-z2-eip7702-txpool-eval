#!/usr/bin/env python3
"""Besu 配对实验：准备网络、运行负载、保全证据和生成中文统计。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from decimal import Decimal
from pathlib import Path

import audit_phased_tx_final_status as audit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROFILES = {
    "cap10": dict(pool="SEQUENCED", capacity=10, normalSenders=5, normalRate=8,
                  attackSenders=5, attackRate=20, phases=[1, 1, 2, 2, 3], slot=4, chainId=13371371),
    "cap100": dict(pool="SEQUENCED", capacity=100, normalSenders=50, normalRate=80,
                   attackSenders=50, attackRate=200, phases=[1, 1, 2, 2, 3], slot=4, chainId=13371372),
    "sequenced4096": dict(pool="SEQUENCED", capacity=4096, normalSenders=100, normalRate=800,
                          attackSenders=512, attackRate=4096, phases=[2, 4, 4, 4, 10], slot=30, chainId=13371373),
    "default": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                    attackSenders=512, attackRate=4096, phases=[2, 4, 4, 4, 10], slot=30, chainId=13371374),
    "default-v2": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                       attackSenders=1000, attackRate=8000, phases=[2, 4, 4, 4, 10], slot=30,
                       chainId=13371375),
    "default-v3": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                       attackSenders=2000, attackRate=16000, phases=[2, 4, 4, 4, 10], slot=30,
                       chainId=13371376),
    "default-v4": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                       normalVictimSenders=3200, recoveryNormalRate=0,
                       attackSenders=4000, attackRate=32000, phases=[2, 4, 4, 4, 20], slot=30,
                       chainId=13371377, batchSize=180),
    "default-v4-layered-2m": dict(pool="LAYERED", capacity=None, layerMaxCapacity=2_000_000,
                                  normalSenders=100, normalRate=800,
                                  normalVictimSenders=3200, recoveryNormalRate=0,
                                  attackSenders=1000, attackRate=8000,
                                  phases=[2, 4, 4, 4, 20], slot=30,
                                  chainId=13371379, batchSize=180, runTimeout=21600),
    "default-v4-layered-2m-a500": dict(pool="LAYERED", capacity=None, layerMaxCapacity=2_000_000,
                                       normalSenders=100, normalRate=800,
                                       normalVictimSenders=3200, recoveryNormalRate=0,
                                       attackSenders=500, attackRate=4000,
                                       phases=[2, 4, 4, 4, 20], slot=30,
                                       chainId=13371380, batchSize=180, runTimeout=21600),
    "default-v4-layered-2m-a750": dict(pool="LAYERED", capacity=None, layerMaxCapacity=2_000_000,
                                       normalSenders=100, normalRate=800,
                                       normalVictimSenders=3200, recoveryNormalRate=0,
                                       attackSenders=750, attackRate=6000,
                                       phases=[2, 4, 4, 4, 20], slot=30,
                                       chainId=13371381, batchSize=180, runTimeout=21600),
    "default-v4-layered-2m-a875": dict(pool="LAYERED", capacity=None, layerMaxCapacity=2_000_000,
                                       normalSenders=100, normalRate=800,
                                       normalVictimSenders=3200, recoveryNormalRate=0,
                                       attackSenders=875, attackRate=7000,
                                       phases=[2, 4, 4, 4, 20], slot=30,
                                       chainId=13371382, batchSize=180, runTimeout=21600),
    "default-v4-layered-2m-a950": dict(pool="LAYERED", capacity=None, layerMaxCapacity=2_000_000,
                                       normalSenders=100, normalRate=800,
                                       normalVictimSenders=3200, recoveryNormalRate=0,
                                       attackSenders=950, attackRate=7600,
                                       phases=[2, 4, 4, 4, 20], slot=30,
                                       chainId=13371383, batchSize=180, runTimeout=21600),
    "default-v5": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                       normalVictimSenders=3200, recoveryNormalRate=0,
                       attackSenders=8000, attackRate=64000, phases=[2, 4, 4, 4, 20], slot=30,
                       chainId=13371378, batchSize=120, runTimeout=21600),
    "default-v5-p16k": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                            normalVictimSenders=3200, recoveryNormalRate=0,
                            attackSenders=8000, attackRate=64000, phases=[2, 4, 4, 4, 20], slot=30,
                            tailPaddingBytes=16384, chainId=13371384, batchSize=120, runTimeout=21600),
    "default-v5-p24k": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                            normalVictimSenders=3200, recoveryNormalRate=0,
                            attackSenders=8000, attackRate=64000, phases=[2, 4, 4, 4, 20], slot=30,
                            tailPaddingBytes=24576, chainId=13371388, batchSize=120, runTimeout=21600),
    "default-v5-p16k-parallel": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                                     normalVictimSenders=3200, recoveryNormalRate=0,
                                     attackSenders=8000, attackRate=64000, phases=[2, 4, 4, 4, 20], slot=30,
                                     tailPaddingBytes=16384, attackHelperSendWorkers=64,
                                     chainId=13371386, batchSize=120, runTimeout=21600),
    "default-v5-p16k-fast": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                                 normalVictimSenders=3200, recoveryNormalRate=0,
                                 attackSenders=8000, attackRate=64000, phases=[2, 4, 4, 4, 20], slot=30,
                                 tailPaddingBytes=16384, attackHelperSendWorkers=64, attackPresign=True,
                                 chainId=13371387, batchSize=120, runTimeout=21600),
    "default-v5-total320k": dict(pool="LAYERED", capacity=None, normalSenders=100, normalRate=800,
                                 normalVictimSenders=3200, recoveryNormalRate=0,
                                 attackSenders=10000, attackRate=80000, phases=[2, 4, 4, 4, 20], slot=30,
                                 chainId=13371385, batchSize=100, runTimeout=28800),
}
PHASE_NAMES = {"warmup": "预热", "saturation": "攻击前负载", "control": "对照窗口",
               "attack_on": "攻击窗口", "recovery": "恢复", "drain": "排空",
               "after_window": "窗口结束后", "unknown": "区块未映射"}
STATES = {"INCLUDED_SUCCESS": "成功上链", "INCLUDED_FAILED": "上链执行失败",
          "PENDING_FINAL": "仍在待处理池", "QUEUED_FINAL": "仍在排队池",
          "DROPPED_OR_EVICTED": "未上链且池中缺失",
          "UNKNOWN_UNSTABLE_AUDIT": "审计跨区块，状态待复核",
          "REJECTED_OR_SEND_ERROR": "发送失败或拒绝"}
NORMAL_FINALITY_STATES = {
    "INCLUDED_SUCCESS": "成功上链",
    "INCLUDED_FAILED": "上链执行失败",
    "STILL_PENDING_AT_FINAL_AUDIT": "最终复核仍在 pending",
    "STILL_QUEUED_AT_FINAL_AUDIT": "最终复核仍在 queued",
    "ABSENT_NO_RECEIPT_CONFIRMED": "多轮复核确认无回执且池中缺失",
    "UNKNOWN_NEEDS_MORE_OBSERVATIONS": "仍需更多观察",
}


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path, rows, fields=None):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def gwei_to_wei(value):
    price = Decimal(value) * 10**9
    if not price.is_finite() or price <= 0 or price != price.to_integral_value():
        raise ValueError("费用必须是正数，且可以精确转换为整数 wei")
    return int(price)


def make_plan(profile, seed, first_gwei="7", tail_gwei="10"):
    defaults = dict(normalPriceWei=3_000_000_000, normalPayloadBytes=0,
                    firstPaddingBytes=0, tailPaddingBytes=8192,
                    image="hyperledger/besu:26.6.1",
                    package="github.com/ethpandaops/ethereum-package@6.1.0")
    plan = dict(defaults, **PROFILES[profile])
    plan.update(profile=profile, seed=seed,
                firstPriceWei=gwei_to_wei(first_gwei), tailPriceWei=gwei_to_wei(tail_gwei))
    recovery_rate = plan.get("recoveryNormalRate", plan["normalRate"])
    plan["normalTotal"] = (
        (plan["phases"][0] + plan["phases"][1] + plan["phases"][2]) * plan["normalRate"]
        + plan["phases"][3] * recovery_rate
    )
    plan["attackTotal"] = plan["phases"][2] * plan["attackRate"]
    return plan


def network_config(plan):
    participant = {"el_type": "besu", "el_image": plan["image"], "cl_type": "lighthouse", "count": 1}
    if plan["pool"] == "SEQUENCED":
        participant["el_extra_params"] = [
            "--tx-pool=SEQUENCED", f"--tx-pool-max-size={plan['capacity']}",
            "--tx-pool-limit-by-account-percentage=1.0", "--tx-pool-no-local-priority=true",
            "--tx-pool-min-gas-price=0", "--tx-pool-price-bump=1",
        ]
    elif plan["pool"] == "LAYERED" and plan.get("layerMaxCapacity"):
        participant["el_extra_params"] = [
            "--tx-pool=LAYERED", f"--tx-pool-layer-max-capacity={plan['layerMaxCapacity']}",
        ]
    return {"participants": [participant],
            "network_params": {"network_id": str(plan["chainId"]), "seconds_per_slot": plan["slot"]},
            "additional_services": ["dora"]}


def runner_command(plan, mode, rpc, enclave, out_dir, helper):
    phases = plan["phases"]
    values = {
        "mode": mode, "client": "besu", "rpc": rpc, "enclave": enclave,
        "trial-id": f"pool_{plan['profile']}_{plan['seed']}_{mode}",
        "seed": f"pool-pair-{plan['seed']}", "workload-seed": f"pool-pair-{plan['seed']}",
        "normal-senders": plan["normalSenders"], "normal-rate-per-block": plan["normalRate"],
        "normal-victim-senders": plan.get("normalVictimSenders", 0),
        "recovery-normal-rate-per-block": plan.get("recoveryNormalRate", plan["normalRate"]),
        "normal-rate-jitter": 0, "normal-calldata-bytes": plan["normalPayloadBytes"],
        "normal-gas": plan.get("normalGas", 21000),
        "normal-fund-eth": 1, "normal-gas-price-mode": "fixed", "normal-price": plan["normalPriceWei"],
        "price-unit": 1, "setup-price": 1_000_000_000,
        "warmup-blocks": phases[0], "saturation-blocks": phases[1], "attack-blocks": phases[2],
        "recovery-blocks": phases[3], "drain-blocks": phases[4],
        "batch-size": plan.get("batchSize", min(50, plan["capacity"] or 50)), "setup-wait": 300,
        "receipt-scope": "none", "final-receipt-timeout": 0, "fresh-block-timeout": 180,
        "txpool-classification": "none", "out-dir": out_dir, "helper-binary": helper,
        "normal-helper-send-workers": plan.get("normalHelperSendWorkers", 1),
        "attack-helper-send-workers": plan.get("attackHelperSendWorkers", 1),
    }
    if mode == "attack":
        values.update({"attack-senders": plan["attackSenders"], "attack-rate-per-block": plan["attackRate"],
                       "attack-sender-activation": "new-per-block",
                       "attack-calldata-padding-bytes": plan["tailPaddingBytes"],
                       "attack-first-calldata-padding-bytes": plan["firstPaddingBytes"],
                       "attack-gas": plan.get("attackGas", 500000),
                       "attack-balance-eth": 1, "attacker-price": plan["tailPriceWei"],
                       "attack-first-price": plan["firstPriceWei"]})
    command = [sys.executable, "-u", str(HERE / "mp_exp5_7702_phased_pos.py")]
    for name, value in values.items():
        command.extend(["--" + name, str(value)])
    if mode == "attack" and plan.get("attackPresign"):
        command.append("--attack-presign")
    command.append("--capture-pool-snapshots")
    command.append("--strict-setup")
    return command


def run_logged(command, log_path, timeout=3600):
    def terminate():
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)

    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n$ " + shlex.join(command) + "\n")
        log.flush()
        process = subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", bufsize=1, start_new_session=True)
        timer = threading.Timer(timeout, terminate)
        timer.start()
        try:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            code = process.wait()
            if code:
                raise RuntimeError(f"命令未完成，退出码 {code}；日志：{log_path}")
        except BaseException:
            terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        finally:
            timer.cancel()


def rpc_batch(rpc, method, params_list):
    payload = [{"jsonrpc": "2.0", "id": i, "method": method, "params": params}
               for i, params in enumerate(params_list)]
    request = urllib.request.Request(rpc, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=90) as response:
        results = json.load(response)
    if not isinstance(results, list):
        raise RuntimeError(f"{method} 的批量响应无效：{results}")
    indexed = {row.get("id"): row for row in results}
    if len(results) != len(payload) or set(indexed) != set(range(len(payload))):
        raise RuntimeError(f"{method} 的批量响应不完整")
    ordered = []
    for i in range(len(payload)):
        row = indexed[i]
        if "error" in row or "result" not in row:
            raise RuntimeError(f"{method} 请求失败：{row}")
        ordered.append(row["result"])
    return ordered


def validate_anchor(run_dir, rpc):
    identity = json.loads((run_dir / "chain_identity.json").read_text(encoding="utf-8"))
    if audit.as_int(audit.rpc_call(rpc, "eth_chainId")) != identity["chainId"]:
        raise RuntimeError("当前网络编号与实验不符，停止审计")
    block = audit.rpc_call(rpc, "eth_getBlockByNumber", [identity["anchor"]["number"], False])
    if not block or block["hash"] != identity["anchor"]["hash"]:
        raise RuntimeError("当前链与实验锚点不符，网络可能已重建；不会用新链覆盖旧结果")


def collect_evidence(run_dir, rpc):
    validate_anchor(run_dir, rpc)
    if not (run_dir / "timeseries.csv").exists():
        live = audit.read_jsonl(run_dir / "timeseries_live.jsonl")
        if not live:
            raise RuntimeError("没有负载采样记录，不能生成分阶段结果")
        write_csv(run_dir / "timeseries.csv", live)
    records = {group: audit.read_jsonl(run_dir / f"{group}_records.jsonl") for group in ("normal", "attack")}
    hashes = [audit.record_hash(record) for group in records.values() for record in group if audit.record_hash(record)]
    if len(hashes) != len(set(hashes)):
        raise RuntimeError("记录中存在重复交易哈希，必须先核查发送器")
    # 固定区块锚点前后检查；跨区块时重读，避免把正在上链的交易误判为池中丢失。
    for attempt in range(3):
        head = audit.rpc_call(rpc, "eth_getBlockByNumber", ["latest", False])
        receipts = {}
        for start in range(0, len(hashes), 100):
            batch = hashes[start:start + 100]
            values = rpc_batch(rpc, "eth_getTransactionReceipt", [[h] for h in batch])
            for tx_hash, receipt in zip(batch, values):
                if receipt and receipt["transactionHash"].lower() != tx_hash:
                    raise RuntimeError("回执交易哈希与请求不一致")
                receipts[tx_hash] = receipt
        content = audit.rpc_call(rpc, "txpool_content")
        if not isinstance(content, dict) or not all(k in content for k in ("pending", "queued")):
            raise RuntimeError("交易池内容响应不完整，停止分类")
        end = audit.rpc_call(rpc, "eth_getBlockByNumber", ["latest", False])
        stable = head["hash"] == end["hash"]
        if stable:
            break
        print(f"审计期间产生了新区块，正在复核（{attempt + 1}/3）", flush=True)
    locations = {}
    pool_rows = []
    for location in ("pending", "queued"):
        for tx in audit.iter_txpool_transactions(content[location]):
            locations[tx["hash"].lower()] = location
            pool_rows.append({"hash": tx["hash"].lower(), "sender": tx["from"].lower(),
                              "nonce": audit.as_int(tx["nonce"]), "location": location,
                              "gasPriceWei": audit.as_int(tx.get("gasPrice"))})
    canonical = {}
    for receipt in receipts.values():
        if not receipt:
            continue
        number = receipt["blockNumber"]
        if number not in canonical:
            canonical[number] = audit.rpc_call(rpc, "eth_getBlockByNumber", [number, False])
        if not canonical[number] or canonical[number]["hash"] != receipt["blockHash"]:
            raise RuntimeError("回执与当前规范链区块不符，停止写入审计结果")
    validate_anchor(run_dir, rpc)
    block_index = audit.read_block_phase_index(run_dir)
    last_window_block = max(block_index, default=-1)
    rows = []
    first_nonces = {}
    for group, entries in records.items():
        for record in entries:
            key = (group, record["sender"].lower())
            first_nonces[key] = min(first_nonces.get(key, 2**256), audit.as_int(record["nonce"]))
    for group, entries in records.items():
        for record in entries:
            row = audit.classify_record(rpc, group, record, block_index, locations, True, 0, 0,
                                        receipt_cache=receipts)
            if not stable and row["finalState"] in ("DROPPED_OR_EVICTED", "PENDING_FINAL", "QUEUED_FINAL"):
                row["finalState"] = "UNKNOWN_UNSTABLE_AUDIT"
            number = audit.as_int(row["receiptBlockNumber"])
            if number is not None and not row["includedPhase"]:
                row["includedPhase"] = "after_window" if number > last_window_block else "unknown"
            row["submittedGlobalTick"] = record.get("globalTick", "")
            row["submittedAtBlock"] = record.get("submittedAtBlock", "")
            row["role"] = record.get("normalRole", "normal") if group == "normal" else (
                "first" if audit.as_int(record["nonce"]) == first_nonces[(group, record["sender"].lower())] else "tail")
            row["feeWei"] = (audit.as_int(row["gasUsed"]) or 0) * (audit.as_int(row["effectiveGasPrice"]) or 0)
            rows.append(row)
    write_csv(run_dir / "tx_final_status.csv", rows)
    save_json(run_dir / "receipts_evidence.json", receipts)
    save_json(run_dir / "receipt_blocks_evidence.json", canonical)
    save_json(run_dir / "pool_final_evidence.json", {"stableHead": stable, "startBlock": head["number"],
                                                     "endBlock": end["number"], "transactions": pool_rows})
    sender_rows = []
    for group, sender in sorted(first_nonces):
        nonce, balance, code = [audit.rpc_call(rpc, method, [sender, end["number"]])
                                for method in ("eth_getTransactionCount", "eth_getBalance", "eth_getCode")]
        chain_nonce = audit.as_int(nonce)
        sender_pool = [tx for tx in pool_rows if tx["sender"] == sender]
        pooled_nonces = {tx["nonce"] for tx in sender_pool}
        sender_rows.append({"group": group, "sender": sender, "blockNumber": audit.as_int(end["number"]),
                            "chainNonce": chain_nonce, "balanceWei": audit.as_int(balance),
                            "delegationCode": code, "poolCount": len(sender_pool),
                            "requiredNonceInPool": chain_nonce in pooled_nonces,
                            "futureNonceWithoutHead": bool(pooled_nonces) and min(pooled_nonces) > chain_nonce})
    write_csv(run_dir / "sender_nonce_evidence.csv", sender_rows)
    summary = {"total": len(rows), "stableHead": stable, "auditBlock": audit.as_int(end["number"]),
               "byGroup": {}, "byRole": {}, "poolCounts": dict(Counter(locations.values()))}
    for field, target in (("group", "byGroup"), ("role", "byRole")):
        for kind in sorted({row[field] for row in rows} | ({"attack", "normal"} if field == "group" else set())):
            selected = [row for row in rows if row[field] == kind]
            summary[target][kind] = {
                "total": len(selected), "accepted": sum(row["accepted"] == "1" for row in selected),
                "states": dict(Counter(row["finalState"] for row in selected)),
                "included": sum(row["receiptFound"] == "1" for row in selected),
                "includedByPhase": dict(Counter(row["includedPhase"] for row in selected if row["receiptFound"] == "1")),
                "feeWei": sum(row["feeWei"] for row in selected),
            }
    summary["sendersWithNonceGap"] = sum(row["futureNonceWithoutHead"] for row in sender_rows)
    save_json(run_dir / "tx_final_status_summary.json", summary)
    build_tables(run_dir, rows, block_index)
    return summary


def build_tables(run_dir, rows, block_index):
    blocks = {number: {"blockNumber": number, "phase": info["includedPhase"],
                       "globalTick": info["includedGlobalTick"], "normalIncluded": 0,
                       "attackIncluded": 0, "normalFeeWei": 0, "attackFeeWei": 0}
              for number, info in block_index.items()}
    for row in rows:
        if row["receiptFound"] != "1":
            continue
        number = int(row["receiptBlockNumber"])
        block = blocks.setdefault(number, {"blockNumber": number, "phase": row["includedPhase"],
                                           "globalTick": "", "normalIncluded": 0, "attackIncluded": 0,
                                           "normalFeeWei": 0, "attackFeeWei": 0})
        block[row["group"] + "Included"] += 1
        block[row["group"] + "FeeWei"] += int(row["feeWei"])
    write_csv(run_dir / "block_stats.csv", [blocks[k] for k in sorted(blocks)])
    phase_rows = []
    for phase in PHASE_NAMES:
        for group in ("normal", "attack"):
            submitted = [row for row in rows if row["group"] == group and row["submittedPhase"] == phase]
            included = [row for row in rows if row["group"] == group and row["includedPhase"] == phase]
            phase_rows.append({"phase": phase, "group": group, "submitted": len(submitted),
                               "includedInThisPhase": len(included),
                               "feeWeiInThisPhase": sum(int(row["feeWei"]) for row in included),
                               "submittedHereIncludedEventually": sum(row["receiptFound"] == "1" for row in submitted),
                               "submittedHereAbsentAtAudit": sum(row["finalState"] == "DROPPED_OR_EVICTED" for row in submitted),
                               "submittedHereStillInPool": sum(row["finalState"] in ("PENDING_FINAL", "QUEUED_FINAL") for row in submitted)})
    write_csv(run_dir / "phase_stats.csv", phase_rows)


def normal_fingerprint(run_dir):
    records = audit.read_jsonl(run_dir / "normal_records.jsonl")
    canonical = [{key: row.get(key) for key in ("sender", "nonce", "gasPriceWei", "globalTick", "hash")}
                 for row in records]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def write_pair_report(pair_dir):
    summaries = {mode: json.loads((pair_dir / mode / "tx_final_status_summary.json").read_text(encoding="utf-8"))
                 for mode in ("baseline", "attack")}
    audited_rows = {mode: read_csv(pair_dir / mode / "tx_final_status.csv") for mode in ("baseline", "attack")}
    matched = normal_fingerprint(pair_dir / "baseline") == normal_fingerprint(pair_dir / "attack")
    save_json(pair_dir / "pair_validation.json", {"sameNormalTransactionsAndSchedule": matched,
                                                  "auditStable": all(s["stableHead"] for s in summaries.values())})
    lines = ["# Besu 交易池配对实验结果", "", f"正常交易及计划一致性：{'通过' if matched else '未通过，不能作为严格配对结果'}。", "",
             "| 运行 | 交易组 | 提交 | 接受 | 上链 | 审计未定 | 其中待处理 | 其中排队 | 池中缺失 | 上链费用（ETH） |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for mode, summary in summaries.items():
        for group in ("normal", "attack"):
            value = summary["byGroup"][group]
            state = value["states"]
            rows = [row for row in audited_rows[mode] if row["group"] == group]
            pending = sum(row["finalPoolLocation"] == "pending" for row in rows)
            queued = sum(row["finalPoolLocation"] == "queued" for row in rows)
            fee = Decimal(value["feeWei"]) / 10**18
            lines.append(f"| {'基线' if mode == 'baseline' else '攻击'} | {'正常' if group == 'normal' else '攻击'} | "
                         f"{value['total']} | {value['accepted']} | {value['included']} | "
                         f"{state.get('UNKNOWN_UNSTABLE_AUDIT', 0)} | {pending} | {queued} | "
                         f"{state.get('DROPPED_OR_EVICTED', 0)} | {fee} |")
    lines.extend(["", "## 全部最终状态", "", "| 运行 | 交易组 | 状态 | 数量 |", "|---|---|---|---:|"])
    for mode, summary in summaries.items():
        for group, value in summary["byGroup"].items():
            for state, count in value["states"].items():
                lines.append(f"| {'基线' if mode == 'baseline' else '攻击'} | {'正常' if group == 'normal' else '攻击'} | "
                             f"{STATES.get(state, state)} | {count} |")
    normal_finality_path = pair_dir / "attack" / "normal_finality_summary.json"
    if normal_finality_path.exists():
        finality = json.loads(normal_finality_path.read_text(encoding="utf-8"))
        lines.extend(["", "## 攻击组 normal 未定交易复核", "",
                      "该表只复核攻击组中此前未定的 normal 交易；通过多轮跨新区块观察，把状态从“审计待复核”进一步拆开。",
                      "",
                      "| 指标 | 数值 |", "|---|---:|",
                      f"| 复核 normal 交易数 | {finality.get('targetNormalTxs', 0)} |",
                      f"| 起始观察区块 | {finality.get('startBlock', '')} |",
                      f"| 结束观察区块 | {finality.get('endBlock', '')} |",
                      f"| 观察轮数 | {finality.get('observations', 0)} |",
                      f"| 确认池中缺失所需轮数 | {finality.get('confirmAbsentObservations', 0)} |",
                      "", "| 最终复核状态 | 数量 |", "|---|---:|"])
        for state, count in finality.get("states", {}).items():
            lines.append(f"| {NORMAL_FINALITY_STATES.get(state, state)} | {count} |")
        lines.extend(["", "| 提交阶段 | 最终复核状态 | 数量 |", "|---|---|---:|"])
        for phase, states in finality.get("submittedPhaseStates", {}).items():
            for state, count in states.items():
                lines.append(f"| {PHASE_NAMES.get(phase, phase)} | {NORMAL_FINALITY_STATES.get(state, state)} | {count} |")
    lines.extend(["", "## 攻击首笔与后续交易", "", "| 角色 | 提交 | 上链 | 费用（ETH） |", "|---|---:|---:|---:|"])
    for role in ("first", "tail"):
        value = summaries["attack"]["byRole"].get(role, {"total": 0, "included": 0, "feeWei": 0})
        lines.append(f"| {'首笔' if role == 'first' else '后续'} | {value['total']} | {value['included']} | "
                     f"{Decimal(value['feeWei']) / 10**18} |")
    lines.extend(["", "## 各阶段实际成交", "", "| 阶段 | 基线正常 | 攻击组正常 | 攻击交易 |", "|---|---:|---:|---:|"])
    for phase in ("warmup", "saturation", "attack_on", "recovery", "drain", "after_window", "unknown"):
        baseline_phase = "control" if phase == "attack_on" else phase
        counts = [summaries["baseline"]["byGroup"]["normal"]["includedByPhase"].get(baseline_phase, 0),
                  summaries["attack"]["byGroup"]["normal"]["includedByPhase"].get(phase, 0),
                  summaries["attack"]["byGroup"]["attack"]["includedByPhase"].get(phase, 0)]
        lines.append(f"| {PHASE_NAMES[phase]} | {counts[0]} | {counts[1]} | {counts[2]} |")
    lines.extend(["", "## 数据检查", ""])
    for mode in summaries:
        ticks = read_csv(pair_dir / mode / "timeseries.csv")
        spills = sum(int(row["blockNumber"]) - int(row["startBlock"]) != 1 for row in ticks)
        snapshots = audit.read_jsonl(pair_dir / mode / "pool_snapshots.jsonl")
        errors = sum(bool(row.get("error")) for row in snapshots)
        lines.append(f"- {'基线' if mode == 'baseline' else '攻击'}：{spills} 个采样周期没有严格对应一个区块；池快照错误 {errors} 次；"
                     f"审计区块稳定：{summaries[mode]['stableHead']}。")
    lines.extend(["", "池中缺失表示审计时没有回执且不在本节点池中；内部移除原因仍需客户端事件或日志佐证。",
                  "费用按回执 gasUsed × effectiveGasPrice 统计，包含上链但执行失败的交易。",
                  "攻击交易费用为零只描述本次攻击负载上链费用；是否压制正常交易须结合阶段对照判断。",
                  "首笔缺失且后续排队是待验证机制；sender_nonce_evidence.csv 保存链上 nonce、余额和委托代码。",
                  "本实验通过本地 RPC 提交，不等价于通过对等网络传播的公网场景。",
                  "阶段表按实际成交阶段统计；phase_stats.csv 同时记录各提交阶段交易的最终去向。", ""])
    (pair_dir / "实验结果.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines), flush=True)
    if not matched:
        raise RuntimeError("正常交易计划或内容不一致，结果已保存但配对校验未通过")


def discover_rpc(enclave):
    result = subprocess.run(["kurtosis", "enclave", "inspect", enclave],
                            check=True, text=True, capture_output=True, timeout=60)
    match = re.search(r"(?<![\w-])rpc:\s+8545/tcp\s+->\s+(?:http://)?(127\.0\.0\.1:\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"未识别到节点 RPC 地址：{result.stdout}")
    return "http://" + match.group(1)


def run_mode(pair_dir, mode):
    plan = json.loads((pair_dir / "plan.json").read_text(encoding="utf-8"))
    run_dir = pair_dir / mode
    if run_dir.exists():
        raise RuntimeError(f"{run_dir} 已存在；请审计已有结果或新建一对实验，避免覆盖")
    run_dir.mkdir()
    run_id = re.sub(r"[^A-Za-z0-9-]", "-", pair_dir.name[-15:])
    enclave = f"bp-{plan['profile']}-{run_id}-{'b' if mode == 'baseline' else 'a'}"
    save_json(run_dir / "execution.json", {"mode": mode, "enclave": enclave, "status": "starting"})
    print(f"\n正在启动{'基线' if mode == 'baseline' else '攻击'}独立网络：{enclave}", flush=True)
    run_logged(["kurtosis", "run", "--enclave", enclave, plan["package"],
                "--args-file", str(pair_dir / "network.yaml"), "--image-download", "missing"],
               run_dir / "network.log", timeout=1200)
    rpc = discover_rpc(enclave)
    chain_id = audit.as_int(audit.rpc_call(rpc, "eth_chainId"))
    client = audit.rpc_call(rpc, "web3_clientVersion")
    if chain_id != plan["chainId"] or not client.lower().startswith("besu/"):
        raise RuntimeError(f"网络身份不匹配：{chain_id}, {client}")
    sync = audit.rpc_call(rpc, "eth_syncing")
    if sync is not False:
        raise RuntimeError("节点仍在同步，请检查 network.log")
    anchor = audit.rpc_call(rpc, "eth_getBlockByNumber", ["latest", False])
    save_json(run_dir / "chain_identity.json", {"chainId": chain_id, "client": client, "rpc": rpc, "anchor": anchor})
    save_json(run_dir / "execution.json", {"mode": mode, "enclave": enclave, "rpc": rpc, "status": "running"})
    command = runner_command(plan, mode, rpc, enclave, run_dir, pair_dir / "scale_helper")
    save_json(run_dir / "argv.json", command)
    run_logged(command, run_dir / "run.log", timeout=plan.get("runTimeout", 3600))
    print("正在自动收取逐笔回执、交易池状态和阶段统计。", flush=True)
    summary = collect_evidence(run_dir, rpc)
    if not summary["stableHead"]:
        save_json(run_dir / "execution.json", {"mode": mode, "enclave": enclave, "rpc": rpc, "status": "audit_needs_retry"})
        print("审计期间区块持续变化，结果标为待复核；已保存当前证据并继续生成汇总。", flush=True)
        return
    save_json(run_dir / "execution.json", {"mode": mode, "enclave": enclave, "rpc": rpc, "status": "audited"})
    print(f"已完成并保存审计：{run_dir}", flush=True)


def prepare(args):
    pair_dir = (args.out_dir or REPO.parents[1] / "Besu配对实验" / f"{args.profile}_{time.strftime('%Y%m%d_%H%M%S')}").resolve()
    pair_dir.mkdir(parents=True, exist_ok=False)
    plan = make_plan(args.profile, args.seed, args.first_gwei, args.tail_gwei)
    save_json(pair_dir / "plan.json", plan)
    # JSON 是合法 YAML，避免依赖额外的 YAML 库。
    save_json(pair_dir / "network.yaml", network_config(plan))
    for mode in ("baseline", "attack"):
        save_json(pair_dir / f"{mode}_planned_argv.json", runner_command(
            plan, mode, "http://127.0.0.1:自动发现", "运行时创建", pair_dir / mode, pair_dir / "scale_helper"))
    save_json(pair_dir / "source_hashes.json", {
        name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
        for name in ("besu_pool_pair.py", "mp_exp5_7702_phased_pos.py", "mp_exp5_7702_scale_pos.py",
                     "eip7702_scale_helper.go", "Alice7702Drain.sol", "audit_phased_tx_final_status.py")})
    print(f"实验目录：{pair_dir}\n方案：{plan['profile']}，交易池：{plan['pool']}\n"
          f"基线正常交易：{plan['normalTotal']}；攻击组正常交易：{plan['normalTotal']}；"
          f"攻击交易：{plan['attackTotal']}\n正常 3 gwei，攻击首笔 {args.first_gwei} gwei，后续 {args.tail_gwei} gwei。", flush=True)
    return pair_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "pair"):
        command = sub.add_parser(name, help="生成可审阅方案" if name == "plan" else "自动运行基线及攻击并审计")
        command.add_argument("--profile", choices=PROFILES, default="default")
        command.add_argument("--seed", default="seed01")
        command.add_argument("--first-gwei", default="7")
        command.add_argument("--tail-gwei", default="10")
        command.add_argument("--out-dir", type=Path)
    run = sub.add_parser("run", help="继续已准备实验中的一个分支")
    run.add_argument("--pair-dir", type=Path, required=True)
    run.add_argument("--mode", choices=("baseline", "attack"), required=True)
    collect = sub.add_parser("audit", help="对尚未重建的网络重新收集回执和状态")
    collect.add_argument("--run-dir", type=Path, required=True)
    collect.add_argument("--rpc")
    report = sub.add_parser("report", help="从本地审计数据重新生成对照结果")
    report.add_argument("--pair-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "audit":
        run_dir = args.run_dir.resolve()
        rpc = args.rpc or json.loads((run_dir / "chain_identity.json").read_text(encoding="utf-8"))["rpc"]
        collect_evidence(run_dir, rpc)
    elif args.command == "report":
        write_pair_report(args.pair_dir.resolve())
    else:
        pair_dir = prepare(args) if args.command in ("plan", "pair") else args.pair_dir.resolve()
        if args.command == "plan":
            return
        go = shutil.which("go") or "/usr/local/go/bin/go"
        if not (pair_dir / "scale_helper").exists():
            print("正在预编译发送工具，避免在实验区块中重复编译。", flush=True)
            run_logged([go, "build", "-o", str(pair_dir / "scale_helper"),
                        str(HERE / "eip7702_scale_helper.go")], pair_dir / "build.log", timeout=600)
        modes = ("baseline", "attack") if args.command == "pair" else (args.mode,)
        for mode in modes:
            run_mode(pair_dir, mode)
        if all((pair_dir / mode / "tx_final_status_summary.json").exists() for mode in ("baseline", "attack")):
            write_pair_report(pair_dir)
        print(f"\n结果已保存：{pair_dir}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.SubprocessError, OSError) as exc:
        print(f"\n实验停止：{exc}\n已有记录和网络保留，请先排查或补做审计。", file=sys.stderr)
        raise SystemExit(1)
