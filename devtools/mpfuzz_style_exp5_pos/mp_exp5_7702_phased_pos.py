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

    parser.add_argument("--normal-senders", type=int, default=55)
    parser.add_argument("--attack-senders", type=int, default=125)
    parser.add_argument("--normal-rate-per-block", type=int, default=128)
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
    if args.attack_first_calldata_padding_bytes < -1:
        raise RuntimeError("attack-first-calldata-padding-bytes must be -1 or non-negative")
    min_normal_gas = 21_000 + args.normal_calldata_bytes * 4
    if args.normal_gas < min_normal_gas:
        raise RuntimeError(f"normal-gas is too low for {args.normal_calldata_bytes} zero calldata bytes: need at least {min_normal_gas}")
    if args.price_unit is None:
        args.price_unit = (args.attack_gas_cap_eth * ETHER) // (args.attacker_price * args.attack_gas)

    active_normal_blocks = args.warmup_blocks + args.saturation_blocks + args.attack_blocks + args.recovery_blocks
    normal_total = active_normal_blocks * args.normal_rate_per_block
    normal_txs_per_sender = math.ceil(normal_total / args.normal_senders)
    normal_required_gas_price = scale.normal_gas_price_upper_bound_wei(args)
    normal_required_wei = normal_txs_per_sender * (normal_required_gas_price * args.normal_gas + 1)
    if int(args.normal_fund_eth * ETHER) < normal_required_wei:
        need = Decimal(normal_required_wei) / Decimal(ETHER)
        raise RuntimeError(f"normal-fund-eth is too low: need at least {need} ETH per normal sender")

    if args.mode == "attack":
        attack_total = args.attack_blocks * args.attack_rate_per_block
        attack_txs_per_sender = math.ceil(attack_total / args.attack_senders)
        attack_required_wei = attack_txs_per_sender * args.attacker_price * args.price_unit * args.attack_gas
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


def run_helper_once(repo_root, args, mode, accounts, out_dir, tag, extra, gas_price_sampler=None, txs_per_sender=1):
    if not accounts:
        return []
    accounts_path = out_dir / f"{tag}_{mode}_accounts.jsonl"
    scale.write_jsonl(accounts_path, accounts)
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
            for account in group_accounts:
                first_calldata_pending.discard(account["label"])
        records.extend(run_helper_once(repo_root, args, mode, group_accounts, out_dir, tag, extra, gas_price_sampler, txs_per_sender))
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
        "normalGasPriceMode",
        "normalFeeHistory",
        "normalFeeField",
        "normalFeeJitter",
        "normalFeeMultiplier",
        "normalFeeSamplingStrategy",
        "normalFeeFloorWei",
        "normalFeeCapWei",
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
        "txpoolClassification",
    ]
    write_csv(path, fieldnames, [metrics])


