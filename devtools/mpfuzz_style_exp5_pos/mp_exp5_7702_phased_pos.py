#!/usr/bin/env python3
"""
Time-phased Deter-Z2 evaluation for Besu/Erigon Kurtosis PoS devnets.

The scale experiment captures a one-shot txpool displacement snapshot. This
script keeps the normal workload running across phases and enables the attack
only during a configured attack-on window, so the output can be plotted as a
block-by-block time series.
"""

import argparse
import csv
import json
import math
import random
import shlex
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import mp_exp5_pos as base
import mp_exp5_7702_pos as exp7702
import mp_exp5_7702_scale_pos as scale


ETHER = 10**18


def parse_args():
    parser = argparse.ArgumentParser(description="Run phased Deter-Z2 local txpool evaluation.")
    parser.add_argument("--mode", choices=["baseline", "attack"], default="attack")
    parser.add_argument("--rpc", default=None, help="EL HTTP RPC URL. Defaults to Kurtosis discovery.")
    parser.add_argument("--enclave", required=True, help="Kurtosis enclave name.")
    parser.add_argument("--client", choices=["besu", "erigon"], required=True)
    parser.add_argument("--trial-id", default="", help="Optional trial label included in output paths.")
    parser.add_argument("--seed", default="exp5-7702-phased")
    parser.add_argument("--workload-seed", default=None, help="Seed for the paired normal workload. Defaults to --seed.")

    parser.add_argument("--normal-senders", type=int, default=55)
    parser.add_argument("--attack-senders", type=int, default=125)
    parser.add_argument("--normal-rate-per-block", type=int, default=128)
    parser.add_argument("--normal-rate-jitter", type=float, default=0.0, help="Symmetric per-block normal tx-count jitter. 0.15 means roughly +/-15%% while preserving the total planned normal tx count.")
    parser.add_argument("--normal-sender-shuffle", action="store_true", help="Shuffle normal sender order per block using --workload-seed.")
    parser.add_argument("--normal-victim-senders", type=int, default=0, help="Use this many one-shot normal senders during the attack/control phase. Warmup, saturation, and recovery keep using --normal-senders.")
    parser.add_argument("--attack-rate-per-block", type=int, default=512)
    parser.add_argument(
        "--attack-sender-activation",
        choices=["all", "new-per-block", "cumulative"],
        default="all",
        help=(
            "How attack senders are used across the attack-on window. "
            "all preserves the original behavior; new-per-block partitions senders across attack blocks; "
            "cumulative keeps previously activated senders while adding new ones each attack block."
        ),
    )
    parser.add_argument("--warmup-blocks", type=int, default=2)
    parser.add_argument("--saturation-blocks", type=int, default=2)
    parser.add_argument("--attack-blocks", type=int, default=2)
    parser.add_argument("--recovery-blocks", type=int, default=2)
    parser.add_argument("--recovery-normal-rate-per-block", type=int, default=None, help="Override normal tx count during recovery. Default uses --normal-rate-per-block; 0 keeps recovery as observation-only.")
    parser.add_argument("--drain-blocks", type=int, default=0)

    parser.add_argument("--normal-calldata-bytes", type=int, default=0)
    parser.add_argument("--attack-calldata-padding-bytes", type=int, default=0)
    parser.add_argument("--attack-first-calldata-padding-bytes", type=int, default=-1, help="Optional zero-byte padding for the first attack transaction from each attack sender. Default -1 uses --attack-calldata-padding-bytes for every attack transaction.")
    parser.add_argument("--normal-gas", type=int, default=21_000)
    parser.add_argument("--attack-gas", type=int, default=500_000)
    parser.add_argument("--normal-fund-eth", type=float, default=10)
    parser.add_argument("--attack-balance-eth", type=int, default=300)
    parser.add_argument("--attack-gas-cap-eth", type=int, default=2)

    parser.add_argument("--price-unit", type=int, default=None)
    parser.add_argument("--setup-price", type=int, default=10)
    parser.add_argument("--normal-price", type=int, default=3)
    parser.add_argument("--attacker-price", type=int, default=7)
    parser.add_argument("--attack-first-price", type=int, default=-1, help="Optional gas price multiplier for the first attack transaction per sender. Default -1 uses --attacker-price for every attack transaction.")
    parser.add_argument("--normal-gas-price-mode", choices=["fixed", "sampled"], default="fixed", help="fixed uses normal-price * price-unit; sampled draws each normal tx gas price from a mainnet fee_history.csv column.")
    parser.add_argument("--normal-fee-history", default=None, help="Path to fee_history.csv produced by collect_mainnet_fee_history.py. Providing this also enables sampled mode.")
    parser.add_argument("--normal-fee-field", default="effectiveP50Gwei", help="fee_history.csv column to sample for normal tx gas prices.")
    parser.add_argument("--normal-fee-jitter", type=float, default=0.10, help="Symmetric random jitter applied to each sampled normal gas price, e.g. 0.10 means +/-10%%.")
    parser.add_argument("--normal-fee-multiplier", type=float, default=1.0, help="Multiplier applied after sampling the mainnet fee value.")
    parser.add_argument("--normal-fee-sampling-strategy", choices=["per-tx", "per-sender", "sender-monotonic"], default="per-tx", help="How sampled normal gas prices are assigned. per-tx preserves the original independent sampling; per-sender reuses one sampled price per sender; sender-monotonic samples per transaction but never decreases for the same sender nonce stream.")
    parser.add_argument("--normal-fee-floor-wei", type=int, default=0, help="Optional minimum sampled normal gas price in wei.")
    parser.add_argument("--normal-fee-floor-gwei", default=None, help="Optional minimum sampled normal gas price in gwei.")
    parser.add_argument("--normal-fee-cap-wei", type=int, default=0, help="Optional maximum sampled normal gas price in wei. Default 0 disables the cap.")
    parser.add_argument("--normal-fee-cap-gwei", default=None, help="Optional maximum sampled normal gas price in gwei.")
    parser.add_argument("--deploy-gas", type=int, default=2_000_000)
    parser.add_argument("--setcode-gas", type=int, default=250_000)
    parser.add_argument("--batch-size", type=int, default=125)
    parser.add_argument("--setup-wait", type=int, default=240)
    parser.add_argument("--receipt-timeout", type=int, default=240)
    parser.add_argument("--final-receipt-timeout", type=int, default=5)
    parser.add_argument(
        "--receipt-scope",
        choices=["none", "included", "all"],
        default="included",
        help="none skips final receipt queries; included queries only hashes observed in blocks; all queries every accepted workload hash.",
    )
    parser.add_argument("--fresh-block-timeout", type=int, default=90)
    parser.add_argument(
        "--pre-setup-fresh-blocks",
        type=int,
        default=2,
        help="Require this many fresh blocks before sending setup transactions.",
    )
    parser.add_argument("--solc-version", default="0.8.20")
    parser.add_argument("--evm-version", default="london")
    parser.add_argument("--go-binary", default="/usr/local/go/bin/go")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--helper-binary", default=None, help="Use an already compiled scale helper.")
    parser.add_argument("--normal-helper-send-workers", type=int, default=1, help="Parallel sender workers for normal workload helper calls.")
    parser.add_argument("--attack-helper-send-workers", type=int, default=1, help="Parallel sender workers for attack workload helper calls.")
    parser.add_argument("--attack-presign", action="store_true", help="Pre-sign attack raw transactions during setup and only broadcast them during attack_on.")
    parser.add_argument("--capture-pool-snapshots", action="store_true", help="Archive compact pool membership before/after submission and after blocks.")
    parser.add_argument("--strict-setup", action="store_true", help="Require funded accounts, installed delegation, and an empty pool before workload.")
    parser.add_argument(
        "--txpool-classification",
        choices=["none", "content"],
        default="none",
        help="Use txpool_content to classify known normal/attack hashes at every sample. Expensive for large runs.",
    )
    return parser.parse_args()


def positive(name, value):
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")


