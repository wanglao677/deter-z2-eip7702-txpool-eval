"""Deterministic, matched Phase 2 normal arrivals (public plan only)."""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BESU = HERE.parent / "besu26_layertrace"
import sys
sys.path.insert(0, str(BESU))
from normal_fee import prices, parameters, statistics
sys.path.pop(0)


def schedule(config: dict, seed: str, accounts: list[dict], client: str) -> list[dict]:
    phase = config["phase2"]
    slots = phase["preSlots"] + phase["attackSlots"] + phase["postSlots"]
    seconds = config["secondsPerSlot"]
    rate = phase["normalArrivalRatePerSlot"]
    if not accounts or not seed or client not in {"besu", "erigon"}:
        raise ValueError("Need client, seed and normal accounts")
    if any(type(phase[k]) is not int or phase[k] <= 0
           for k in ("preSlots", "attackSlots", "postSlots", "normalArrivalRatePerSlot")):
        raise ValueError("Phase durations and normal rate must be positive integers")
    if phase.get("normalArrivalModel") != "poisson-exponential":
        raise ValueError("Only poisson-exponential normal arrivals are supported")
    if phase.get("normalSenderAssignment") != "round-robin-global":
        raise ValueError("Phase 2 requires sequential nonces via global round-robin sender assignment")
    rng = random.Random(f"phase2-arrivals-v1:{phase['arrivalSeed']}:{seed}:{client}")
    offsets: list[int] = []
    for slot in range(slots):
        elapsed = 0.0
        while True:
            elapsed += rng.expovariate(rate / seconds)
            if elapsed >= seconds:
                break
            offsets.append(slot * seconds * 1000 + int(elapsed * 1000))
    fee_values = prices(config, len(offsets))
    nonces = Counter()
    rows = []
    for index, (offset, price) in enumerate(zip(offsets, fee_values)):
        sender_index = index % len(accounts)
        account = accounts[sender_index]
        nonce = nonces[sender_index]
        nonces[sender_index] += 1
        rows.append(dict(index=index, offsetMillis=offset, slotIndex=offset // (seconds * 1000),
                         senderIndex=sender_index, sender=account["address"],
                         label=f"{account['label']}-n{nonce + 1}", nonce=nonce,
                         gasPriceWei=price))
    return rows


def phase_for_slot(index: int, phase: dict) -> str:
    if index < 0 or index >= phase["preSlots"] + phase["attackSlots"] + phase["postSlots"]:
        raise ValueError("Slot outside observation horizon")
    if index < phase["preSlots"]:
        return "pre_attack"
    if index < phase["preSlots"] + phase["attackSlots"]:
        return "attack_observation"
    return "post_injection_observation"


def validate_plan(config: dict, rows: list[dict], accounts: list[dict], block_gas_limit: int) -> dict:
    phase = config["phase2"]
    counts = Counter(row["slotIndex"] for row in rows)
    sender_counts = Counter(row["senderIndex"] for row in rows)
    spec = parameters(config)
    per_sender_max_wei = {}
    for sender_index, account in enumerate(accounts):
        # Conservative upper bound based on configured gas limit and maximum fee.
        count = sender_counts[sender_index]
        required = count * (config["normalGas"] * spec["maximumWei"] + 1)
        funded = config["normalFundEth"] * 10**18
        if required > funded:
            raise RuntimeError(f"{account['label']}: {required} Wei upper bound exceeds funding {funded}")
        per_sender_max_wei[account["label"]] = required
    if any(n * config["normalGas"] * 10_000 >
           block_gas_limit * phase["maxNormalGasBudgetBps"] for n in counts.values()):
        raise RuntimeError("Normal arrival plan exceeds the configured share of observed block gas limit")
    if any(row["nonce"] >= sender_counts[row["senderIndex"]] for row in rows):
        raise RuntimeError("Invalid normal nonce")
    return dict(count=len(rows), maxPerSlot=max(counts.values(), default=0),
                minPerSlot=min((counts.get(i, 0) for i in range(
                    phase["preSlots"] + phase["attackSlots"] + phase["postSlots"])), default=0),
                maxSenderUpfrontWei=max(per_sender_max_wei.values(), default=0),
                maxPlannedGasPerSlot=max(counts.values(), default=0) * config["normalGas"],
                observedBlockGasLimit=block_gas_limit)


def write_schedule(folder: Path, config: dict, seed: str, client: str,
                   rows: list[dict], budget: dict) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "normal_schedule.jsonl"
    payload = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    path.write_text(payload, encoding="utf-8")
    meta = dict(client=client, seed=seed, model="poisson-exponential",
                scheduleSha256=hashlib.sha256(payload.encode()).hexdigest(),
                normalGasPriceMode=parameters(config)["mode"],
                normalGasLimit=config["normalGas"],
                plannedSignedGasPriceStats=statistics([row["gasPriceWei"] for row in rows]),
                budget=budget,
                phase2=config["phase2"])
    (folder / "normal_schedule_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta
