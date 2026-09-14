#!/usr/bin/env python3
"""
Scale version of the EIP-7702 txpool eviction experiment for default-size
Besu/Erigon txpools under Kurtosis + ethereum-package.

The small exp5 script proves the four-transaction pattern. This script scales
that pattern to thousands of normal transactions and thousands of attack
transactions while keeping txpool settings at client defaults.
"""

import argparse
import csv
import json
import math
import random
import subprocess
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace

import mp_exp5_pos as base
import mp_exp5_7702_pos as exp7702


ETHER = 10**18
GWEI = 10**9


def parse_args():
    parser = argparse.ArgumentParser(description="Run default-txpool EIP-7702 eviction scale experiment.")
    parser.add_argument("--mode", choices=["attack", "baseline"], default="attack", help="attack runs the Deter-Z2 workload; baseline sends normal transactions only.")
    parser.add_argument("--rpc", default=None, help="EL HTTP RPC URL. Defaults to Kurtosis discovery.")
    parser.add_argument("--enclave", required=True, help="Kurtosis enclave name.")
    parser.add_argument("--client", choices=["besu", "erigon"], required=True)
    parser.add_argument("--normal-count", type=int, default=7000)
    parser.add_argument("--normal-senders", type=int, default=None, help="Number of normal sender accounts. Defaults to normal-count / normal-txs-per-sender.")
    parser.add_argument("--normal-txs-per-sender", type=int, default=1)
    parser.add_argument("--normal-calldata-bytes", type=int, default=0, help="Zero-byte calldata length attached to each normal EOA transaction.")
    parser.add_argument("--attack-senders", type=int, default=1750)
    parser.add_argument("--attack-txs-per-sender", type=int, default=4)
    parser.add_argument("--attack-calldata-padding-bytes", type=int, default=0, help="Zero-byte padding appended after drainAll(address) calldata.")
    parser.add_argument("--attack-first-calldata-padding-bytes", type=int, default=-1, help="Optional zero-byte padding for the first attack transaction from each attack sender. Default -1 uses --attack-calldata-padding-bytes for every attack transaction.")
    parser.add_argument("--seed", default="exp5-7702-scale")
    parser.add_argument("--workload-seed", default=None, help="Seed for paired randomized normal workloads. Defaults to --seed.")
    parser.add_argument("--trial-id", default="", help="Optional trial label included in output paths and metrics.csv.")
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
    parser.add_argument("--attack-balance-eth", type=int, default=10)
    parser.add_argument("--attack-gas-cap-eth", type=int, default=2)
    parser.add_argument("--normal-fund-eth", type=float, default=0.1)
    parser.add_argument("--deploy-gas", type=int, default=2_000_000)
    parser.add_argument("--setcode-gas", type=int, default=250_000)
    parser.add_argument("--attack-gas", type=int, default=500_000)
    parser.add_argument("--normal-gas", type=int, default=21_000)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--setup-wait", type=int, default=240)
    parser.add_argument("--receipt-timeout", type=int, default=240)
    parser.add_argument("--fresh-block-timeout", type=int, default=90)
    parser.add_argument("--common-window-blocks", type=int, default=1, help="Common short receipt-observation window used for baseline-vs-attack comparison.")
    parser.add_argument("--post-attack-blocks", type=int, default=4)
    parser.add_argument("--solc-version", default="0.8.20")
    parser.add_argument("--evm-version", default="london")
    parser.add_argument("--go-binary", default="/usr/local/go/bin/go")
    parser.add_argument("--out-dir", default=None)
    return parser.parse_args()


def private_key_to_address(private_key):
    point = base.scalar_mult(private_key, base.SECP256K1_G)
    public = point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")
    return "0x" + base.keccak256(public)[12:].hex()


def derive_accounts(prefix, count, seed):
    accounts = []
    for index in range(count):
        material = f"{seed}:{prefix}:{index}".encode("utf-8")
        key = int.from_bytes(base.keccak256(material), "big") % (base.SECP256K1_N - 1) + 1
        accounts.append(
            {
                "label": f"{prefix}{index:05d}",
                "address": private_key_to_address(key),
                "private_key": "0x" + key.to_bytes(32, "big").hex(),
            }
        )
    return accounts


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def read_json_records(output):
    records = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        records.append(json.loads(line))
    return records


def chunked(items, size):
    for start in range(0, len(items), size):
        yield start, items[start : start + size]


def uses_sampled_normal_gas_price(args):
    if args.normal_fee_history and args.normal_gas_price_mode == "fixed":
        args.normal_gas_price_mode = "sampled"
    return args.normal_gas_price_mode == "sampled"


def parse_fee_value_to_wei(raw, field):
    if raw is None or str(raw).strip() == "":
        return None
    value = str(raw).strip()
    normalized_field = field.lower()
    if normalized_field.endswith("wei") and not normalized_field.endswith("gwei"):
        wei = int(value, 0)
    else:
        wei = int((Decimal(value) * Decimal(GWEI)).to_integral_value(rounding=ROUND_HALF_UP))
    return wei if wei > 0 else None


def load_normal_fee_values_wei(args):
    cached = getattr(args, "_normal_fee_values_wei", None)
    if cached is not None:
        return cached
    if not args.normal_fee_history:
        raise RuntimeError("--normal-fee-history is required when normal gas price mode is sampled")

    path = Path(args.normal_fee_history)
    if not path.exists():
        raise RuntimeError(f"normal fee history file not found: {path}")

    values = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if args.normal_fee_field not in (reader.fieldnames or []):
            raise RuntimeError(f"column {args.normal_fee_field!r} not found in {path}")
        for row in reader:
            wei = parse_fee_value_to_wei(row.get(args.normal_fee_field), args.normal_fee_field)
            if wei is not None:
                values.append(wei)

    if not values:
        raise RuntimeError(f"no positive values found in {path} column {args.normal_fee_field}")
    args._normal_fee_values_wei = values
    return values