def main():
    args = parse_args()
    validate_args(args)
    args.rpc_url = args.rpc or base.discover_kurtosis_rpc(args.enclave)

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

    normal_accounts = scale.derive_accounts("pn", args.normal_senders, f"{args.seed}:{args.client}")
    attack_accounts = scale.derive_accounts("pa", args.attack_senders, f"{args.seed}:{args.client}") if args.mode == "attack" else []
    scale.write_jsonl(out_dir / "normal_accounts.jsonl", normal_accounts)
    scale.write_jsonl(out_dir / "attack_accounts.jsonl", attack_accounts)

    print(f"RPC: {args.rpc_url}")
    print(f"client={client_version}")
    print(f"chainId={chain_id}")
    print(f"outDir={out_dir}")
    print(f"mode={args.mode}")
    print(f"normalSenders={len(normal_accounts)} normalRatePerBlock={args.normal_rate_per_block}")
    print(f"attackSenders={len(attack_accounts)} attackRatePerBlock={args.attack_rate_per_block if args.mode == 'attack' else 0}")
    if args.mode == "attack":
        print(f"attackSenderActivation={args.attack_sender_activation}")
    if args.mode == "attack" and args.attack_first_calldata_padding_bytes >= 0:
        print(f"attackFirstCalldataPaddingBytes={args.attack_first_calldata_padding_bytes}")
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
    status = rpc.call("txpool_status")
    if base.hex_to_int(status["pending"]) or base.hex_to_int(status["queued"]):
        print(f"warning: txpool not empty after setup: {status}")

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
    ]
    attack_first_calldata_pending = (
        {account["label"] for account in attack_accounts}
        if args.mode == "attack" and args.attack_first_calldata_padding_bytes >= 0
        else None
    )

    print("\n========== phased workload ==========")
    global_tick = 0
    for phase, phase_blocks, send_normal, send_attack in phase_plan(args):
        if phase_blocks <= 0:
            continue
        print(f"\n========== phase: {phase} blocks={phase_blocks} ==========")
        phase_start_block = base.hex_to_int(rpc.call("eth_blockNumber"))
        phase_start_tick = global_tick + 1
        for tick in range(phase_blocks):
            current_block = base.hex_to_int(rpc.call("eth_blockNumber"))
            normal_sent_this_tick = 0
            attack_sent_this_tick = 0
            attack_active_senders_this_tick = 0

            if send_normal:
                records, normal_sent_this_tick = send_workload(
                    repo_root,
                    args,
                    "normal",
                    normal_accounts,
                    out_dir,
                    phase,
                    global_tick,
                    args.normal_rate_per_block,
                    normal_extra,
                    gas_price_sampler=normal_gas_price_sampler,
                )
                append_jsonl(out_dir / "normal_records.jsonl", records)
                normal_records.extend(records)
                cumulative_normal_submitted += normal_sent_this_tick
                cumulative_normal_accepted += scale.accepted_count(records)
                cumulative_normal_errors += sum(1 for record in records if record.get("error"))
                normal_hashes.update(accepted_hashes(records))

            if send_attack and args.mode == "attack":
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
                    )
                else:
                    records = []
                    attack_sent_this_tick = 0
                append_jsonl(out_dir / "attack_records.jsonl", records)
                attack_records.extend(records)
                cumulative_attack_submitted += attack_sent_this_tick
                cumulative_attack_accepted += scale.accepted_count(records)
                cumulative_attack_errors += sum(1 for record in records if record.get("error"))
                attack_hashes.update(accepted_hashes(records))

            status_after_send = rpc.call("txpool_status")
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
    for record in attack_records:
        sender = record.get("sender", "").lower()
        if not sender:
            continue
        receipt = attack_receipts.get(record.get("hash", "").lower())
        if receipt and receipt.get("status") == "0x1":
            per_sender[sender] = per_sender.get(sender, 0) + 1
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
        "normalGasPriceMode": args.normal_gas_price_mode,
        "normalFeeHistory": args.normal_fee_history or "",
        "normalFeeField": args.normal_fee_field if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeJitter": args.normal_fee_jitter if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeMultiplier": args.normal_fee_multiplier if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeSamplingStrategy": args.normal_fee_sampling_strategy if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeFloorWei": scale.normal_fee_floor_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeCapWei": scale.normal_fee_cap_wei(args) if args.normal_gas_price_mode == "sampled" else "",
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
        "normalGasPriceStats": normal_gas_price_generated_stats,
        "attackSenderActivation": args.attack_sender_activation if args.mode == "attack" else "",
        "attackCalldataPaddingBytes": args.attack_calldata_padding_bytes,
        "attackFirstCalldataPaddingBytes": args.attack_first_calldata_padding_bytes if args.mode == "attack" and args.attack_first_calldata_padding_bytes >= 0 else "",
        "phases": [
            {
                "name": name,
                "blocks": blocks,
                "normalEnabled": send_normal,
                "attackEnabled": send_attack,
            }
            for name, blocks, send_normal, send_attack in phase_plan(args)
        ],
        "metrics": metrics,
        "finalTxpoolStatus": final_status,
        "finalClassificationError": final_classification_error,
        "fundNormal": fund_normal,
        "fundAttack": fund_attack,
        "setcode": setcode_records,
        "timeseries": str(timeseries_path),
        "phaseMarkers": str(phase_markers_path),
        "outDir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"normalSubmitted={cumulative_normal_submitted} normalAccepted={cumulative_normal_accepted} normalIncluded={cumulative_normal_included}")
    print(f"attackSubmitted={cumulative_attack_submitted} attackAccepted={cumulative_attack_accepted} attackIncluded={cumulative_attack_included}")
    print(f"normalReceipts={len(normal_receipts)} attackReceipts={len(attack_receipts)}")
    print(f"attackSuccessfulReceiptsPerSender={mined_counts}")
    print(f"timeseries={timeseries_path}")
    print(f"phaseMarkers={phase_markers_path}")
    print(f"metrics={metrics_path}")
    print(f"summary={out_dir / 'summary.json'}")


if __name__ == "__main__":
    raise SystemExit(main())