def validate_args(args):
    positive("normal-senders", args.normal_senders)
    positive("normal-rate-per-block", args.normal_rate_per_block)
    if args.normal_victim_senders < 0:
        raise RuntimeError("--normal-victim-senders must be non-negative")
    if args.recovery_normal_rate_per_block is not None and args.recovery_normal_rate_per_block < 0:
        raise RuntimeError("--recovery-normal-rate-per-block must be non-negative")
    if args.normal_victim_senders and args.normal_rate_jitter:
        raise RuntimeError("--normal-victim-senders does not support --normal-rate-jitter; keep victim txs one-shot and deterministic")
    if args.recovery_normal_rate_per_block is not None and args.normal_rate_jitter:
        raise RuntimeError("--recovery-normal-rate-per-block does not support --normal-rate-jitter")
    if args.normal_rate_jitter < 0:
        raise RuntimeError("--normal-rate-jitter must be non-negative")
    if args.mode == "attack":
        positive("attack-senders", args.attack_senders)
        positive("attack-rate-per-block", args.attack_rate_per_block)
    for name in ("warmup_blocks", "saturation_blocks", "attack_blocks", "recovery_blocks"):
        if getattr(args, name) < 0:
            raise RuntimeError(f"{name.replace('_', '-')} must be non-negative")
    if args.mode == "attack" and args.attack_blocks <= 0:
        raise RuntimeError("attack-blocks must be positive in attack mode")
    if args.warmup_blocks + args.saturation_blocks + args.attack_blocks + args.recovery_blocks + args.drain_blocks <= 0:
        raise RuntimeError("at least one phase block is required")
    if args.normal_calldata_bytes < 0 or args.attack_calldata_padding_bytes < 0:
        raise RuntimeError("calldata byte counts must be non-negative")
    if args.normal_helper_send_workers <= 0 or args.attack_helper_send_workers <= 0:
        raise RuntimeError("helper send worker counts must be positive")
    if args.attack_presign and args.mode != "attack":
        raise RuntimeError("--attack-presign is only valid in attack mode")
    if args.attack_first_calldata_padding_bytes < -1:
        raise RuntimeError("attack-first-calldata-padding-bytes must be -1 or non-negative")
    if args.attack_first_price < -1:
        raise RuntimeError("attack-first-price must be -1 or non-negative")
    min_normal_gas = 21_000 + args.normal_calldata_bytes * 4
    if args.normal_gas < min_normal_gas:
        raise RuntimeError(f"normal-gas is too low for {args.normal_calldata_bytes} zero calldata bytes: need at least {min_normal_gas}")
    if args.price_unit is None:
        max_attack_price = max(args.attacker_price, args.attack_first_price if args.attack_first_price >= 0 else args.attacker_price)
        args.price_unit = (args.attack_gas_cap_eth * ETHER) // (max_attack_price * args.attack_gas)

    recovery_rate = normal_count_for_phase(args, "recovery")
    background_normal_total = (
        (args.warmup_blocks + args.saturation_blocks) * args.normal_rate_per_block
        + args.recovery_blocks * recovery_rate
    )
    victim_normal_total = 0
    if args.normal_victim_senders:
        victim_normal_total = args.attack_blocks * args.normal_rate_per_block
        if args.normal_victim_senders < victim_normal_total:
            raise RuntimeError(
                "--normal-victim-senders is too low: "
                f"need at least {victim_normal_total} one-shot victim senders"
            )
    else:
        background_normal_total += args.attack_blocks * args.normal_rate_per_block
    normal_txs_per_sender = math.ceil(background_normal_total / args.normal_senders) if background_normal_total else 0
    if args.normal_rate_jitter or args.normal_sender_shuffle:
        max_tick_normal = int(round(args.normal_rate_per_block * (1 + args.normal_rate_jitter)))
        active_background_blocks = args.warmup_blocks + args.saturation_blocks + args.attack_blocks + args.recovery_blocks
        max_normal_txs_per_sender = active_background_blocks * math.ceil(max_tick_normal / args.normal_senders)
        normal_txs_per_sender = max(normal_txs_per_sender, max_normal_txs_per_sender)
    if args.normal_victim_senders:
        normal_txs_per_sender = max(
            normal_txs_per_sender,
            math.ceil(victim_normal_total / args.normal_victim_senders),
        )
    normal_required_gas_price = scale.normal_gas_price_upper_bound_wei(args)
    normal_required_wei = normal_txs_per_sender * (normal_required_gas_price * args.normal_gas + 1)
    if int(args.normal_fund_eth * ETHER) < normal_required_wei:
        need = Decimal(normal_required_wei) / Decimal(ETHER)
        raise RuntimeError(f"normal-fund-eth is too low: need at least {need} ETH per normal sender")

    if args.mode == "attack":
        attack_total = args.attack_blocks * args.attack_rate_per_block
        attack_txs_per_sender = math.ceil(attack_total / args.attack_senders)
        max_attack_price = max(args.attacker_price, args.attack_first_price if args.attack_first_price >= 0 else args.attacker_price)
        attack_required_wei = attack_txs_per_sender * max_attack_price * args.price_unit * args.attack_gas
        if args.attack_balance_eth * ETHER < attack_required_wei:
            need = Decimal(attack_required_wei) / Decimal(ETHER)
            raise RuntimeError(f"attack-balance-eth is too low: need at least {need} ETH per attack sender")