def normal_gas_price_upper_bound_wei(args):
    if not uses_sampled_normal_gas_price(args):
        return args.normal_price * args.price_unit
    if args.normal_fee_jitter < 0:
        raise RuntimeError("--normal-fee-jitter must be non-negative")
    if args.normal_fee_multiplier <= 0:
        raise RuntimeError("--normal-fee-multiplier must be positive")
    values = load_normal_fee_values_wei(args)
    floor = normal_fee_floor_wei(args)
    cap = normal_fee_cap_wei(args)
    if cap and cap < floor:
        raise RuntimeError("normal-fee-cap must be greater than or equal to normal-fee-floor")
    upper = max(floor, 1, int(max(values) * args.normal_fee_multiplier * (1 + args.normal_fee_jitter)))
    return min(upper, cap) if cap else upper


def normal_fee_floor_wei(args):
    floor = args.normal_fee_floor_wei
    if floor < 0:
        raise RuntimeError("--normal-fee-floor-wei must be non-negative")
    if args.normal_fee_floor_gwei not in (None, ""):
        gwei_floor = int((Decimal(str(args.normal_fee_floor_gwei)) * Decimal(GWEI)).to_integral_value(rounding=ROUND_HALF_UP))
        floor = max(floor, gwei_floor)
    return floor


def normal_fee_cap_wei(args):
    cap = args.normal_fee_cap_wei
    if cap < 0:
        raise RuntimeError("--normal-fee-cap-wei must be non-negative")
    if args.normal_fee_cap_gwei not in (None, ""):
        gwei_cap = int((Decimal(str(args.normal_fee_cap_gwei)) * Decimal(GWEI)).to_integral_value(rounding=ROUND_HALF_UP))
        cap = min(cap, gwei_cap) if cap else gwei_cap
    return cap


def gas_price_stats(prices):
    if not prices:
        return {}
    ordered = sorted(int(price) for price in prices)
    return {
        "count": len(ordered),
        "minWei": ordered[0],
        "medianWei": ordered[len(ordered) // 2],
        "avgWei": int(sum(ordered) / len(ordered)),
        "maxWei": ordered[-1],
    }


class NormalGasPriceSampler:
    def __init__(self, args, seed):
        self.values = load_normal_fee_values_wei(args)
        self.jitter = args.normal_fee_jitter
        self.multiplier = args.normal_fee_multiplier
        self.floor_wei = normal_fee_floor_wei(args)
        self.cap_wei = normal_fee_cap_wei(args)
        self.strategy = getattr(args, "normal_fee_sampling_strategy", "per-tx")
        self.rng = random.Random(seed)
        self.generated = []
        self.sender_prices = {}
        self.sender_last_prices = {}

    def sample_one(self):
        sampled = self.rng.choice(self.values)
        factor = self.multiplier
        if self.jitter:
            factor *= 1 + self.rng.uniform(-self.jitter, self.jitter)
        price = max(self.floor_wei, 1, int(round(sampled * factor)))
        if self.cap_wei:
            price = min(price, self.cap_wei)
        return price

    def sender_key(self, account):
        return account.get("label") or account.get("address") or account.get("private_key")

    def price_for_sender(self, account):
        key = self.sender_key(account)
        if self.strategy == "per-sender":
            if key not in self.sender_prices:
                self.sender_prices[key] = self.sample_one()
            return self.sender_prices[key]
        if self.strategy == "sender-monotonic":
            price = self.sample_one()
            previous = self.sender_last_prices.get(key)
            if previous is not None:
                price = max(previous, price)
            self.sender_last_prices[key] = price
            return price
        return self.sample_one()

    def ordered_accounts_for_prices(self, accounts, txs_per_sender, send_order):
        if send_order == "round-robin":
            for _ in range(txs_per_sender):
                for account in accounts:
                    yield account
        else:
            for account in accounts:
                for _ in range(txs_per_sender):
                    yield account

    def take(self, count, accounts=None, txs_per_sender=1, send_order="account"):
        prices = []
        expected = len(accounts) * txs_per_sender if accounts is not None else None
        if self.strategy == "per-tx" or accounts is None or expected != count:
            prices = [self.sample_one() for _ in range(count)]
        else:
            for account in self.ordered_accounts_for_prices(accounts, txs_per_sender, send_order):
                prices.append(self.price_for_sender(account))
        self.generated.extend(prices)
        return prices

    def source_stats(self):
        return gas_price_stats(self.values)

    def generated_stats(self):
        return gas_price_stats(self.generated)


def build_normal_gas_price_sampler(args):
    if not uses_sampled_normal_gas_price(args):
        return None
    workload_seed = getattr(args, "workload_seed", None) or args.seed
    seed = f"{workload_seed}:{args.client}:normal-fee-sampler"
    return NormalGasPriceSampler(args, seed)


def write_gas_price_list(path, prices):
    path.write_text("\n".join(str(price) for price in prices) + "\n", encoding="utf-8")


def extra_option_value(extra, option, default):
    for index, value in enumerate(extra):
        if value == option and index + 1 < len(extra):
            return extra[index + 1]
    return default


def run_helper(repo_root, args, mode, accounts_path, extra):
    helper = "./devtools/mpfuzz_style_exp5_pos/eip7702_scale_helper.go"
    prefix = [args.helper_binary] if getattr(args, "helper_binary", None) else [args.go_binary, "run", helper]
    cmd = prefix + [
        "--mode",
        mode,
        "--rpc",
        args.rpc_url,
        "--accounts",
        str(accounts_path),
    ] + extra
    output = subprocess.check_output(cmd, cwd=repo_root, text=True)
    return read_json_records(output)


def run_helper_in_batches(repo_root, args, mode, accounts, out_dir, extra, gas_price_sampler=None, txs_per_sender=1):
    all_records = []
    for batch_index, batch in chunked(accounts, args.batch_size):
        batch_path = out_dir / f"{mode}_{batch_index:05d}.jsonl"
        write_jsonl(batch_path, batch)
        print(f"{mode}: batchStart={batch_index} batchSize={len(batch)}")
        call_extra = list(extra)
        if gas_price_sampler is not None:
            gas_prices = gas_price_sampler.take(
                len(batch) * txs_per_sender,
                accounts=batch,
                txs_per_sender=txs_per_sender,
                send_order=extra_option_value(call_extra, "--send-order", "account"),
            )
            gas_prices_path = out_dir / f"{mode}_{batch_index:05d}_gas_prices.txt"
            write_gas_price_list(gas_prices_path, gas_prices)
            call_extra += ["--gas-price-wei-list", str(gas_prices_path)]
        records = run_helper(repo_root, args, mode, batch_path, call_extra)
        errors = [record for record in records if record.get("error")]
        if errors:
            print(f"warning: {mode} batchStart={batch_index} errors={len(errors)} first={errors[0]}")
        all_records.extend(records)
    return all_records


def wait_until_block(rpc, target, timeout, start=None):
    if start is None:
        start = base.hex_to_int(rpc.call("eth_blockNumber"))
    deadline = time.time() + timeout
    print(f"waiting for block >= {target} from {start}")
    while time.time() < deadline:
        current = base.hex_to_int(rpc.call("eth_blockNumber"))
        if current >= target:
            print(f"currentBlock={current}")
            return current
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for block {target}")


def wait_blocks(rpc, count, timeout):
    start = base.hex_to_int(rpc.call("eth_blockNumber"))
    return wait_until_block(rpc, start + count, timeout, start=start)


def txpool_hashes(rpc):
    content = rpc.call("txpool_content")
    locations = base.pool_locations(content)
    return locations


def accepted_count(records):
    return sum(1 for record in records if record.get("hash") and not record.get("error"))


def successful_receipt_count(receipts):
    return sum(1 for receipt in receipts.values() if receipt.get("status") == "0x1")


def failed_receipt_count(receipts):
    return sum(1 for receipt in receipts.values() if receipt.get("status") and receipt.get("status") != "0x1")


def hex_quantity_to_int(value):
    if value in (None, ""):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)
    return int(value)


