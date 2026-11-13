#!/usr/bin/env python3
"""Matched layertrace workloads with selectable drain or no-drain delegate."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
LEGACY = HERE.parents[1] / "devtools" / "mpfuzz_style_exp5_pos"
sys.path.insert(0, str(LEGACY))
import mp_exp5_pos as base
import mp_exp5_7702_pos as delegate_tools
import mp_exp5_7702_scale_pos as scale


def validate_funder_key():
    """Check the local devnet sponsor before any network is started or tx is sent."""
    key = os.environ.get("EXP5_FUNDER_PRIVATE_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Missing EXP5_FUNDER_PRIVATE_KEY in this terminal. The key CSV only supplies a/x; "
            "set the funded0 devnet private key locally, then run prepare again. "
            "Never paste the key into logs or chat."
        )
    try:
        from eth_account import Account
        signer_address = Account.from_key(key).address
    except (ValueError, TypeError) as exc:
        raise RuntimeError("EXP5_FUNDER_PRIVATE_KEY is not a valid 32-byte private key") from None
    if signer_address.lower() != base.DEFAULT_FUNDER["address"].lower():
        raise RuntimeError("EXP5_FUNDER_PRIVATE_KEY does not match the funded0 devnet address")
    base.DEFAULT_FUNDER["private_key"] = key


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class Rpc:
    def __init__(self, url):
        self.url = url

    def request(self, body):
        request = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        # Only read-only calls use this client. Never retry ambiguous submissions here.
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except (OSError, ValueError):
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)

    def call(self, method, params=None):
        response = self.request(dict(jsonrpc="2.0", id=1, method=method, params=params or []))
        if "error" in response:
            raise RuntimeError(f"{method}: {response['error']}")
        return response["result"]

    def batch(self, method, arguments):
        if not arguments:
            return []
        response = self.request([dict(jsonrpc="2.0", id=i, method=method, params=params) for i, params in enumerate(arguments)])
        if not isinstance(response, list):
            raise RuntimeError(f"{method}: batch response is not a list")
        by_id = {r["id"]: r for r in response}
        if len(by_id) != len(arguments) or len(response) != len(arguments):
            raise RuntimeError(f"{method}: incomplete/duplicate batch response")
        if any("error" in r for r in response):
            raise RuntimeError(f"{method}: RPC batch contains errors")
        return [by_id[i]["result"] for i in range(len(arguments))]

    def height(self):
        return int(self.call("eth_blockNumber"), 16)


def wait_block(rpc, target, timeout=1200):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = rpc.height()
        if current >= target:
            return current
        time.sleep(1)
    raise TimeoutError(f"block {target} not reached in {timeout}s")


def private_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        os.chmod(path, 0o600)
        for row in rows:
            f.write(json.dumps(row) + "\n")


def send(helper, rpc, out, stage, accounts, flags, expected):
    account_path = out / "private" / (stage + "_accounts.jsonl")
    private_jsonl(account_path, accounts)
    command = [str(helper), "--rpc", rpc.url, "--accounts", str(account_path), "--timeout", "30m"] + flags
    before = dict(stage=stage, unixMillis=int(time.time() * 1000), block=rpc.height())
    start = time.monotonic()
    # Never print the command: setup arguments contain the local devnet sponsor key.
    with (out / (stage + ".jsonl")).open("w") as stdout, (out / (stage + ".stderr.log")).open("w") as stderr:
        process = subprocess.run(command, stdout=stdout, stderr=stderr)
    records = scale.read_json_records((out / (stage + ".jsonl")).read_text())
    marker = dict(before=before, afterUnixMillis=int(time.time() * 1000), seconds=time.monotonic() - start,
                  returnCode=process.returncode, records=len(records), expected=expected)
    save(out / (stage + "_timing.json"), marker)
    marker["afterBlock"] = rpc.height()
    marker["crossedBlocks"] = marker["afterBlock"] - before["block"]
    save(out / (stage + "_timing.json"), marker)
    print(f"{stage}: records={len(records)}/{expected}, seconds={marker['seconds']:.2f}, crossedBlocks={marker['crossedBlocks']}", flush=True)
    errors = [r for r in records if r.get("error") or not r.get("hash")]
    if process.returncode or errors or len(records) != expected:
        raise RuntimeError(f"{stage}: incomplete submission; preserve {stage}.jsonl and .stderr.log; no automatic resubmission")
    return records


def receipts(rpc, records):
    found = []
    for offset in range(0, len(records), 100):
        batch = records[offset:offset + 100]
        replies = rpc.batch("eth_getTransactionReceipt", [[r["hash"]] for r in batch])
        found.extend(dict(kind=r["kind"], label=r["label"], hash=r["hash"], receipt=receipt)
                     for r, receipt in zip(batch, replies))
    return found


def sender_snapshot(rpc, accounts, block):
    tag = hex(block)
    rows = []
    for offset in range(0, len(accounts), 100):
        batch = accounts[offset:offset + 100]
        balances = rpc.batch("eth_getBalance", [[a["address"], tag] for a in batch])
        nonces = rpc.batch("eth_getTransactionCount", [[a["address"], tag] for a in batch])
        codes = rpc.batch("eth_getCode", [[a["address"], tag] for a in batch])
        rows.extend(dict(label=a["label"], address=a["address"], block=block, balanceWei=str(int(b, 16)),
                         nonce=int(n, 16), code=c) for a, b, n, c in zip(batch, balances, nonces, codes))
    return rows


def validate_setup(rows, config, delegate):
    for row in rows:
        attack = row["label"].startswith("a")
        price = config["priceUnitWei"] * config["attackPriceMultiplier" if attack else "normalPriceMultiplier"]
        gas = config["attackGas" if attack else "normalGas"]
        required = config["txsPerSender"] * (price * gas + (0 if attack else 1))
        if int(row["balanceWei"]) < required:
            raise RuntimeError(f"{row['label']}: insufficient cumulative upfront balance")
        expected_code = "0xef0100" + delegate.lower().removeprefix("0x") if attack else "0x"
        if row["code"].lower() != expected_code:
            raise RuntimeError(f"{row['label']}: unexpected delegation/code")
        if row["nonce"] != (1 if attack else 0):
            raise RuntimeError(f"{row['label']}: not a fresh experimental account")


def fee_wei(receipt):
    return int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)


def block_point(rpc, number, genesis_timestamp, seconds_per_slot):
    header = rpc.call("eth_getBlockByNumber", [hex(number), False])
    timestamp = int(header["timestamp"], 16)
    delta = timestamp - genesis_timestamp
    return dict(blockNumber=number, blockHash=header["hash"], timestamp=timestamp,
                slot=(delta // seconds_per_slot if delta >= 0 and delta % seconds_per_slot == 0 else None),
                slotSource="execution_timestamp_minus_genesis_timestamp")


def first_attack_balances(rpc, records, receipt_rows, setup_rows, final_rows):
    by_hash = {row["hash"].lower(): row["receipt"] for row in receipt_rows}
    setup = {row["address"].lower(): row for row in setup_rows}
    final = {row["address"].lower(): row for row in final_rows}
    included_in_block = {}
    for row in records:
        receipt = by_hash.get(row["hash"].lower())
        if receipt:
            key = (row["sender"].lower(), int(receipt["blockNumber"], 16))
            included_in_block[key] = included_in_block.get(key, 0) + 1
    rows = []
    for record in records:
        if not record["label"].endswith("-x1"):
            continue
        sender = record["sender"].lower()
        receipt = by_hash.get(record["hash"].lower())
        row = dict(sender=record["sender"], label=record["label"], firstHash=record["hash"],
                   setupBalanceWei=setup[sender]["balanceWei"],
                   windowBalanceWei=final[sender]["balanceWei"],
                   firstIncludedBlock=None, firstReceiptStatus=None,
                   beforeBlockBalanceWei=None, afterBlockBalanceWei=None,
                   sameSenderIncludedInFirstBlock=None)
        if receipt:
            block = int(receipt["blockNumber"], 16)
            row.update(firstIncludedBlock=block, firstReceiptStatus=receipt["status"],
                       beforeBlockBalanceWei=str(int(rpc.call("eth_getBalance", [record["sender"], hex(block - 1)]), 16)),
                       afterBlockBalanceWei=str(int(rpc.call("eth_getBalance", [record["sender"], hex(block)]), 16)),
                       sameSenderIncludedInFirstBlock=included_in_block[(sender, block)])
        rows.append(row)
    return rows


def run(args):
    os.umask(0o077)
    validate_funder_key()
    config = json.loads(args.config.read_text())
    out = args.out.resolve()
    if (out / "workload_manifest.json").exists():
        raise RuntimeError("This output already contains a workload; use a fresh network and directory")
    out.mkdir(parents=True, exist_ok=True)
    (out / "private").mkdir(mode=0o700, exist_ok=True)
    rpc = Rpc(args.rpc)
    version = rpc.call("web3_clientVersion")
    if not version.startswith("besu/v26.6.1/"):
        raise RuntimeError(f"Expected Besu 26.6.1; received {version}")
    chain_id = int(rpc.call("eth_chainId"), 16)
    if chain_id != 13371349:
        raise RuntimeError("Expected the generated local devnet chain ID 13371349")
    accounts = base.load_key_csv(args.key_csv)
    funder = accounts[base.DEFAULT_FUNDER["label"]]
    normal = scale.derive_accounts("n", config["normalSenders"], args.seed + ":besu")
    attackers = scale.derive_accounts("a", config["attackSenders"], args.seed + ":besu")
    total_funding = (config["normalSenders"] * config["normalFundEth"] + config["attackSenders"] * config["attackFundEth"]) * 10**18
    if int(rpc.call("eth_getBalance", [funder["address"], "latest"]), 16) < total_funding + 10**18:
        raise RuntimeError("Local devnet funder cannot cover matched setup")
    genesis = rpc.call("eth_getBlockByNumber", ["0x0", False])
    genesis_timestamp = int(genesis["timestamp"], 16)
    save(out / "workload_manifest.json", dict(config=config, group=args.group, mode=args.mode,
         delegateCase=args.delegate_case, seed=args.seed, rpc=args.rpc,
         clientVersion=version, chainId=chain_id, genesis=genesis,
         accounts=[{k: a[k] for k in ("label", "address")} for a in normal + attackers]))
    wait_block(rpc, rpc.height() + 1)
    setup_price = str(config["setupGasPriceWei"])
    deploy_args = SimpleNamespace(case=args.delegate_case, rpc_url=args.rpc, deploy_gas=2000000,
        setup_price=int(setup_price), price_unit=1, receipt_timeout=1200, solc_version="0.8.20", evm_version="london")
    delegate, deployment_receipt = delegate_tools.deploy_delegate(
        rpc, chain_id, accounts, deploy_args, include_receipt=True)
    setup_receipts = [deployment_receipt]
    for kind, group, amount in (("normal", normal, config["normalFundEth"]), ("attack", attackers, config["attackFundEth"])):
        for offset in range(0, len(group), config["setupBatchSize"]):
            batch = group[offset:offset + config["setupBatchSize"]]
            records = send(args.helper, rpc, out, f"fund_{kind}_{offset}", batch,
                ["--mode", "fund", "--sponsor-key", funder["private_key"], "--value-wei", str(amount * 10**18),
                 "--gas-price-wei", setup_price, "--gas", "21000", "--wait", "1200s"], len(batch))
            stage_receipts = receipts(rpc, records)
            if any(not r["receipt"] or r["receipt"]["status"] != "0x1" for r in stage_receipts):
                raise RuntimeError("Funding receipts incomplete/failed")
            setup_receipts.extend(dict(r, stage=f"fund_{kind}") for r in stage_receipts)
    for offset in range(0, len(attackers), config["setupBatchSize"]):
        batch = attackers[offset:offset + config["setupBatchSize"]]
        records = send(args.helper, rpc, out, f"authorize_{offset}", batch,
            ["--mode", "setcode", "--sponsor-key", funder["private_key"], "--delegate-address", delegate,
             "--gas-fee-cap-wei", setup_price, "--gas-tip-cap-wei", setup_price, "--gas", "250000", "--wait", "1200s"], len(batch))
        stage_receipts = receipts(rpc, records)
        if any(not r["receipt"] or r["receipt"]["status"] != "0x1" for r in stage_receipts):
            raise RuntimeError("Authorization receipts incomplete/failed")
        setup_receipts.extend(dict(r, stage="authorize") for r in stage_receipts)
    setup_receipts[0]["stage"] = "deploy"
    save(out / "setup_receipts.json", setup_receipts)
    setup_fee_by_stage = {}
    for row in setup_receipts:
        stage = row["stage"]
        setup_fee_by_stage[stage] = setup_fee_by_stage.get(stage, 0) + fee_wei(row["receipt"])
    state = sender_snapshot(rpc, normal + attackers, rpc.height())
    save(out / "setup_accounts.json", state)
    validate_setup(state, config, delegate)
    receiver_address = accounts["x"]["address"]
    receiver_before_wei = int(rpc.call("eth_getBalance", [receiver_address, hex(state[0]["block"])]), 16)
    if any(int(v, 16) for v in rpc.call("txpool_status").values()):
        raise RuntimeError("Setup did not drain the pool")
    wait_block(rpc, rpc.height() + 1)
    normal_records = send(args.helper, rpc, out, "normal", normal,
        ["--mode", "normal", "--value-wei", "1", "--gas", str(config["normalGas"]),
         "--gas-price-wei", str(config["priceUnitWei"] * config["normalPriceMultiplier"]),
         "--calldata-bytes", str(config["normalPaddingBytes"]), "--send-order", config["normalSendOrder"],
         "--txs-per-sender", str(config["txsPerSender"])], len(normal) * config["txsPerSender"])
    normal_timing = json.loads((out / "normal_timing.json").read_text())
    save(out / "normal_block_points.json", dict(
        start=block_point(rpc, normal_timing["before"]["block"], genesis_timestamp, config["secondsPerSlot"]),
        end=block_point(rpc, normal_timing["afterBlock"], genesis_timestamp, config["secondsPerSlot"])))
    save(out / "pool_after_normal.json", dict(block=rpc.height(), status=rpc.call("txpool_status")))
    attack_records = []
    if args.mode == "attack":
        attack_records = send(args.helper, rpc, out, "attack", attackers,
            ["--mode", "attack", "--receiver", receiver_address, "--value-wei", "0", "--gas", str(config["attackGas"]),
             "--gas-price-wei", str(config["priceUnitWei"] * config["attackPriceMultiplier"]),
             "--calldata-bytes", str(config["attackTailPaddingBytes"]), "--first-calldata-bytes", str(config["attackFirstPaddingBytes"]),
             "--send-order", config["attackSendOrder"], "--txs-per-sender", str(config["txsPerSender"])], len(attackers) * config["txsPerSender"])
    else:
        (out / "attack.jsonl").write_text("")
    injection_end = rpc.height()
    injection_point = block_point(rpc, injection_end, genesis_timestamp, config["secondsPerSlot"])
    if attack_records:
        attack_timing = json.loads((out / "attack_timing.json").read_text())
        save(out / "attack_block_points.json", dict(
            start=block_point(rpc, attack_timing["before"]["block"], genesis_timestamp, config["secondsPerSlot"]),
            end=block_point(rpc, attack_timing["afterBlock"], genesis_timestamp, config["secondsPerSlot"])))
    save(out / "pool_after_injection.json", dict(block=injection_end, status=rpc.call("txpool_status"),
         point=injection_point))
    for window in (config["commonWindowBlocks"], config["fullWindowBlocks"]):
        block = wait_block(rpc, injection_end + window, timeout=config["secondsPerSlot"] * (window + 4))
        final_senders = sender_snapshot(rpc, attackers, block)
        save(out / f"senders_window{window}.json", final_senders)
        save(out / f"window{window}_marker.json", dict(targetBlock=injection_end + window,
             observedBlock=block, startPoint=injection_point,
             endPoint=block_point(rpc, block, genesis_timestamp, config["secondsPerSlot"]),
             unixMillis=int(time.time() * 1000)))
    all_receipts = receipts(rpc, normal_records + attack_records)
    save(out / "receipts.json", all_receipts)
    # Check receipt block hashes against canonical headers; do not trust stale receipt caches.
    receipt_blocks = {r["receipt"]["blockNumber"] for r in all_receipts if r["receipt"]}
    canonical = {tag: rpc.call("eth_getBlockByNumber", [tag, False])["hash"] for tag in receipt_blocks}
    if any(r["receipt"] and canonical[r["receipt"]["blockNumber"]] != r["receipt"]["blockHash"] for r in all_receipts):
        raise RuntimeError("Receipt block hash is no longer canonical; re-audit required")
    sender_balances = first_attack_balances(rpc, attack_records, all_receipts, state, final_senders)
    save(out / "first_attack_balances.json", sender_balances)
    receiver_after_wei = int(rpc.call("eth_getBalance", [receiver_address, hex(block)]), 16)
    save(out / "receiver_balance.json", dict(address=receiver_address,
         beforeBlock=state[0]["block"], beforeWei=str(receiver_before_wei),
         afterBlock=block, afterWei=str(receiver_after_wei)))
    if args.delegate_case == "alice-no-drain" and receiver_before_wei != receiver_after_wei:
        raise RuntimeError("No-drain receiver balance changed; preserve evidence and investigate")
    included_attack_blocks = [int(r["receipt"]["blockNumber"], 16) for r in all_receipts
                              if r["kind"] == "attack" and r["receipt"]]
    first_attack_point = (block_point(rpc, min(included_attack_blocks), genesis_timestamp, config["secondsPerSlot"])
                          if included_attack_blocks else None)
    summary = dict(group=args.group, mode=args.mode, delegateCase=args.delegate_case,
        injectionEndBlock=injection_end, firstAttackIncludedPoint=first_attack_point, setupFeeWei=str(sum(setup_fee_by_stage.values())),
        setupFeeByStageWei={k: str(v) for k, v in setup_fee_by_stage.items()},
        receiverBalanceDeltaWei=str(receiver_after_wei - receiver_before_wei), windows={})
    for window in (config["commonWindowBlocks"], config["fullWindowBlocks"]):
        groups = {}
        for kind, records in (("normal", normal_records), ("attack", attack_records)):
            included = [r["receipt"] for r in all_receipts if r["kind"] == kind and r["receipt"] and int(r["receipt"]["blockNumber"], 16) <= injection_end + window]
            groups[kind] = dict(submitted=len(records), accepted=len(records), included=len(included),
                successful=sum(r["status"] == "0x1" for r in included),
                failed=sum(r["status"] != "0x1" for r in included),
                feeWei=str(sum(fee_wei(r) for r in included)))
        summary["windows"][str(window)] = groups
    save(out / "summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("baseline", "attack"), required=True)
    p.add_argument("--config", type=Path, default=HERE / "config.json")
    p.add_argument("--group", choices=("G0", "G1", "G2"), default=None)
    p.add_argument("--delegate-case", choices=("alice-sender", "alice-no-drain"), default="alice-sender")
    p.add_argument("--rpc", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--helper", type=Path, required=True)
    p.add_argument("--key-csv", type=Path, required=True)
    p.add_argument("--seed", default="seed01")
    run(p.parse_args())