def make_deploy_args(args):
    return SimpleNamespace(
        case="alice-sender",
        rpc_url=args.rpc_url,
        deploy_gas=args.deploy_gas,
        setup_price=args.setup_price,
        price_unit=args.price_unit,
        receipt_timeout=max(args.receipt_timeout, args.setup_wait),
        solc_version=args.solc_version,
        evm_version=args.evm_version,
    )


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_commands(path, repo_root, argv):
    command = shlex.join([sys.executable] + argv)
    path.write_text(
        "\n".join(
            [
                "# Reproduction command",
                "",
                "```bash",
                f"cd {shlex.quote(str(repo_root))}",
                command,
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )


def append_jsonl(path, rows):
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def capture_pool_snapshot(rpc, out_dir, phase, tick, point):
    from audit_phased_tx_final_status import iter_txpool_transactions

    snapshot = {"phase": phase, "globalTick": tick, "point": point, "unixTime": time.time()}
    try:
        snapshot["startBlock"] = base.hex_to_int(rpc.call("eth_blockNumber"))
        content = rpc.call("txpool_content")
        if not isinstance(content, dict):
            raise RuntimeError("txpool_content did not return an object")
        snapshot["transactions"] = [
            {"hash": tx.get("hash"), "sender": tx.get("from"), "nonce": tx.get("nonce"),
             "gasPrice": tx.get("gasPrice"), "location": location}
            for location in ("pending", "queued")
            for tx in iter_txpool_transactions(content.get(location, {}))
        ]
        snapshot["endBlock"] = base.hex_to_int(rpc.call("eth_blockNumber"))
    except Exception as exc:
        snapshot["error"] = str(exc)
    append_jsonl(out_dir / "pool_snapshots.jsonl", [snapshot])


def verify_setup(rpc, args, normal_accounts, attack_accounts, setup_records, delegate, out_dir):
    errors = [row for row in setup_records if row.get("error") or not row.get("hash")]
    if errors:
        raise RuntimeError(f"setup submission failed for {len(errors)} transactions")
    status = rpc.call("txpool_status")
    if base.hex_to_int(status["pending"]) or base.hex_to_int(status["queued"]):
        raise RuntimeError(f"setup has not drained: {status}")
    evidence = []
    for group, accounts, required in (
        ("normal", normal_accounts, int(args.normal_fund_eth * ETHER)),
        ("attack", attack_accounts, args.attack_balance_eth * ETHER),
    ):
        for account in accounts:
            address = account["address"]
            balance = base.balance(rpc, address)
            code = rpc.call("eth_getCode", [address, "latest"])
            valid = balance >= required and (group != "attack" or code.lower() == "0xef0100" + delegate[2:].lower())
            evidence.append({"group": group, "sender": address, "balanceWei": balance, "code": code, "valid": valid})
    (out_dir / "setup_evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    if not all(row["valid"] for row in evidence):
        raise RuntimeError("funding or delegation verification failed; inspect setup_evidence.json")


def init_nonce_tracker(rpc, accounts):
    return {
        account["label"]: base.hex_to_int(rpc.call("eth_getTransactionCount", [account["address"], "pending"]))
        for account in accounts
    }


def attack_first_gas_price_list(accounts, txs_per_sender, send_order, first_gas_price_wei, regular_gas_price_wei):
    prices = []
    if txs_per_sender <= 0:
        return prices
    if send_order == "round-robin":
        for tx_index in range(txs_per_sender):
            for _account in accounts:
                prices.append(first_gas_price_wei if tx_index == 0 else regular_gas_price_wei)
    else:
        for _account in accounts:
            for tx_index in range(txs_per_sender):
                prices.append(first_gas_price_wei if tx_index == 0 else regular_gas_price_wei)
    return prices


def run_helper_once(repo_root, args, mode, accounts, out_dir, tag, extra, gas_price_sampler=None, txs_per_sender=1, nonce_tracker=None):
    if not accounts:
        return []
    accounts_path = out_dir / f"{tag}_{mode}_accounts.jsonl"
    helper_accounts = []
    for account in accounts:
        helper_account = dict(account)
        if nonce_tracker is not None:
            label = account["label"]
            helper_account["nonce"] = nonce_tracker[label]
            nonce_tracker[label] += txs_per_sender
        helper_accounts.append(helper_account)
    scale.write_jsonl(accounts_path, helper_accounts)
    call_extra = list(extra)
    if gas_price_sampler is not None:
        gas_prices = gas_price_sampler.take(
            len(accounts) * txs_per_sender,
            accounts=accounts,
            txs_per_sender=txs_per_sender,
            send_order=scale.extra_option_value(call_extra, "--send-order", "account"),
        )
        gas_prices_path = out_dir / f"{tag}_{mode}_gas_prices.txt"
        scale.write_gas_price_list(gas_prices_path, gas_prices)
        call_extra += ["--gas-price-wei-list", str(gas_prices_path)]
    records = scale.run_helper(repo_root, args, mode, accounts_path, call_extra)
    errors = [record for record in records if record.get("error")]
    if errors:
        print(f"warning: {tag} {mode} errors={len(errors)} first={errors[0]}")
    return records


def select_accounts(accounts, start, count):
    if count <= 0:
        return []
    return [accounts[(start + index) % len(accounts)] for index in range(count)]


def workload_seed_value(args):
    return args.workload_seed or args.seed


def normal_count_for_phase(args, phase):
    if phase == "recovery" and args.recovery_normal_rate_per_block is not None:
        return args.recovery_normal_rate_per_block
    return args.normal_rate_per_block


def build_normal_workload_plan(args, phases):
    rng = random.Random(f"{workload_seed_value(args)}:{args.client}:normal-count-plan")
    rows = []
    active_indexes = []
    global_tick = 1

    for phase, phase_blocks, send_normal, send_attack in phases:
        for phase_tick in range(1, phase_blocks + 1):
            row = {
                "globalTick": global_tick,
                "phase": phase,
                "phaseTick": phase_tick,
                "normalEnabled": send_normal,
                "attackEnabled": send_attack,
                "normalCount": normal_count_for_phase(args, phase) if send_normal else 0,
            }
            rows.append(row)
            if row["normalCount"]:
                active_indexes.append(len(rows) - 1)
            global_tick += 1

    if not active_indexes or args.normal_rate_jitter == 0:
        return rows

    base_count = args.normal_rate_per_block
    min_count = max(0, int(round(base_count * (1 - args.normal_rate_jitter))))
    max_count = max(min_count, int(round(base_count * (1 + args.normal_rate_jitter))))
    target_total = base_count * len(active_indexes)

    for index in active_indexes:
        rows[index]["normalCount"] = rng.randint(min_count, max_count)

    delta = target_total - sum(rows[index]["normalCount"] for index in active_indexes)
    while delta:
        indexes = list(active_indexes)
        rng.shuffle(indexes)
        changed = False
        for index in indexes:
            if delta > 0 and rows[index]["normalCount"] < max_count:
                rows[index]["normalCount"] += 1
                delta -= 1
                changed = True
            elif delta < 0 and rows[index]["normalCount"] > min_count:
                rows[index]["normalCount"] -= 1
                delta += 1
                changed = True
            if delta == 0:
                break
        if not changed:
            raise RuntimeError("failed to balance randomized normal workload plan")
    return rows


def normal_accounts_for_tick(accounts, args, global_tick):
    if not args.normal_sender_shuffle:
        return accounts
    ordered = list(accounts)
    rng = random.Random(f"{workload_seed_value(args)}:{args.client}:normal-sender-order:{global_tick}")
    rng.shuffle(ordered)
    return ordered


def victim_accounts_for_attack_tick(accounts, phase_tick, count):
    if count <= 0:
        return []
    start = phase_tick * count
    end = start + count
    if end > len(accounts):
        raise RuntimeError(
            "not enough one-shot victim normal accounts for attack/control phase: "
            f"need indexes [{start}, {end}), have {len(accounts)}"
        )
    return accounts[start:end]


def attack_accounts_for_tick(accounts, args, attack_phase_tick):
    if args.attack_sender_activation == "all":
        return accounts
    if not accounts:
        return []
    group_size = math.ceil(len(accounts) / args.attack_blocks)
    start = attack_phase_tick * group_size
    end = min(len(accounts), start + group_size)
    if start >= len(accounts):
        return accounts if args.attack_sender_activation == "cumulative" else []
    if args.attack_sender_activation == "new-per-block":
        return accounts[start:end]
    if args.attack_sender_activation == "cumulative":
        return accounts[:end]
    raise RuntimeError(f"unknown attack sender activation mode: {args.attack_sender_activation}")


def send_workload(
    repo_root,
    args,
    mode,
    accounts,
    out_dir,
    phase,
    tick,
    count,
    common_extra,
    gas_price_sampler=None,
    first_calldata_bytes=None,
    first_calldata_pending=None,
    first_gas_price_wei=None,
    nonce_tracker=None,
):
    if count <= 0:
        return [], 0
    records = []
    full_rounds = count // len(accounts)
    remainder = count % len(accounts)
    seq = 0

    def send_group(group_accounts, txs_per_sender, apply_first_calldata):
        nonlocal seq
        if not group_accounts:
            return
        tag = f"{phase}_{tick:03d}_{seq:02d}"
        extra = common_extra + ["--txs-per-sender", str(txs_per_sender)]
        if apply_first_calldata:
            extra += ["--first-calldata-bytes", str(first_calldata_bytes)]
            if first_gas_price_wei is not None:
                send_order = scale.extra_option_value(extra, "--send-order", "account")
                regular_gas_price_wei = scale.extra_option_value(
                    extra,
                    "--gas-price-wei",
                    str(args.attacker_price * args.price_unit),
                )
                gas_prices = attack_first_gas_price_list(
                    group_accounts,
                    txs_per_sender,
                    send_order,
                    int(first_gas_price_wei),
                    int(regular_gas_price_wei),
                )
                gas_prices_path = out_dir / f"{tag}_{mode}_first_low_tail_gas_prices.txt"
                scale.write_gas_price_list(gas_prices_path, gas_prices)
                extra += ["--gas-price-wei-list", str(gas_prices_path)]
            for account in group_accounts:
                first_calldata_pending.discard(account["label"])
        records.extend(
            run_helper_once(
                repo_root,
                args,
                mode,
                group_accounts,
                out_dir,
                tag,
                extra,
                gas_price_sampler,
                txs_per_sender,
                nonce_tracker=nonce_tracker,
            )
        )
        seq += 1

    def split_by_first_calldata(group_accounts):
        if first_calldata_bytes is None or first_calldata_pending is None:
            return [], group_accounts
        first_accounts = [account for account in group_accounts if account["label"] in first_calldata_pending]
        regular_accounts = [account for account in group_accounts if account["label"] not in first_calldata_pending]
        return first_accounts, regular_accounts

    if full_rounds:
        first_accounts, regular_accounts = split_by_first_calldata(accounts)
        send_group(first_accounts, full_rounds, True)
        send_group(regular_accounts, full_rounds, False)
    if remainder:
        start_offset = (tick * count) % len(accounts)
        selected = select_accounts(accounts, start_offset, remainder)
        first_accounts, regular_accounts = split_by_first_calldata(selected)
        send_group(first_accounts, 1, True)
        send_group(regular_accounts, 1, False)
    for record in records:
        record["phase"] = phase
        record["phaseTick"] = tick
    return records, len(records)


def presign_attack_workloads(
    repo_root,
    args,
    attack_accounts,
    out_dir,
    common_extra,
    first_calldata_bytes=None,
    first_gas_price_wei=None,
    nonce_tracker=None,
):
    presign_dir = out_dir / "presigned_attack"
    presign_dir.mkdir(parents=True, exist_ok=True)
    first_calldata_pending = (
        {account["label"] for account in attack_accounts}
        if first_calldata_bytes is not None
        else set()
    )
    manifest = {}

    for attack_phase_tick in range(args.attack_blocks):
        tick_accounts = attack_accounts_for_tick(attack_accounts, args, attack_phase_tick)
        active_senders = len(tick_accounts)
        tag = f"attack_on_{attack_phase_tick + 1:03d}"
        if not active_senders:
            manifest[str(attack_phase_tick)] = {
                "phase": "attack_on",
                "phaseTick": attack_phase_tick + 1,
                "activeSenders": 0,
                "txsPerSender": 0,
                "expected": 0,
            }
            continue
        if args.attack_rate_per_block % active_senders:
            raise RuntimeError(
                "--attack-presign requires attack-rate-per-block to be divisible by "
                f"active senders for each attack tick: rate={args.attack_rate_per_block}, "
                f"active={active_senders}"
            )

        txs_per_sender = args.attack_rate_per_block // active_senders
        helper_accounts = []
        for account in tick_accounts:
            helper_account = dict(account)
            if nonce_tracker is not None:
                label = account["label"]
                helper_account["nonce"] = nonce_tracker[label]
                nonce_tracker[label] += txs_per_sender
            helper_accounts.append(helper_account)

        accounts_path = presign_dir / f"{tag}_accounts.jsonl"
        raw_path = presign_dir / f"{tag}.rawtx"
        records_path = presign_dir / f"{tag}_records.jsonl"
        scale.write_jsonl(accounts_path, helper_accounts)

        extra = list(common_extra) + ["--txs-per-sender", str(txs_per_sender)]
        labels = {account["label"] for account in tick_accounts}
        apply_first = bool(first_calldata_pending & labels)
        if apply_first:
            if labels - first_calldata_pending:
                raise RuntimeError(
                    "--attack-presign does not support mixed first/non-first senders in one helper call"
                )
            extra += ["--first-calldata-bytes", str(first_calldata_bytes)]
            if first_gas_price_wei is not None:
                send_order = scale.extra_option_value(extra, "--send-order", "account")
                regular_gas_price_wei = scale.extra_option_value(
                    extra,
                    "--gas-price-wei",
                    str(args.attacker_price * args.price_unit),
                )
                gas_prices = attack_first_gas_price_list(
                    tick_accounts,
                    txs_per_sender,
                    send_order,
                    int(first_gas_price_wei),
                    int(regular_gas_price_wei),
                )
                gas_prices_path = presign_dir / f"{tag}_gas_prices.txt"
                scale.write_gas_price_list(gas_prices_path, gas_prices)
                extra += ["--gas-price-wei-list", str(gas_prices_path)]
            first_calldata_pending -= labels

        extra += [
            "--presign-raw-output",
            str(raw_path),
            "--presign-records-output",
            str(records_path),
        ]
        records = scale.run_helper(repo_root, args, "attack", accounts_path, extra)
        errors = [record for record in records if record.get("error")]
        if errors:
            raise RuntimeError(f"pre-sign attack tick {attack_phase_tick + 1} failed: {errors[0]}")
        expected = active_senders * txs_per_sender
        if len(records) != expected:
            raise RuntimeError(
                f"pre-sign attack tick {attack_phase_tick + 1} produced {len(records)} records, "
                f"expected {expected}"
            )
        manifest[str(attack_phase_tick)] = {
            "phase": "attack_on",
            "phaseTick": attack_phase_tick + 1,
            "activeSenders": active_senders,
            "txsPerSender": txs_per_sender,
            "expected": expected,
            "accountsPath": str(accounts_path),
            "rawPath": str(raw_path),
            "recordsPath": str(records_path),
        }
        print(
            f"pre-signed attack tick={attack_phase_tick + 1} "
            f"activeSenders={active_senders} txsPerSender={txs_per_sender} total={expected}"
        )

    (out_dir / "presigned_attack_manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return manifest


def broadcast_presigned_attack_workload(repo_root, args, tick_info, common_extra):
    if not tick_info or not tick_info.get("activeSenders"):
        return [], 0
    extra = list(common_extra) + [
        "--txs-per-sender",
        str(tick_info["txsPerSender"]),
        "--broadcast-raw-input",
        tick_info["rawPath"],
        "--broadcast-records-input",
        tick_info["recordsPath"],
    ]
    records = scale.run_helper(repo_root, args, "attack", tick_info["accountsPath"], extra)
    errors = [record for record in records if record.get("error")]
    if errors:
        print(f"warning: pre-signed attack broadcast errors={len(errors)} first={errors[0]}")
    for record in records:
        record["phase"] = tick_info["phase"]
        record["phaseTick"] = tick_info["phaseTick"]
    return records, len(records)


def accepted_hashes(records):
    return {record["hash"].lower() for record in records if record.get("hash") and not record.get("error")}


def block_hashes(rpc, block_number):
    block = rpc.call("eth_getBlockByNumber", [hex(block_number), False])
    if not block:
        return []
    return [tx_hash.lower() for tx_hash in block.get("transactions", [])]


def count_new_block_inclusions(rpc, start_block, end_block, normal_hashes, attack_hashes, seen_normal, seen_attack):
    normal_included = 0
    attack_included = 0
    for block_number in range(start_block, end_block + 1):
        for tx_hash in block_hashes(rpc, block_number):
            if tx_hash in normal_hashes and tx_hash not in seen_normal:
                seen_normal.add(tx_hash)
                normal_included += 1
            if tx_hash in attack_hashes and tx_hash not in seen_attack:
                seen_attack.add(tx_hash)
                attack_included += 1
    return normal_included, attack_included


def receipt_statuses_for_hashes(rpc, hashes, timeout):
    deadline = time.time() + timeout
    remaining = {tx_hash.lower() for tx_hash in hashes if tx_hash}
    receipts = {}
    first_pass = True
    while remaining and (first_pass or time.time() < deadline):
        first_pass = False
        next_remaining = set()
        for tx_hash in sorted(remaining):
            receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                receipts[tx_hash] = receipt
            else:
                next_remaining.add(tx_hash)
        remaining = next_remaining
        if remaining and time.time() < deadline:
            time.sleep(1)
    return receipts


def receipt_statuses_for_scope(rpc, records, included_hashes, scope, timeout):
    if scope == "none":
        return {}
    if scope == "all":
        return scale.receipt_statuses(rpc, records, timeout)
    return receipt_statuses_for_hashes(rpc, included_hashes, timeout)


def classify_known_hashes(rpc, normal_records, attack_records, enabled):
    if not enabled:
        return None, None, ""
    try:
        locations = scale.txpool_hashes(rpc)
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    return scale.count_locations(normal_records, locations), scale.count_locations(attack_records, locations), ""


def phase_plan(args):
    phases = [
        ("warmup", args.warmup_blocks, True, False),
        ("saturation", args.saturation_blocks, True, False),
    ]
    if args.mode == "attack":
        phases.append(("attack_on", args.attack_blocks, True, True))
    else:
        phases.append(("control", args.attack_blocks, True, False))
    phases.append(("recovery", args.recovery_blocks, True, False))
    if args.drain_blocks:
        phases.append(("drain", args.drain_blocks, False, False))
    return phases


def location_value(counts, key):
    if not counts:
        return ""
    return counts.get(key, "")


def write_metrics(path, metrics):
    fieldnames = [
        "client",
        "clientVersion",
        "chainId",
        "mode",
        "trialId",
        "rpc",
        "outDir",
        "warmupBlocks",
        "saturationBlocks",
        "attackBlocks",
        "recoveryBlocks",
        "drainBlocks",
        "normalRatePerBlock",
        "attackRatePerBlock",
        "attackSenderActivation",
        "normalSubmitted",
        "normalAccepted",
        "normalErrored",
        "normalIncluded",
        "normalSuccessfulReceipts",
        "normalFailedReceipts",
        "normalInclusionRate",
        "attackSubmitted",
        "attackAccepted",
        "attackErrored",
        "attackIncluded",
        "attackSuccessfulReceipts",
        "attackFailedReceipts",
        "attackSuccessfulSenders",
        "attackSuccessfulReceiptsPerSender",
        "attackInclusionRate",
        "normalPendingFinal",
        "normalQueuedFinal",
        "normalDroppedFinal",
        "attackPendingFinal",
        "attackQueuedFinal",
        "attackDroppedFinal",
        "txpoolPendingFinal",
        "txpoolQueuedFinal",
        "normalGasUsedOnChain",
        "normalCostWei",
        "normalCostEth",
        "attackGasUsedOnChain",
        "attackCostWei",
        "attackCostEth",
        "priceUnitWei",
        "normalPrice",
        "attackerPrice",
        "attackFirstPrice",
        "normalGasPriceMode",
        "normalFeeHistory",
        "normalFeeField",
        "normalFeeJitter",
        "normalFeeMultiplier",
        "normalFeeSamplingStrategy",
        "normalFeeFloorWei",
        "normalFeeCapWei",
        "workloadSeed",
        "normalRateJitter",
        "normalSenderShuffle",
        "normalVictimSenders",
        "recoveryNormalRatePerBlock",
        "normalPlannedTotal",
        "normalGasPriceMinWei",
        "normalGasPriceMedianWei",
        "normalGasPriceAvgWei",
        "normalGasPriceMaxWei",
        "receiptScope",
        "normalGas",
        "attackGas",
        "normalCalldataBytes",
        "attackCalldataPaddingBytes",
        "attackFirstCalldataPaddingBytes",
        "normalSenders",
        "attackSenders",
        "normalHelperSendWorkers",
        "attackHelperSendWorkers",
        "attackPresign",
        "txpoolClassification",
    ]
    write_csv(path, fieldnames, [metrics])


def main():
    args = parse_args()
    validate_args(args)
    args.rpc_url = args.rpc or base.discover_kurtosis_rpc(args.enclave)
    phases = phase_plan(args)
    normal_workload_plan = build_normal_workload_plan(args, phases)
    normal_workload_by_tick = {row["globalTick"]: row for row in normal_workload_plan}

    exp_dir = Path(__file__).resolve().parent
    repo_root = exp_dir.parents[1]
    accounts = base.load_key_csv(exp_dir / "key_prive_exp5.csv")
    funder = accounts[base.DEFAULT_FUNDER["label"]]
    receiver = accounts["x"]
    rpc = base.RpcClient(args.rpc_url)
    chain_id = base.hex_to_int(rpc.call("eth_chainId"))
    client_version = rpc.call("web3_clientVersion")
    normal_gas_price_sampler = scale.build_normal_gas_price_sampler(args)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    trial_part = f"_{args.trial_id}" if args.trial_id else ""
    out_dir = Path(args.out_dir) if args.out_dir else exp_dir / "scale_runs" / f"{args.client}_{args.mode}_phased{trial_part}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_commands(out_dir / "commands.md", repo_root, sys.argv)

    normal_background_accounts = scale.derive_accounts("pn", args.normal_senders, f"{args.seed}:{args.client}")
    normal_victim_accounts = (
        scale.derive_accounts("pv", args.normal_victim_senders, f"{args.seed}:{args.client}")
        if args.normal_victim_senders
        else []
    )
    normal_accounts = normal_background_accounts + normal_victim_accounts
    attack_accounts = scale.derive_accounts("pa", args.attack_senders, f"{args.seed}:{args.client}") if args.mode == "attack" else []
    scale.write_jsonl(out_dir / "normal_accounts.jsonl", normal_accounts)
    scale.write_jsonl(out_dir / "normal_background_accounts.jsonl", normal_background_accounts)
    scale.write_jsonl(out_dir / "normal_victim_accounts.jsonl", normal_victim_accounts)
    scale.write_jsonl(out_dir / "attack_accounts.jsonl", attack_accounts)
    normal_workload_plan_path = out_dir / "normal_workload_plan.csv"
    write_csv(
        normal_workload_plan_path,
        ["globalTick", "phase", "phaseTick", "normalEnabled", "attackEnabled", "normalCount"],
        normal_workload_plan,
    )

    print(f"RPC: {args.rpc_url}")
    print(f"client={client_version}")
    print(f"chainId={chain_id}")
    print(f"outDir={out_dir}")
    print(f"mode={args.mode}")
    print(f"normalSenders={len(normal_accounts)} normalBackgroundSenders={len(normal_background_accounts)} normalVictimSenders={len(normal_victim_accounts)} normalRatePerBlock={args.normal_rate_per_block}")
    print(f"workloadSeed={workload_seed_value(args)}")
    print(f"normalRateJitter={args.normal_rate_jitter}")
    print(f"normalSenderShuffle={args.normal_sender_shuffle}")
    print(f"normalPlannedTotal={sum(row['normalCount'] for row in normal_workload_plan)}")
    print(f"attackSenders={len(attack_accounts)} attackRatePerBlock={args.attack_rate_per_block if args.mode == 'attack' else 0}")
    if args.mode == "attack":
        print(f"attackSenderActivation={args.attack_sender_activation}")
    if args.mode == "attack" and args.attack_first_calldata_padding_bytes >= 0:
        print(f"attackFirstCalldataPaddingBytes={args.attack_first_calldata_padding_bytes}")
    if args.mode == "attack" and args.attack_first_price >= 0:
        print(f"attackFirstPrice={args.attack_first_price}")
    print(f"helperSendWorkers normal={args.normal_helper_send_workers} attack={args.attack_helper_send_workers if args.mode == 'attack' else 0}")
    print(f"attackPresign={args.attack_presign if args.mode == 'attack' else False}")
    print(
        "phases="
        f"warmup:{args.warmup_blocks},saturation:{args.saturation_blocks},"
        f"attack/control:{args.attack_blocks},recovery:{args.recovery_blocks},drain:{args.drain_blocks}"
    )
    print(f"priceUnitWei={args.price_unit}")
    print(f"normalGasPriceMode={args.normal_gas_price_mode}")
    if normal_gas_price_sampler:
        print(f"normalFeeHistory={args.normal_fee_history}")
        print(f"normalFeeField={args.normal_fee_field}")
        print(f"normalFeeJitter={args.normal_fee_jitter}")
        print(f"normalFeeMultiplier={args.normal_fee_multiplier}")
        print(f"normalFeeSamplingStrategy={normal_gas_price_sampler.strategy}")
        print(f"normalFeeFloorWei={scale.normal_fee_floor_wei(args)}")
        print(f"normalFeeCapWei={scale.normal_fee_cap_wei(args)}")
        print(f"normalFeeSourceStats={normal_gas_price_sampler.source_stats()}")
    print(f"receiver={receiver['address']} balanceWei={base.balance(rpc, receiver['address'])}")

    status = rpc.call("txpool_status")
    if base.hex_to_int(status["pending"]) or base.hex_to_int(status["queued"]):
        raise RuntimeError(f"txpool is not empty before setup: {status}")

    print("\n========== setup: wait for fresh block production ==========")
    scale.wait_blocks(rpc, args.pre_setup_fresh_blocks, args.fresh_block_timeout)

    setup_gas_price = str(args.setup_price * args.price_unit)
    normal_gas_price = str(args.normal_price * args.price_unit)
    attack_gas_price = str(args.attacker_price * args.price_unit)
    attack_first_gas_price = str(args.attack_first_price * args.price_unit) if args.attack_first_price >= 0 else None
    delegate = None

    if args.mode == "attack":
        print("\n========== setup: deploy Alice7702Drain implementation ==========")
        delegate = exp7702.deploy_delegate(rpc, chain_id, accounts, make_deploy_args(args))

    print("\n========== setup: fund generated normal senders ==========")
    fund_normal = scale.run_helper_in_batches(
        repo_root,
        args,
        "fund",
        normal_accounts,
        out_dir,
        [
            "--sponsor-key",
            funder["private_key"],
            "--value-wei",
            str(int(args.normal_fund_eth * ETHER)),
            "--gas-price-wei",
            setup_gas_price,
            "--gas",
            "21000",
            "--wait",
            f"{args.setup_wait}s",
        ],
    )
    print(f"fund normal sent={sum(1 for r in fund_normal if r.get('hash'))} errors={sum(1 for r in fund_normal if r.get('error'))}")

    fund_attack = []
    setcode_records = []
    if args.mode == "attack":
        print("\n========== setup: fund generated attack senders ==========")
        fund_attack = scale.run_helper_in_batches(
            repo_root,
            args,
            "fund",
            attack_accounts,
            out_dir,
            [
                "--sponsor-key",
                funder["private_key"],
                "--value-wei",
                str(args.attack_balance_eth * ETHER),
                "--gas-price-wei",
                setup_gas_price,
                "--gas",
                "21000",
                "--wait",
                f"{args.setup_wait}s",
            ],
        )
        print(f"fund attack sent={sum(1 for r in fund_attack if r.get('hash'))} errors={sum(1 for r in fund_attack if r.get('error'))}")

        print("\n========== setup: EIP-7702 delegate attack senders ==========")
        setcode_records = scale.run_helper_in_batches(
            repo_root,
            args,
            "setcode",
            attack_accounts,
            out_dir,
            [
                "--sponsor-key",
                funder["private_key"],
                "--delegate-address",
                delegate,
                "--gas-fee-cap-wei",
                setup_gas_price,
                "--gas-tip-cap-wei",
                setup_gas_price,
                "--gas",
                str(args.setcode_gas),
                "--wait",
                f"{args.setup_wait}s",
            ],
        )
        print(f"setcode sent={sum(1 for r in setcode_records if r.get('hash'))} errors={sum(1 for r in setcode_records if r.get('error'))}")

    scale.wait_blocks(rpc, 1, args.fresh_block_timeout)
    if args.strict_setup:
        verify_setup(rpc, args, normal_accounts, attack_accounts,
                     fund_normal + fund_attack + setcode_records, delegate, out_dir)
    status = rpc.call("txpool_status")
    if base.hex_to_int(status["pending"]) or base.hex_to_int(status["queued"]):
        print(f"warning: txpool not empty after setup: {status}")

    normal_nonce_tracker = init_nonce_tracker(rpc, normal_accounts)
    attack_nonce_tracker = init_nonce_tracker(rpc, attack_accounts) if args.mode == "attack" else {}
    print(f"normalStartingNonceRange={min(normal_nonce_tracker.values()) if normal_nonce_tracker else 0}..{max(normal_nonce_tracker.values()) if normal_nonce_tracker else 0}")
    if args.mode == "attack":
        print(f"attackStartingNonceRange={min(attack_nonce_tracker.values()) if attack_nonce_tracker else 0}..{max(attack_nonce_tracker.values()) if attack_nonce_tracker else 0}")

    normal_records = []
    attack_records = []
    seen_normal = set()
    seen_attack = set()
    normal_hashes = set()
    attack_hashes = set()
    cumulative_normal_submitted = 0
    cumulative_attack_submitted = 0
    cumulative_normal_accepted = 0
    cumulative_attack_accepted = 0
    cumulative_normal_errors = 0
    cumulative_attack_errors = 0
    cumulative_normal_included = 0
    cumulative_attack_included = 0
    last_observed_block = base.hex_to_int(rpc.call("eth_blockNumber"))
    timeseries = []
    phase_markers = []

    normal_extra = [
        "--value-wei",
        "1",
        "--gas-price-wei",
        normal_gas_price,
        "--gas",
        str(args.normal_gas),
        "--calldata-bytes",
        str(args.normal_calldata_bytes),
        "--send-order",
        "round-robin",
        "--send-workers",
        str(args.normal_helper_send_workers),
    ]
    attack_extra = [
        "--receiver",
        receiver["address"],
        "--value-wei",
        "0",
        "--gas-price-wei",
        attack_gas_price,
        "--gas",
        str(args.attack_gas),
        "--calldata-bytes",
        str(args.attack_calldata_padding_bytes),
        "--send-order",
        "round-robin",
        "--send-workers",
        str(args.attack_helper_send_workers),
    ]
    attack_first_calldata_pending = (
        {account["label"] for account in attack_accounts}
        if args.mode == "attack" and args.attack_first_calldata_padding_bytes >= 0
        else None
    )
    presigned_attack_ticks = {}
    if args.mode == "attack" and args.attack_presign:
        print("\n========== setup: pre-sign attack workload ==========")
        presigned_attack_ticks = presign_attack_workloads(
            repo_root,
            args,
            attack_accounts,
            out_dir,
            attack_extra,
            first_calldata_bytes=args.attack_first_calldata_padding_bytes if args.attack_first_calldata_padding_bytes >= 0 else None,
            first_gas_price_wei=attack_first_gas_price,
            nonce_tracker=attack_nonce_tracker,
        )
        last_observed_block = base.hex_to_int(rpc.call("eth_blockNumber"))

    print("\n========== phased workload ==========")
    global_tick = 0
    for phase, phase_blocks, send_normal, send_attack in phases:
        if phase_blocks <= 0:
            continue
        print(f"\n========== phase: {phase} blocks={phase_blocks} ==========")
        phase_start_block = base.hex_to_int(rpc.call("eth_blockNumber"))
        phase_start_tick = global_tick + 1
        for tick in range(phase_blocks):
            current_block = base.hex_to_int(rpc.call("eth_blockNumber"))
            tick_started = time.monotonic()
            observation_start_block = last_observed_block + 1
            if args.capture_pool_snapshots:
                capture_pool_snapshot(rpc, out_dir, phase, global_tick + 1, "before_send")
            normal_sent_this_tick = 0
            attack_sent_this_tick = 0
            attack_active_senders_this_tick = 0
            normal_send_elapsed_seconds = 0.0
            attack_send_elapsed_seconds = 0.0
            plan_row = normal_workload_by_tick[global_tick + 1]

            if send_normal:
                normal_role = "victim" if args.normal_victim_senders and phase in ("attack_on", "control") else "background"
                if normal_role == "victim":
                    normal_accounts_this_tick = victim_accounts_for_attack_tick(
                        normal_victim_accounts,
                        tick,
                        int(plan_row["normalCount"]),
                    )
                else:
                    normal_accounts_this_tick = normal_accounts_for_tick(normal_background_accounts, args, global_tick + 1)
                normal_send_started = time.monotonic()
                records, normal_sent_this_tick = send_workload(
                    repo_root,
                    args,
                    "normal",
                    normal_accounts_this_tick,
                    out_dir,
                    phase,
                    global_tick,
                    int(plan_row["normalCount"]),
                    normal_extra,
                    gas_price_sampler=normal_gas_price_sampler,
                    nonce_tracker=normal_nonce_tracker,
                )
                normal_send_elapsed_seconds = time.monotonic() - normal_send_started
                for record in records:
                    record.update(globalTick=global_tick + 1, submittedPhaseTick=tick + 1,
                                  submittedAtBlock=current_block, normalRole=normal_role)
                append_jsonl(out_dir / "normal_records.jsonl", records)
                normal_records.extend(records)
                cumulative_normal_submitted += normal_sent_this_tick
                cumulative_normal_accepted += scale.accepted_count(records)
                cumulative_normal_errors += sum(1 for record in records if record.get("error"))
                normal_hashes.update(accepted_hashes(records))

            if send_attack and args.mode == "attack":
                attack_send_started = time.monotonic()
                if args.attack_presign:
                    tick_info = presigned_attack_ticks.get(str(tick), {})
                    attack_active_senders_this_tick = int(tick_info.get("activeSenders", 0))
                    records, attack_sent_this_tick = broadcast_presigned_attack_workload(
                        repo_root,
                        args,
                        tick_info,
                        attack_extra,
                    )
                else:
                    attack_accounts_this_tick = attack_accounts_for_tick(attack_accounts, args, tick)
                    attack_active_senders_this_tick = len(attack_accounts_this_tick)
                    if attack_accounts_this_tick:
                        records, attack_sent_this_tick = send_workload(
                            repo_root,
                            args,
                            "attack",
                            attack_accounts_this_tick,
                            out_dir,
                            phase,
                            global_tick,
                            args.attack_rate_per_block,
                            attack_extra,
                            first_calldata_bytes=args.attack_first_calldata_padding_bytes if args.attack_first_calldata_padding_bytes >= 0 else None,
                            first_calldata_pending=attack_first_calldata_pending,
                            first_gas_price_wei=attack_first_gas_price,
                            nonce_tracker=attack_nonce_tracker,
                        )
                    else:
                        records = []
                        attack_sent_this_tick = 0
                attack_send_elapsed_seconds = time.monotonic() - attack_send_started
                for record in records:
                    record.update(globalTick=global_tick + 1, submittedPhaseTick=tick + 1,
                                  submittedAtBlock=current_block)
                append_jsonl(out_dir / "attack_records.jsonl", records)
                attack_records.extend(records)
                cumulative_attack_submitted += attack_sent_this_tick
                cumulative_attack_accepted += scale.accepted_count(records)
                cumulative_attack_errors += sum(1 for record in records if record.get("error"))
                attack_hashes.update(accepted_hashes(records))

            status_after_send = rpc.call("txpool_status")
            send_end_block = base.hex_to_int(rpc.call("eth_blockNumber"))
            send_elapsed_seconds = time.monotonic() - tick_started
            if args.capture_pool_snapshots:
                capture_pool_snapshot(rpc, out_dir, phase, global_tick + 1, "after_send")
            target_block = current_block + 1
            observed_block = scale.wait_until_block(
                rpc,
                target_block,
                args.fresh_block_timeout,
                start=current_block,
            )
            included_normal, included_attack = count_new_block_inclusions(
                rpc,
                last_observed_block + 1,
                observed_block,
                normal_hashes,
                attack_hashes,
                seen_normal,
                seen_attack,
            )
            last_observed_block = observed_block
            cumulative_normal_included += included_normal
            cumulative_attack_included += included_attack
            status_after_block = rpc.call("txpool_status")
            if args.capture_pool_snapshots:
                capture_pool_snapshot(rpc, out_dir, phase, global_tick + 1, "after_block")
            normal_counts, attack_counts, classification_error = classify_known_hashes(
                rpc,
                normal_records,
                attack_records,
                args.txpool_classification == "content",
            )
            row = {
                "blockNumber": observed_block,
                "sampleUnixTime": int(time.time()),
                "phase": phase,
                "phaseTick": tick + 1,
                "globalTick": global_tick + 1,
                "startBlock": current_block,
                "observationStartBlock": observation_start_block,
                "sendEndBlock": send_end_block,
                "sendElapsedSeconds": send_elapsed_seconds,
                "normalSendElapsedSeconds": normal_send_elapsed_seconds,
                "attackSendElapsedSeconds": attack_send_elapsed_seconds,
                "targetBlock": target_block,
                "normalSubmittedThisTick": normal_sent_this_tick,
                "attackSubmittedThisTick": attack_sent_this_tick,
                "attackActiveSendersThisTick": attack_active_senders_this_tick,
                "normalSubmittedTotal": cumulative_normal_submitted,
                "attackSubmittedTotal": cumulative_attack_submitted,
                "normalAcceptedTotal": cumulative_normal_accepted,
                "attackAcceptedTotal": cumulative_attack_accepted,
                "normalErroredTotal": cumulative_normal_errors,
                "attackErroredTotal": cumulative_attack_errors,
                "normalIncludedInObservedBlocks": included_normal,
                "attackIncludedInObservedBlocks": included_attack,
                "normalIncludedTotal": cumulative_normal_included,
                "attackIncludedTotal": cumulative_attack_included,
                "normalBacklog": cumulative_normal_accepted - cumulative_normal_included,
                "attackBacklog": cumulative_attack_accepted - cumulative_attack_included,
                "txpoolPendingAfterSend": scale.status_count(status_after_send, "pending"),
                "txpoolQueuedAfterSend": scale.status_count(status_after_send, "queued"),
                "txpoolPendingAfterBlock": scale.status_count(status_after_block, "pending"),
                "txpoolQueuedAfterBlock": scale.status_count(status_after_block, "queued"),
                "normalPending": location_value(normal_counts, "pending"),
                "normalQueued": location_value(normal_counts, "queued"),
                "normalDropped": location_value(normal_counts, "dropped"),
                "attackPending": location_value(attack_counts, "pending"),
                "attackQueued": location_value(attack_counts, "queued"),
                "attackDropped": location_value(attack_counts, "dropped"),
                "txpoolClassificationError": classification_error,
            }
            timeseries.append(row)
            append_jsonl(out_dir / "timeseries_live.jsonl", [row])
            print(
                "sample "
                f"block={observed_block} phase={phase} "
                f"normalSent={normal_sent_this_tick} attackSent={attack_sent_this_tick} "
                f"attackActiveSenders={attack_active_senders_this_tick} "
                f"normalIncluded={included_normal} attackIncluded={included_attack} "
                f"txpool={status_after_block}"
            )
            global_tick += 1
        phase_markers.append(
            {
                "phase": phase,
                "startBlock": phase_start_block,
                "endBlock": last_observed_block,
                "startGlobalTick": phase_start_tick,
                "endGlobalTick": global_tick,
                "normalEnabled": send_normal,
                "attackEnabled": send_attack,
            }
        )

    timeseries_fields = [
        "blockNumber",
        "sampleUnixTime",
        "phase",
        "phaseTick",
        "globalTick",
        "startBlock",
        "observationStartBlock",
        "sendEndBlock",
        "sendElapsedSeconds",
        "normalSendElapsedSeconds",
        "attackSendElapsedSeconds",
        "targetBlock",
        "normalSubmittedThisTick",
        "attackSubmittedThisTick",
        "attackActiveSendersThisTick",
        "normalSubmittedTotal",
        "attackSubmittedTotal",
        "normalAcceptedTotal",
        "attackAcceptedTotal",
        "normalErroredTotal",
        "attackErroredTotal",
        "normalIncludedInObservedBlocks",
        "attackIncludedInObservedBlocks",
        "normalIncludedTotal",
        "attackIncludedTotal",
        "normalBacklog",
        "attackBacklog",
        "txpoolPendingAfterSend",
        "txpoolQueuedAfterSend",
        "txpoolPendingAfterBlock",
        "txpoolQueuedAfterBlock",
        "normalPending",
        "normalQueued",
        "normalDropped",
        "attackPending",
        "attackQueued",
        "attackDropped",
        "txpoolClassificationError",
    ]
    timeseries_path = out_dir / "timeseries.csv"
    write_csv(timeseries_path, timeseries_fields, timeseries)
    phase_markers_path = out_dir / "phase_markers.csv"
    write_csv(
        phase_markers_path,
        [
            "phase",
            "startBlock",
            "endBlock",
            "startGlobalTick",
            "endGlobalTick",
            "normalEnabled",
            "attackEnabled",
        ],
        phase_markers,
    )

    print("\n========== final receipts ==========")
    normal_receipts = receipt_statuses_for_scope(
        rpc,
        normal_records,
        seen_normal,
        args.receipt_scope,
        args.final_receipt_timeout,
    )
    attack_receipts = (
        receipt_statuses_for_scope(
            rpc,
            attack_records,
            seen_attack,
            args.receipt_scope,
            args.final_receipt_timeout,
        )
        if args.mode == "attack"
        else {}
    )
    (out_dir / "normal_receipts.json").write_text(json.dumps(normal_receipts, indent=2), encoding="utf-8")
    (out_dir / "attack_receipts.json").write_text(json.dumps(attack_receipts, indent=2), encoding="utf-8")

    final_status = rpc.call("txpool_status")
    final_normal_counts, final_attack_counts, final_classification_error = classify_known_hashes(
        rpc,
        normal_records,
        attack_records,
        args.txpool_classification == "content",
    )
    normal_successful = scale.successful_receipt_count(normal_receipts)
    normal_failed = scale.failed_receipt_count(normal_receipts)
    attack_successful = scale.successful_receipt_count(attack_receipts)
    attack_failed = scale.failed_receipt_count(attack_receipts)
    normal_gas_used, normal_cost_wei = scale.receipt_cost_summary(normal_receipts)
    attack_gas_used, attack_cost_wei = scale.receipt_cost_summary(attack_receipts)
    if normal_gas_price_sampler:
        normal_gas_price_generated_stats = normal_gas_price_sampler.generated_stats()
    else:
        accepted_normal_count = scale.accepted_count(normal_records)
        normal_gas_price_generated_stats = scale.gas_price_stats([int(normal_gas_price)] * accepted_normal_count)

    per_sender = {}
    counted_receipt_hashes = set()
    for record in attack_records:
        sender = record.get("sender", "").lower()
        if not sender:
            continue
        tx_hash = record.get("hash", "").lower()
        receipt = attack_receipts.get(tx_hash)
        if receipt and receipt.get("status") == "0x1":
            if tx_hash not in counted_receipt_hashes:
                per_sender[sender] = per_sender.get(sender, 0) + 1
                counted_receipt_hashes.add(tx_hash)
        else:
            per_sender.setdefault(sender, 0)
    mined_counts = {}
    for count in per_sender.values():
        mined_counts[count] = mined_counts.get(count, 0) + 1

    metrics = {
        "client": args.client,
        "clientVersion": client_version,
        "chainId": chain_id,
        "mode": args.mode,
        "trialId": args.trial_id,
        "rpc": args.rpc_url,
        "outDir": str(out_dir),
        "warmupBlocks": args.warmup_blocks,
        "saturationBlocks": args.saturation_blocks,
        "attackBlocks": args.attack_blocks,
        "recoveryBlocks": args.recovery_blocks,
        "drainBlocks": args.drain_blocks,
        "normalRatePerBlock": args.normal_rate_per_block,
        "attackRatePerBlock": args.attack_rate_per_block if args.mode == "attack" else 0,
        "attackSenderActivation": args.attack_sender_activation if args.mode == "attack" else "",
        "normalSubmitted": cumulative_normal_submitted,
        "normalAccepted": cumulative_normal_accepted,
        "normalErrored": cumulative_normal_errors,
        "normalIncluded": cumulative_normal_included,
        "normalSuccessfulReceipts": normal_successful,
        "normalFailedReceipts": normal_failed,
        "normalInclusionRate": scale.ratio(normal_successful, cumulative_normal_submitted),
        "attackSubmitted": cumulative_attack_submitted,
        "attackAccepted": cumulative_attack_accepted,
        "attackErrored": cumulative_attack_errors,
        "attackIncluded": cumulative_attack_included,
        "attackSuccessfulReceipts": attack_successful,
        "attackFailedReceipts": attack_failed,
        "attackSuccessfulSenders": sum(1 for count in per_sender.values() if count > 0),
        "attackSuccessfulReceiptsPerSender": json.dumps(mined_counts, sort_keys=True, separators=(",", ":")),
        "attackInclusionRate": scale.ratio(attack_successful, cumulative_attack_submitted),
        "normalPendingFinal": location_value(final_normal_counts, "pending"),
        "normalQueuedFinal": location_value(final_normal_counts, "queued"),
        "normalDroppedFinal": location_value(final_normal_counts, "dropped"),
        "attackPendingFinal": location_value(final_attack_counts, "pending"),
        "attackQueuedFinal": location_value(final_attack_counts, "queued"),
        "attackDroppedFinal": location_value(final_attack_counts, "dropped"),
        "txpoolPendingFinal": scale.status_count(final_status, "pending"),
        "txpoolQueuedFinal": scale.status_count(final_status, "queued"),
        "normalGasUsedOnChain": normal_gas_used,
        "normalCostWei": normal_cost_wei,
        "normalCostEth": scale.wei_to_eth(normal_cost_wei),
        "attackGasUsedOnChain": attack_gas_used,
        "attackCostWei": attack_cost_wei,
        "attackCostEth": scale.wei_to_eth(attack_cost_wei),
        "priceUnitWei": args.price_unit,
        "normalPrice": args.normal_price,
        "attackerPrice": args.attacker_price if args.mode == "attack" else "",
        "attackFirstPrice": args.attack_first_price if args.mode == "attack" and args.attack_first_price >= 0 else "",
        "normalGasPriceMode": args.normal_gas_price_mode,
        "normalFeeHistory": args.normal_fee_history or "",
        "normalFeeField": args.normal_fee_field if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeJitter": args.normal_fee_jitter if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeMultiplier": args.normal_fee_multiplier if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeSamplingStrategy": args.normal_fee_sampling_strategy if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeFloorWei": scale.normal_fee_floor_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeCapWei": scale.normal_fee_cap_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "workloadSeed": workload_seed_value(args),
        "normalRateJitter": args.normal_rate_jitter,
        "normalSenderShuffle": args.normal_sender_shuffle,
        "normalVictimSenders": args.normal_victim_senders,
        "recoveryNormalRatePerBlock": normal_count_for_phase(args, "recovery"),
        "normalPlannedTotal": sum(row["normalCount"] for row in normal_workload_plan),
        "normalGasPriceMinWei": normal_gas_price_generated_stats.get("minWei", ""),
        "normalGasPriceMedianWei": normal_gas_price_generated_stats.get("medianWei", ""),
        "normalGasPriceAvgWei": normal_gas_price_generated_stats.get("avgWei", ""),
        "normalGasPriceMaxWei": normal_gas_price_generated_stats.get("maxWei", ""),
        "receiptScope": args.receipt_scope,
        "normalGas": args.normal_gas,
        "attackGas": args.attack_gas,
        "normalCalldataBytes": args.normal_calldata_bytes,
        "attackCalldataPaddingBytes": args.attack_calldata_padding_bytes,
        "attackFirstCalldataPaddingBytes": args.attack_first_calldata_padding_bytes if args.mode == "attack" and args.attack_first_calldata_padding_bytes >= 0 else "",
        "normalSenders": len(normal_accounts),
        "attackSenders": len(attack_accounts),
        "normalHelperSendWorkers": args.normal_helper_send_workers,
        "attackHelperSendWorkers": args.attack_helper_send_workers if args.mode == "attack" else "",
        "attackPresign": args.attack_presign if args.mode == "attack" else "",
        "txpoolClassification": args.txpool_classification,
    }
    metrics_path = out_dir / "metrics.csv"
    write_metrics(metrics_path, metrics)

    summary = {
        "client": args.client,
        "clientVersion": client_version,
        "chainId": chain_id,
        "mode": args.mode,
        "delegate": delegate,
        "normalGasPriceMode": args.normal_gas_price_mode,
        "normalFeeHistory": args.normal_fee_history,
        "normalFeeField": args.normal_fee_field if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeJitter": args.normal_fee_jitter if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeMultiplier": args.normal_fee_multiplier if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeSamplingStrategy": args.normal_fee_sampling_strategy if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeFloorWei": scale.normal_fee_floor_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeCapWei": scale.normal_fee_cap_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "workloadSeed": workload_seed_value(args),
        "normalRateJitter": args.normal_rate_jitter,
        "normalSenderShuffle": args.normal_sender_shuffle,
        "normalVictimSenders": args.normal_victim_senders,
        "recoveryNormalRatePerBlock": normal_count_for_phase(args, "recovery"),
        "normalPlannedTotal": sum(row["normalCount"] for row in normal_workload_plan),
        "normalGasPriceStats": normal_gas_price_generated_stats,
        "attackSenderActivation": args.attack_sender_activation if args.mode == "attack" else "",
        "attackCalldataPaddingBytes": args.attack_calldata_padding_bytes,
        "attackFirstCalldataPaddingBytes": args.attack_first_calldata_padding_bytes if args.mode == "attack" and args.attack_first_calldata_padding_bytes >= 0 else "",
        "attackFirstPrice": args.attack_first_price if args.mode == "attack" and args.attack_first_price >= 0 else "",
        "normalHelperSendWorkers": args.normal_helper_send_workers,
        "attackHelperSendWorkers": args.attack_helper_send_workers if args.mode == "attack" else "",
        "attackPresign": args.attack_presign if args.mode == "attack" else "",
        "phases": [
            {
                "name": name,
                "blocks": blocks,
                "normalEnabled": send_normal,
                "attackEnabled": send_attack,
            }
            for name, blocks, send_normal, send_attack in phases
        ],
        "metrics": metrics,
        "finalTxpoolStatus": final_status,
        "finalClassificationError": final_classification_error,
        "fundNormal": fund_normal,
        "fundAttack": fund_attack,
        "setcode": setcode_records,
        "timeseries": str(timeseries_path),
        "phaseMarkers": str(phase_markers_path),
        "normalWorkloadPlan": str(normal_workload_plan_path),
        "outDir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"normalSubmitted={cumulative_normal_submitted} normalAccepted={cumulative_normal_accepted} normalIncluded={cumulative_normal_included}")
    print(f"attackSubmitted={cumulative_attack_submitted} attackAccepted={cumulative_attack_accepted} attackIncluded={cumulative_attack_included}")
    print(f"normalReceipts={len(normal_receipts)} attackReceipts={len(attack_receipts)}")
    print(f"attackSuccessfulReceiptsPerSender={mined_counts}")
    print(f"timeseries={timeseries_path}")
    print(f"phaseMarkers={phase_markers_path}")
    print(f"normalWorkloadPlan={normal_workload_plan_path}")
    print(f"metrics={metrics_path}")
    print(f"summary={out_dir / 'summary.json'}")


if __name__ == "__main__":
    raise SystemExit(main())