def receipt_cost_summary(receipts):
    gas_used_total = 0
    cost_wei_total = 0
    for receipt in receipts.values():
        gas_used = hex_quantity_to_int(receipt.get("gasUsed"))
        gas_price = hex_quantity_to_int(receipt.get("effectiveGasPrice") or receipt.get("gasPrice"))
        gas_used_total += gas_used
        cost_wei_total += gas_used * gas_price
    return gas_used_total, cost_wei_total


def receipts_up_to_block(receipts, block_number):
    return {
        tx_hash: receipt
        for tx_hash, receipt in receipts.items()
        if hex_quantity_to_int(receipt.get("blockNumber")) <= block_number
    }


def wei_to_eth(value):
    if value in (None, ""):
        return ""
    rendered = format(Decimal(value) / Decimal(ETHER), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def empty_location_counts():
    return {"pending": 0, "queued": 0, "dropped": 0, "rejected": 0, "errored": 0}


def status_count(status, key):
    if not status:
        return 0
    return base.hex_to_int(status.get(key, "0x0"))


def ratio(numerator, denominator):
    if denominator <= 0:
        return ""
    return f"{numerator / denominator:.6f}"


def write_metrics_csv(path, metrics):
    fieldnames = [
        "client",
        "clientVersion",
        "chainId",
        "mode",
        "trialId",
        "rpc",
        "outDir",
        "executionWindowStartBlock",
        "commonWindowBlocks",
        "commonWindowTargetBlock",
        "commonWindowObservedBlock",
        "fullWindowBlocks",
        "fullWindowTargetBlock",
        "fullWindowObservedBlock",
        "normalSubmitted",
        "normalAccepted",
        "normalRejected",
        "normalErrored",
        "normalPendingAfterNormal",
        "normalQueuedAfterNormal",
        "normalPendingAtTxpoolCheck",
        "normalQueuedAtTxpoolCheck",
        "normalMissingAtTxpoolCheck",
        "normalPendingFinal",
        "normalQueuedFinal",
        "normalMissingFromTxpoolFinal",
        "normalReceiptsCommonWindow",
        "normalSuccessfulReceiptsCommonWindow",
        "normalFailedReceiptsCommonWindow",
        "normalGasUsedCommonWindow",
        "normalCostWeiCommonWindow",
        "normalCostEthCommonWindow",
        "normalInclusionRateCommonWindow",
        "normalSuccessfulInclusionRateCommonWindow",
        "normalReceiptsFullWindow",
        "normalSuccessfulReceiptsFullWindow",
        "normalFailedReceiptsFullWindow",
        "normalGasUsedFullWindow",
        "normalCostWeiFullWindow",
        "normalCostEthFullWindow",
        "normalInclusionRateFullWindow",
        "normalSuccessfulInclusionRateFullWindow",
        "normalReceipts",
        "normalSuccessfulReceipts",
        "normalFailedReceipts",
        "normalGasUsedOnChain",
        "normalCostWei",
        "normalCostEth",
        "normalInclusionRate",
        "normalSuccessfulInclusionRate",
        "evictionRate",
        "attackSubmitted",
        "attackAccepted",
        "attackRejected",
        "attackErrored",
        "attackPendingAtTxpoolCheck",
        "attackQueuedAtTxpoolCheck",
        "attackDroppedAtTxpoolCheck",
        "attackPendingFinal",
        "attackQueuedFinal",
        "attackDroppedFinal",
        "attackPresentAfterAttack",
        "attackReceipts",
        "attackIncludedReceipts",
        "attackSuccessfulReceipts",
        "attackFailedReceipts",
        "attackSuccessfulReceiptsPerSender",
        "attackSuccessfulSenders",
        "attackGasUsedOnChain",
        "attackCostWei",
        "attackCostEth",
        "attackWorkloadExecutionCostWei",
        "attackWorkloadExecutionCostEth",
        "attackCostPerSuccessfulSenderWei",
        "attackCostPerEvictedNormalWei",
        "poolAmplification",
        "priceUnitWei",
        "setupPrice",
        "normalPrice",
        "attackerPrice",
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
        "normalGas",
        "attackGas",
        "normalCalldataBytes",
        "attackCalldataPaddingBytes",
        "attackFirstCalldataPaddingBytes",
        "normalSenders",
        "normalTxsPerSender",
        "attackSenders",
        "attackTxsPerSender",
        "postAttackBlocks",
        "txpoolContentUsed",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({field: metrics.get(field, "") for field in fieldnames})


def infer_locations_from_status(status, normal_records, attack_records):
    pending = base.hex_to_int(status.get("pending", "0x0"))
    queued = base.hex_to_int(status.get("queued", "0x0"))
    normal_accepted = accepted_count(normal_records)
    attack_accepted = accepted_count(attack_records)
    if queued == 0 and pending == normal_accepted + attack_accepted:
        normal_counts = {"pending": normal_accepted, "queued": 0, "dropped": 0, "rejected": 0, "errored": 0}
        attack_counts = {"pending": attack_accepted, "queued": 0, "dropped": 0, "rejected": 0, "errored": 0}
        normal_counts["rejected"] = sum(1 for record in normal_records if not record.get("hash") and not record.get("error"))
        attack_counts["rejected"] = sum(1 for record in attack_records if not record.get("hash") and not record.get("error"))
        normal_counts["errored"] = sum(1 for record in normal_records if record.get("error"))
        attack_counts["errored"] = sum(1 for record in attack_records if record.get("error"))
        return normal_counts, attack_counts
    return None


def count_locations(records, locations):
    counts = {"pending": 0, "queued": 0, "dropped": 0, "rejected": 0, "errored": 0}
    for record in records:
        if record.get("error"):
            counts["errored"] += 1
            continue
        tx_hash = record.get("hash")
        if not tx_hash:
            counts["rejected"] += 1
            continue
        counts[locations.get(tx_hash.lower(), "dropped")] += 1
    return counts


def count_normal_by_hash_lookup(rpc, normal_records, attack_records, status):
    """Fallback for huge txpool_content responses.

    Besu can time out returning one giant txpool_content payload when thousands
    of large-calldata transactions are present. Querying normal tx hashes one
    by one is slower, but it lets us classify normal eviction without pulling
    the entire pool JSON response at once.
    """
    normal_counts = {"pending": 0, "queued": 0, "dropped": 0, "rejected": 0, "errored": 0}
    for index, record in enumerate(normal_records, 1):
        if record.get("error"):
            normal_counts["errored"] += 1
            continue
        tx_hash = record.get("hash")
        if not tx_hash:
            normal_counts["rejected"] += 1
            continue
        tx = rpc.call("eth_getTransactionByHash", [tx_hash])
        if tx and not tx.get("blockNumber"):
            normal_counts["pending"] += 1
        else:
            normal_counts["dropped"] += 1
        if index % 500 == 0:
            print(f"normal hash lookup progress={index}/{len(normal_records)}")

    pending_total = base.hex_to_int(status.get("pending", "0x0"))
    queued_total = base.hex_to_int(status.get("queued", "0x0"))
    attack_counts = {"pending": 0, "queued": 0, "dropped": 0, "rejected": 0, "errored": 0}
    attack_counts["errored"] = sum(1 for record in attack_records if record.get("error"))
    attack_counts["rejected"] = sum(1 for record in attack_records if not record.get("hash") and not record.get("error"))
    attack_accepted = accepted_count(attack_records)
    if queued_total == 0:
        attack_counts["pending"] = max(0, pending_total - normal_counts["pending"])
        attack_counts["dropped"] = max(0, attack_accepted - attack_counts["pending"])
    else:
        attack_counts["pending"] = min(attack_accepted, pending_total)
        attack_counts["queued"] = min(attack_accepted - attack_counts["pending"], queued_total)
        attack_counts["dropped"] = max(0, attack_accepted - attack_counts["pending"] - attack_counts["queued"])
    return normal_counts, attack_counts


def receipt_statuses(rpc, records, timeout):
    deadline = time.time() + timeout
    remaining = [record for record in records if record.get("hash") and not record.get("error")]
    receipts = {}
    while remaining and time.time() < deadline:
        next_remaining = []
        for record in remaining:
            receipt = rpc.call("eth_getTransactionReceipt", [record["hash"]])
            if receipt:
                receipts[record["hash"].lower()] = receipt
            else:
                next_remaining.append(record)
        remaining = next_remaining
        if remaining:
            time.sleep(1)
    return receipts


def make_deploy_args(args):
    return SimpleNamespace(
        case="alice-sender",
        rpc_url=args.rpc_url,
        deploy_gas=args.deploy_gas,
        setup_price=args.setup_price,
        price_unit=args.price_unit,
        receipt_timeout=args.receipt_timeout,
        solc_version=args.solc_version,
        evm_version=args.evm_version,
    )


def main():
    args = parse_args()
    is_attack_mode = args.mode == "attack"
    if args.normal_count <= 0 or args.normal_txs_per_sender <= 0:
        raise RuntimeError("normal counts must be positive")
    if is_attack_mode and (args.attack_senders <= 0 or args.attack_txs_per_sender <= 0):
        raise RuntimeError("attack counts must be positive")
    if args.common_window_blocks <= 0 or args.post_attack_blocks <= 0:
        raise RuntimeError("common-window-blocks and post-attack-blocks must be positive")
    if args.common_window_blocks > args.post_attack_blocks:
        raise RuntimeError("common-window-blocks must be <= post-attack-blocks")
    if args.normal_calldata_bytes < 0 or args.attack_calldata_padding_bytes < 0:
        raise RuntimeError("calldata byte counts must be non-negative")
    if args.attack_first_calldata_padding_bytes < -1:
        raise RuntimeError("attack-first-calldata-padding-bytes must be -1 or non-negative")
    min_normal_gas = 21_000 + args.normal_calldata_bytes * 4
    if args.normal_gas < min_normal_gas:
        raise RuntimeError(f"normal-gas is too low for {args.normal_calldata_bytes} zero calldata bytes: need at least {min_normal_gas}")
    if args.normal_senders is None:
        if args.normal_count % args.normal_txs_per_sender != 0:
            raise RuntimeError("normal-count must be divisible by normal-txs-per-sender when normal-senders is omitted")
        args.normal_senders = args.normal_count // args.normal_txs_per_sender
    if args.normal_senders <= 0:
        raise RuntimeError("normal-senders must be positive")
    normal_total = args.normal_senders * args.normal_txs_per_sender
    if normal_total != args.normal_count:
        raise RuntimeError("normal-count must equal normal-senders * normal-txs-per-sender")
    attack_total = args.attack_senders * args.attack_txs_per_sender
    if is_attack_mode and attack_total < args.normal_count:
        raise RuntimeError("attack_senders * attack_txs_per_sender must be >= normal_count")

    if args.price_unit is None:
        args.price_unit = (args.attack_gas_cap_eth * ETHER) // (args.attacker_price * args.attack_gas)
    normal_required_gas_price = normal_gas_price_upper_bound_wei(args)
    normal_required_wei = args.normal_txs_per_sender * (normal_required_gas_price * args.normal_gas + 1)
    normal_fund_wei_arg = int(args.normal_fund_eth * ETHER)
    if normal_fund_wei_arg < normal_required_wei:
        raise RuntimeError(f"normal-fund-eth is too low for {args.normal_txs_per_sender} normal txs per sender: need at least {normal_required_wei} wei")
    if is_attack_mode:
        attack_required_wei = args.attack_txs_per_sender * args.attacker_price * args.price_unit * args.attack_gas
        attack_fund_wei_arg = args.attack_balance_eth * ETHER
        if attack_fund_wei_arg < attack_required_wei:
            raise RuntimeError(f"attack-balance-eth is too low for {args.attack_txs_per_sender} attack txs per sender: need at least {attack_required_wei} wei")

    normal_gas_price_sampler = build_normal_gas_price_sampler(args)

    args.rpc_url = args.rpc or base.discover_kurtosis_rpc(args.enclave)

    exp_dir = Path(__file__).resolve().parent
    repo_root = exp_dir.parents[1]
    accounts = base.load_key_csv(exp_dir / "key_prive_exp5.csv")
    funder = accounts[base.DEFAULT_FUNDER["label"]]
    receiver = accounts["x"]
    rpc = base.RpcClient(args.rpc_url)
    chain_id = base.hex_to_int(rpc.call("eth_chainId"))
    client = rpc.call("web3_clientVersion")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    trial_part = f"_{args.trial_id}" if args.trial_id else ""
    out_dir = Path(args.out_dir) if args.out_dir else exp_dir / "scale_runs" / f"{args.client}_{args.mode}{trial_part}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    normal_accounts = derive_accounts('n', args.normal_senders, args.seed + ':' + args.client)
    attack_accounts = derive_accounts("a", args.attack_senders, args.seed + ":" + args.client) if is_attack_mode else []
    normal_path = out_dir / "normal_accounts.jsonl"
    attack_path = out_dir / "attack_accounts.jsonl"
    write_jsonl(normal_path, normal_accounts)
    write_jsonl(attack_path, attack_accounts)

    print(f"RPC: {args.rpc_url}")
    print(f"client={client}")
    print(f"chainId={chain_id}")
    print(f"outDir={out_dir}")
    print(f"mode={args.mode}")
    print(f'normalCount={args.normal_count}')
    print(f'normalSenders={len(normal_accounts)} normalTxsPerSender={args.normal_txs_per_sender}')
    print(f"attackSenders={len(attack_accounts)} attackTxsPerSender={args.attack_txs_per_sender}")
    if is_attack_mode and args.attack_first_calldata_padding_bytes >= 0:
        print(f"attackFirstCalldataPaddingBytes={args.attack_first_calldata_padding_bytes}")
    print(f"commonWindowBlocks={args.common_window_blocks} fullWindowBlocks={args.post_attack_blocks}")
    print(f"priceUnitWei={args.price_unit}")
    print(f"normalGasPriceMode={args.normal_gas_price_mode}")
    if normal_gas_price_sampler:
        print(f"normalFeeHistory={args.normal_fee_history}")
        print(f"normalFeeField={args.normal_fee_field}")
        print(f"normalFeeJitter={args.normal_fee_jitter}")
        print(f"normalFeeMultiplier={args.normal_fee_multiplier}")
        print(f"normalFeeSamplingStrategy={normal_gas_price_sampler.strategy}")
        print(f"normalFeeFloorWei={normal_fee_floor_wei(args)}")
        print(f"normalFeeCapWei={normal_fee_cap_wei(args)}")
        print(f"normalFeeSourceStats={normal_gas_price_sampler.source_stats()}")
    print(f"receiver={receiver['address']} balanceWei={base.balance(rpc, receiver['address'])}")

    status = rpc.call("txpool_status")
    if base.hex_to_int(status["pending"]) or base.hex_to_int(status["queued"]):
        raise RuntimeError(f"txpool is not empty before setup: {status}")

    setup_gas_price = str(args.setup_price * args.price_unit)
    normal_gas_price = str(args.normal_price * args.price_unit)
    attack_gas_price = str(args.attacker_price * args.price_unit)
    normal_fund_wei = str(int(args.normal_fund_eth * ETHER))
    attack_fund_wei = str(args.attack_balance_eth * ETHER)
    delegate = None

    if is_attack_mode:
        print("\n========== deploy Alice7702Drain implementation ==========")
        delegate = exp7702.deploy_delegate(rpc, chain_id, accounts, make_deploy_args(args))

    print("\n========== setup: fund generated normal senders ==========")
    fund_normal = run_helper_in_batches(
        repo_root,
        args,
        "fund",
        normal_accounts,
        out_dir,
        [
            "--sponsor-key",
            funder["private_key"],
            "--value-wei",
            normal_fund_wei,
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
    if is_attack_mode:
        print("\n========== setup: fund generated attack senders ==========")
        fund_attack = run_helper_in_batches(
            repo_root,
            args,
            "fund",
            attack_accounts,
            out_dir,
            [
                "--sponsor-key",
                funder["private_key"],
                "--value-wei",
                attack_fund_wei,
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
        setcode_records = run_helper_in_batches(
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

    wait_blocks(rpc, 1, args.fresh_block_timeout)
    status = rpc.call("txpool_status")
    if base.hex_to_int(status["pending"]) or base.hex_to_int(status["queued"]):
        print(f"warning: txpool not empty after setup: {status}")

    print("\n========== workload: send normal transactions ==========")
    normal_records = run_helper_in_batches(
        repo_root,
        args,
        "normal",
        normal_accounts,
        out_dir,
        [
            "--value-wei",
            "1",
            "--gas-price-wei",
            normal_gas_price,
            "--gas",
            str(args.normal_gas),
            "--calldata-bytes",
            str(args.normal_calldata_bytes),
            '--txs-per-sender',
            str(args.normal_txs_per_sender),
        ],
        gas_price_sampler=normal_gas_price_sampler,
        txs_per_sender=args.normal_txs_per_sender,
    )
    if normal_gas_price_sampler:
        normal_gas_price_generated_stats = normal_gas_price_sampler.generated_stats()
    else:
        normal_gas_price_generated_stats = gas_price_stats([int(normal_gas_price)] * accepted_count(normal_records))
    normal_records_path = out_dir / "normal_records.jsonl"
    write_jsonl(normal_records_path, normal_records)
    status_after_normal = rpc.call("txpool_status")
    print(f"afterNormal txpool={status_after_normal}")

    attack_records = []
    attack_records_path = out_dir / "attack_records.jsonl"
    write_jsonl(attack_records_path, attack_records)
    status_after_attack = None
    if is_attack_mode:
        print("\n========== workload: send attack transactions ==========")
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
            "--txs-per-sender",
            str(args.attack_txs_per_sender),
        ]
        if args.attack_first_calldata_padding_bytes >= 0:
            attack_extra += ["--first-calldata-bytes", str(args.attack_first_calldata_padding_bytes)]
        attack_records = run_helper_in_batches(
            repo_root,
            args,
            "attack",
            attack_accounts,
            out_dir,
            attack_extra,
        )
        write_jsonl(attack_records_path, attack_records)
        status_after_attack = rpc.call("txpool_status")
        print(f"afterAttack txpool={status_after_attack}")

    print("\n========== txpool location summary ==========")
    location_status = status_after_attack if is_attack_mode else status_after_normal
    inferred_counts = infer_locations_from_status(location_status, normal_records, attack_records)
    if inferred_counts:
        normal_counts, attack_counts = inferred_counts
        txpool_content_used = False
        print("txpoolContent=skipped: txpool_status already accounts for all accepted transactions")
    else:
        try:
            locations = txpool_hashes(rpc)
            normal_counts = count_locations(normal_records, locations)
            attack_counts = count_locations(attack_records, locations)
            txpool_content_used = True
        except TimeoutError:
            print("txpoolContent=timeout: falling back to per-normal-hash lookup")
            normal_counts, attack_counts = count_normal_by_hash_lookup(rpc, normal_records, attack_records, location_status)
            txpool_content_used = False
    print(f"normalLocations={normal_counts}")
    print(f"attackLocations={attack_counts}")
    if is_attack_mode and normal_counts["pending"] == 0 and normal_counts["queued"] == 0 and attack_counts["pending"] >= args.normal_count:
        print("PASS: default txpool no longer contains the initial normal transactions after attack fill")
    elif not is_attack_mode and normal_counts["pending"] + normal_counts["queued"] == accepted_count(normal_records):
        print("PASS: baseline normal transactions are present in the txpool before block inclusion")
    else:
        print("CHECK: txpool did not reach the expected pattern for this mode")

    wait_label = "attack execution" if is_attack_mode else "baseline execution"
    execution_window_start_block = base.hex_to_int(rpc.call("eth_blockNumber"))
    common_window_target_block = execution_window_start_block + args.common_window_blocks
    full_window_target_block = execution_window_start_block + args.post_attack_blocks
    print(f"\n========== wait for {wait_label}: common window ==========")
    common_window_observed_block = wait_until_block(
        rpc,
        common_window_target_block,
        args.common_window_blocks * args.fresh_block_timeout,
        start=execution_window_start_block,
    )
    full_window_observed_block = common_window_observed_block
    if args.post_attack_blocks > args.common_window_blocks:
        print(f"\n========== wait for {wait_label}: full window ==========")
        full_window_observed_block = wait_until_block(
            rpc,
            full_window_target_block,
            (args.post_attack_blocks - args.common_window_blocks) * args.fresh_block_timeout,
        )

    raw_attack_receipts = receipt_statuses(rpc, attack_records, args.receipt_timeout) if is_attack_mode else {}
    attack_receipts_common = receipts_up_to_block(raw_attack_receipts, common_window_target_block)
    receipts = receipts_up_to_block(raw_attack_receipts, full_window_target_block)
    (out_dir / "attack_receipts_common_window.json").write_text(json.dumps(attack_receipts_common, indent=2), encoding="utf-8")
    receipts_path = out_dir / "attack_receipts.json"
    receipts_path.write_text(json.dumps(receipts, indent=2), encoding="utf-8")
    per_sender = {}
    for record in attack_records:
        sender = record.get("sender", "").lower()
        if not sender:
            continue
        receipt = receipts.get(record.get("hash", "").lower())
        if receipt and receipt.get("status") == "0x1":
            per_sender[sender] = per_sender.get(sender, 0) + 1
        else:
            per_sender.setdefault(sender, 0)
    mined_counts = {}
    for count in per_sender.values():
        mined_counts[count] = mined_counts.get(count, 0) + 1
    normal_receipts_timeout = 1 if is_attack_mode else args.receipt_timeout
    raw_normal_receipts = receipt_statuses(rpc, normal_records, normal_receipts_timeout)
    normal_receipts_common = receipts_up_to_block(raw_normal_receipts, common_window_target_block)
    normal_receipts = receipts_up_to_block(raw_normal_receipts, full_window_target_block)
    (out_dir / "normal_receipts_common_window.json").write_text(json.dumps(normal_receipts_common, indent=2), encoding="utf-8")
    (out_dir / "normal_receipts.json").write_text(json.dumps(normal_receipts, indent=2), encoding="utf-8")
    normal_successful_receipts_common = successful_receipt_count(normal_receipts_common)
    normal_failed_receipts_common = failed_receipt_count(normal_receipts_common)
    normal_gas_used_common, normal_cost_wei_common = receipt_cost_summary(normal_receipts_common)
    normal_successful_receipts = successful_receipt_count(normal_receipts)
    normal_failed_receipts = failed_receipt_count(normal_receipts)
    attack_successful_receipts = successful_receipt_count(receipts)
    attack_failed_receipts = failed_receipt_count(receipts)
    normal_gas_used, normal_cost_wei = receipt_cost_summary(normal_receipts)
    attack_gas_used, attack_cost_wei = receipt_cost_summary(receipts)
    attack_successful_senders = sum(1 for count in per_sender.values() if count > 0)
    attack_cost_per_successful_sender = attack_cost_wei // attack_successful_senders if attack_successful_senders else ""
    attack_cost_per_evicted_normal = attack_cost_wei // normal_counts["dropped"] if normal_counts["dropped"] else ""
    attack_present_after_attack = attack_counts["pending"] + attack_counts["queued"]
    pool_amplification = ratio(attack_present_after_attack, attack_successful_receipts) if attack_successful_receipts else ""
    normal_rejected = sum(1 for record in normal_records if not record.get("hash") and not record.get("error"))
    attack_rejected = sum(1 for record in attack_records if not record.get("hash") and not record.get("error"))
    normal_errored = sum(1 for record in normal_records if record.get("error"))
    attack_errored = sum(1 for record in attack_records if record.get("error"))
    print(f"normalReceiptsCommonWindow={len(normal_receipts_common)}")
    print(f"normalReceiptsFullWindow={len(normal_receipts)}")
    print(f"attackIncludedReceipts={len(receipts)}")
    print(f"attackSuccessfulReceipts={sum(per_sender.values())}")
    print(f"attackSuccessfulReceiptsPerSender={mined_counts}")
    print(f"poolAmplification={pool_amplification}")
    if is_attack_mode and mined_counts.get(1) == len(attack_accounts) and len(mined_counts) == 1 and len(normal_receipts) == 0:
        print("PASS: every attack sender has exactly one successful on-chain transaction and normal transactions have no receipts")
    elif not is_attack_mode and len(normal_receipts) > 0:
        print("PASS: baseline produced normal transaction receipts during the observation window")
    else:
        print("CHECK: execution result differs from the expected pattern for this mode")

    metrics = {
        "client": args.client,
        "clientVersion": client,
        "chainId": chain_id,
        "mode": args.mode,
        "trialId": args.trial_id,
        "rpc": args.rpc_url,
        "outDir": str(out_dir),
        "executionWindowStartBlock": execution_window_start_block,
        "commonWindowBlocks": args.common_window_blocks,
        "commonWindowTargetBlock": common_window_target_block,
        "commonWindowObservedBlock": common_window_observed_block,
        "fullWindowBlocks": args.post_attack_blocks,
        "fullWindowTargetBlock": full_window_target_block,
        "fullWindowObservedBlock": full_window_observed_block,
        "normalSubmitted": args.normal_count,
        "normalAccepted": accepted_count(normal_records),
        "normalRejected": normal_rejected,
        "normalErrored": normal_errored,
        "normalPendingAfterNormal": status_count(status_after_normal, "pending"),
        "normalQueuedAfterNormal": status_count(status_after_normal, "queued"),
        "normalPendingAtTxpoolCheck": normal_counts["pending"],
        "normalQueuedAtTxpoolCheck": normal_counts["queued"],
        "normalMissingAtTxpoolCheck": normal_counts["dropped"],
        "normalPendingFinal": normal_counts["pending"],
        "normalQueuedFinal": normal_counts["queued"],
        "normalMissingFromTxpoolFinal": normal_counts["dropped"],
        "normalReceiptsCommonWindow": len(normal_receipts_common),
        "normalSuccessfulReceiptsCommonWindow": normal_successful_receipts_common,
        "normalFailedReceiptsCommonWindow": normal_failed_receipts_common,
        "normalGasUsedCommonWindow": normal_gas_used_common,
        "normalCostWeiCommonWindow": normal_cost_wei_common,
        "normalCostEthCommonWindow": wei_to_eth(normal_cost_wei_common),
        "normalInclusionRateCommonWindow": ratio(len(normal_receipts_common), args.normal_count),
        "normalSuccessfulInclusionRateCommonWindow": ratio(normal_successful_receipts_common, args.normal_count),
        "normalReceiptsFullWindow": len(normal_receipts),
        "normalSuccessfulReceiptsFullWindow": normal_successful_receipts,
        "normalFailedReceiptsFullWindow": normal_failed_receipts,
        "normalGasUsedFullWindow": normal_gas_used,
        "normalCostWeiFullWindow": normal_cost_wei,
        "normalCostEthFullWindow": wei_to_eth(normal_cost_wei),
        "normalInclusionRateFullWindow": ratio(len(normal_receipts), args.normal_count),
        "normalSuccessfulInclusionRateFullWindow": ratio(normal_successful_receipts, args.normal_count),
        "normalReceipts": len(normal_receipts),
        "normalSuccessfulReceipts": normal_successful_receipts,
        "normalFailedReceipts": normal_failed_receipts,
        "normalGasUsedOnChain": normal_gas_used,
        "normalCostWei": normal_cost_wei,
        "normalCostEth": wei_to_eth(normal_cost_wei),
        "normalInclusionRate": ratio(len(normal_receipts), args.normal_count),
        "normalSuccessfulInclusionRate": ratio(normal_successful_receipts, args.normal_count),
        "evictionRate": ratio(normal_counts["dropped"], args.normal_count) if is_attack_mode else "",
        "attackSubmitted": attack_total if is_attack_mode else 0,
        "attackAccepted": accepted_count(attack_records),
        "attackRejected": attack_rejected,
        "attackErrored": attack_errored,
        "attackPendingAtTxpoolCheck": attack_counts["pending"],
        "attackQueuedAtTxpoolCheck": attack_counts["queued"],
        "attackDroppedAtTxpoolCheck": attack_counts["dropped"],
        "attackPendingFinal": attack_counts["pending"],
        "attackQueuedFinal": attack_counts["queued"],
        "attackDroppedFinal": attack_counts["dropped"],
        "attackPresentAfterAttack": attack_present_after_attack,
        "attackReceipts": len(receipts),
        "attackIncludedReceipts": len(receipts),
        "attackSuccessfulReceipts": attack_successful_receipts,
        "attackFailedReceipts": attack_failed_receipts,
        "attackSuccessfulReceiptsPerSender": json.dumps(mined_counts, sort_keys=True, separators=(",", ":")),
        "attackSuccessfulSenders": attack_successful_senders,
        "attackGasUsedOnChain": attack_gas_used,
        "attackCostWei": attack_cost_wei,
        "attackCostEth": wei_to_eth(attack_cost_wei),
        "attackWorkloadExecutionCostWei": attack_cost_wei,
        "attackWorkloadExecutionCostEth": wei_to_eth(attack_cost_wei),
        "attackCostPerSuccessfulSenderWei": attack_cost_per_successful_sender,
        "attackCostPerEvictedNormalWei": attack_cost_per_evicted_normal,
        "poolAmplification": pool_amplification,
        "priceUnitWei": args.price_unit,
        "setupPrice": args.setup_price,
        "normalPrice": args.normal_price,
        "attackerPrice": args.attacker_price,
        "normalGasPriceMode": args.normal_gas_price_mode,
        "normalFeeHistory": args.normal_fee_history or "",
        "normalFeeField": args.normal_fee_field if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeJitter": args.normal_fee_jitter if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeMultiplier": args.normal_fee_multiplier if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeSamplingStrategy": args.normal_fee_sampling_strategy if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeFloorWei": normal_fee_floor_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeCapWei": normal_fee_cap_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "normalGasPriceMinWei": normal_gas_price_generated_stats.get("minWei", ""),
        "normalGasPriceMedianWei": normal_gas_price_generated_stats.get("medianWei", ""),
        "normalGasPriceAvgWei": normal_gas_price_generated_stats.get("avgWei", ""),
        "normalGasPriceMaxWei": normal_gas_price_generated_stats.get("maxWei", ""),
        "normalGas": args.normal_gas,
        "attackGas": args.attack_gas,
        "normalCalldataBytes": args.normal_calldata_bytes,
        "attackCalldataPaddingBytes": args.attack_calldata_padding_bytes,
        "attackFirstCalldataPaddingBytes": args.attack_first_calldata_padding_bytes if is_attack_mode and args.attack_first_calldata_padding_bytes >= 0 else "",
        "normalSenders": len(normal_accounts),
        "normalTxsPerSender": args.normal_txs_per_sender,
        "attackSenders": len(attack_accounts),
        "attackTxsPerSender": args.attack_txs_per_sender,
        "postAttackBlocks": args.post_attack_blocks,
        "txpoolContentUsed": txpool_content_used,
    }
    metrics_path = out_dir / "metrics.csv"
    write_metrics_csv(metrics_path, metrics)

    summary = {
        "client": args.client,
        "rpc": args.rpc_url,
        "chainId": chain_id,
        "clientVersion": client,
        "mode": args.mode,
        "delegate": delegate,
        "normalCount": args.normal_count,
        "normalSenders": len(normal_accounts),
        "normalTxsPerSender": args.normal_txs_per_sender,
        "normalCalldataBytes": args.normal_calldata_bytes,
        "normalGas": args.normal_gas,
        "normalGasPriceMode": args.normal_gas_price_mode,
        "normalFeeHistory": args.normal_fee_history,
        "normalFeeField": args.normal_fee_field if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeJitter": args.normal_fee_jitter if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeMultiplier": args.normal_fee_multiplier if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeSamplingStrategy": args.normal_fee_sampling_strategy if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeFloorWei": normal_fee_floor_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "normalFeeCapWei": normal_fee_cap_wei(args) if args.normal_gas_price_mode == "sampled" else "",
        "normalGasPriceStats": normal_gas_price_generated_stats,
        "attackSenders": len(attack_accounts),
        "attackTxsPerSender": args.attack_txs_per_sender,
        "attackCalldataPaddingBytes": args.attack_calldata_padding_bytes,
        "attackFirstCalldataPaddingBytes": args.attack_first_calldata_padding_bytes if is_attack_mode and args.attack_first_calldata_padding_bytes >= 0 else "",
        "executionWindowStartBlock": execution_window_start_block,
        "commonWindowBlocks": args.common_window_blocks,
        "commonWindowTargetBlock": common_window_target_block,
        "commonWindowObservedBlock": common_window_observed_block,
        "fullWindowBlocks": args.post_attack_blocks,
        "fullWindowTargetBlock": full_window_target_block,
        "fullWindowObservedBlock": full_window_observed_block,
        "afterNormal": status_after_normal,
        "afterAttack": status_after_attack,
        "normalLocations": normal_counts,
        "attackLocations": attack_counts,
        "txpoolContentUsed": txpool_content_used,
        "normalReceiptsCommonWindow": len(normal_receipts_common),
        "normalReceiptsFullWindow": len(normal_receipts),
        "attackIncludedReceipts": len(receipts),
        "attackSuccessfulReceiptsPerSender": mined_counts,
        "attackSuccessfulReceipts": attack_successful_receipts,
        "normalReceipts": len(normal_receipts),
        "poolAmplification": pool_amplification,
        "metrics": metrics,
        "outDir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"metrics={metrics_path}")
    print(f"summary={out_dir / 'summary.json'}")


if __name__ == "__main__":
    raise SystemExit(main())
