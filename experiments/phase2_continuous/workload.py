"""Run one Phase 2 group on a local Kurtosis execution RPC."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from eth_account import Account

HERE = Path(__file__).resolve().parent
BESU = HERE.parent / "besu26_layertrace"
LEGACY = HERE.parents[1] / "devtools" / "mpfuzz_style_exp5_pos"
sys.path.insert(0, str(BESU))
sys.path.insert(0, str(LEGACY))
import workload as prior
import mp_exp5_pos as base
import mp_exp5_7702_pos as delegate_tools
import mp_exp5_7702_scale_pos as scale
from funder import load_funder

from metrics import collect
from plan import schedule, validate_plan
from normal_fee import parameters as normal_fee_parameters


def save(path: Path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def assert_private_rpc(url: str):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Phase 2 accepts only a local isolated Kurtosis HTTP RPC")


def sleep_until(target: float, stop: threading.Event):
    while not stop.is_set():
        remaining = target - time.monotonic()
        if remaining <= 0:
            return True
        stop.wait(min(remaining, 1))
    return False


def raw_send(url: str, raw: bytes, expected_hash: str) -> tuple[str | None, str | None, str | None]:
    body = json.dumps(dict(jsonrpc="2.0", id=1, method="eth_sendRawTransaction",
                           params=["0x" + raw.hex()])).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            data = json.load(error)
        except (ValueError, OSError):
            return None, "ambiguous", f"HTTP {error.code} without parseable JSON-RPC response"
    except (OSError, ValueError) as error:
        # A disconnected response is ambiguous: the node may have accepted the tx.
        return None, "ambiguous", f"{type(error).__name__}: {error}"
    if "error" in data:
        return None, "rejected", str(data["error"])
    result = data.get("result")
    if not isinstance(result, str) or result.lower() != expected_hash.lower():
        return None, "ambiguous", f"Unexpected RPC result: {result!r}"
    return result, None, None


def send_normal(rpc_url: str, chain_id: int, accounts: list[dict], rows: list[dict],
                config: dict, anchor_mono: float, anchor_unix_ms: int,
                output: Path, stop: threading.Event, error_box: list[str]):
    data = bytes(config["normalPaddingBytes"])
    with output.open("w", encoding="utf-8", buffering=1) as handle:
        try:
            for row in rows:
                if not sleep_until(anchor_mono + row["offsetMillis"] / 1000, stop):
                    return
                account = accounts[row["senderIndex"]]
                sign_started = int(time.time() * 1000)
                # Legacy type-0; gas limit and fee plan match the paired groups.
                tx = dict(nonce=row["nonce"], gasPrice=row["gasPriceWei"],
                          gas=config["normalGas"], to=account["address"], value=1,
                          data=data, chainId=chain_id)
                signed = Account.sign_transaction(tx, account["private_key"])
                expected_hash = "0x" + signed.hash.hex().removeprefix("0x")
                attempted = int(time.time() * 1000)
                result, kind, error = raw_send(rpc_url, signed.raw_transaction, expected_hash)
                completed = int(time.time() * 1000)
                record = dict(kind="normal", label=row["label"], sender=account["address"],
                              nonce=row["nonce"], gasPriceWei=str(row["gasPriceWei"]),
                              plannedOffsetMillis=row["offsetMillis"],
                              plannedUnixMillis=anchor_unix_ms + row["offsetMillis"],
                              signStartedUnixMillis=sign_started,
                              attemptUnixMillis=attempted, completedUnixMillis=completed,
                              actualUnixMillis=attempted, hash=result,
                              localSignedHash=expected_hash, errorKind=kind, error=error)
                handle.write(json.dumps(record) + "\n")
        except Exception as error:
            error_box.append(f"{type(error).__name__}: {error}")


def watch_pool(rpc: prior.Rpc, output: Path, stop: threading.Event):
    last = -1
    with output.open("w", encoding="utf-8", buffering=1) as handle:
        while not stop.is_set():
            try:
                height = rpc.height()
                if height != last:
                    for number in range(last + 1 if last >= 0 else height, height + 1):
                        row = dict(blockNumber=number, unixMillis=int(time.time() * 1000))
                        try:
                            status = rpc.call("txpool_status")
                            row.update(pending=int(status["pending"], 16),
                                       queued=int(status["queued"], 16), error="")
                        except (KeyError, ValueError, OSError, RuntimeError) as error:
                            row.update(pending="", queued="", error=str(error))
                        handle.write(json.dumps(row) + "\n")
                    last = height
            except (OSError, RuntimeError, ValueError) as error:
                handle.write(json.dumps(dict(blockNumber=None, unixMillis=int(time.time() * 1000),
                                             error=str(error))) + "\n")
            stop.wait(1)


def load_accounts(key_csv, funder):
    # load_key_csv inserts DEFAULT_FUNDER into the returned account map. Set the
    # locally generated sponsor first, as Phase 1 does, before loading the CSV.
    base.DEFAULT_FUNDER.update(funder)
    accounts = base.load_key_csv(key_csv)
    if accounts[base.DEFAULT_FUNDER["label"]]["address"].lower() != funder["address"].lower():
        raise RuntimeError("Local devnet funder address mismatch")
    return accounts


def setup(args, config, rpc, normal, attackers, funder, out, planned):
    accounts = load_accounts(args.key_csv, funder)
    genesis_balance = int(rpc.call("eth_getBalance", [funder["address"], "0x0"]), 16)
    if genesis_balance != config["funderGenesisBalanceEth"] * 10**18:
        raise RuntimeError("Funder genesis balance differs from local plan")
    required_funds = (config["normalSenders"] * config["normalFundEth"] +
                      config["attackSenders"] * config["attackFundEth"]) * 10**18
    if int(rpc.call("eth_getBalance", [funder["address"], "latest"]), 16) < required_funds + 10**18:
        raise RuntimeError("Insufficient local devnet sponsor balance")
    prior.wait_block(rpc, rpc.height() + 1, timeout=config["secondsPerSlot"] * 3)
    deploy_args = SimpleNamespace(case=args.delegate_case, rpc_url=args.rpc, deploy_gas=2000000,
                                  setup_price=config["setupGasPriceWei"], price_unit=1,
                                  receipt_timeout=1200, solc_version="0.8.20", evm_version="london")
    chain_id = int(rpc.call("eth_chainId"), 16)
    delegate, deployment_receipt = delegate_tools.deploy_delegate(
        rpc, chain_id, accounts, deploy_args, include_receipt=True)
    setup_receipts = [dict(stage="deploy", receipt=deployment_receipt)]
    setup_price = str(config["setupGasPriceWei"])
    for kind, group, amount in (("normal", normal, config["normalFundEth"]),
                                ("attack", attackers, config["attackFundEth"])):
        for offset in range(0, len(group), config["setupBatchSize"]):
            batch = group[offset:offset + config["setupBatchSize"]]
            records = prior.send(args.helper, rpc, out, f"fund_{kind}_{offset}", batch,
                ["--mode", "fund", "--sponsor-key", funder["private_key"], "--value-wei",
                 str(amount * 10**18), "--gas-price-wei", setup_price,
                 "--gas", "21000", "--wait", "1200s"], len(batch))
            result = prior.receipts(rpc, records)
            if any(not r["receipt"] or r["receipt"]["status"] != "0x1" for r in result):
                raise RuntimeError("Funding receipt incomplete or failed")
            setup_receipts.extend(dict(r, stage=f"fund_{kind}") for r in result)
    for offset in range(0, len(attackers), config["setupBatchSize"]):
        batch = attackers[offset:offset + config["setupBatchSize"]]
        records = prior.send(args.helper, rpc, out, f"authorize_{offset}", batch,
            ["--mode", "setcode", "--sponsor-key", funder["private_key"],
             "--delegate-address", delegate, "--gas-fee-cap-wei", setup_price,
             "--gas-tip-cap-wei", setup_price, "--gas", "250000", "--wait", "1200s"], len(batch))
        result = prior.receipts(rpc, records)
        if any(not r["receipt"] or r["receipt"]["status"] != "0x1" for r in result):
            raise RuntimeError("Authorization receipt incomplete or failed")
        setup_receipts.extend(dict(r, stage="authorize") for r in result)
    save(out / "setup_receipts.json", setup_receipts)
    state = prior.sender_snapshot(rpc, normal + attackers, rpc.height())
    save(out / "setup_accounts.json", state)
    normal_counts = {}
    for planned_row in planned:
        label = normal[planned_row["senderIndex"]]["label"]
        normal_counts[label] = normal_counts.get(label, 0) + 1
    max_price = normal_fee_parameters(config)["maximumWei"]
    for account in state:
        if account["label"].startswith("n"):
            needed = normal_counts.get(account["label"], 0) * (config["normalGas"] * max_price + 1)
            if int(account["balanceWei"]) < needed:
                raise RuntimeError(f"{account['label']}: actual funded balance below normal plan bound")
        if account["label"].startswith("a"):
            attack_bound = config["txsPerSender"] * config["attackGas"] * (
                config["priceUnitWei"] * config["attackPriceMultiplier"])
            if int(account["balanceWei"]) < attack_bound:
                raise RuntimeError(f"Attack account {account['label']} below cumulative upfront balance bound")
            wanted = "0xef0100" + delegate.lower().removeprefix("0x")
            if account["nonce"] != 1 or account["code"].lower() != wanted:
                raise RuntimeError(f"Attack account {account['label']} not freshly delegated")
        elif account["nonce"] != 0 or account["code"] != "0x":
            raise RuntimeError(f"Normal account {account['label']} not fresh")
    status = rpc.call("txpool_status")
    if any(int(v, 16) for v in status.values()):
        raise RuntimeError("Setup has not drained txpool")
    return accounts["x"]["address"], state


def run(args):
    os.umask(0o077)
    assert_private_rpc(args.rpc)
    config = json.loads(args.config.read_text())
    if args.client == "besu" and config["besuVersion"] != "26.6.1":
        raise RuntimeError("Wrong Besu version")
    if args.client == "erigon" and config["erigonVersion"] != "3.5.0":
        raise RuntimeError("Wrong Erigon version")
    if config["attackSenders"] != 125 or config["txsPerSender"] != 128:
        raise RuntimeError("Phase 2 preserves the 125 x 128 attack workload")
    if config["attackFirstPaddingBytes"] != 0:
        raise RuntimeError("Phase 2 first attack tx must have no calldata padding")
    if config["normalGasPriceMode"] not in ("fixed", "randomized-uniform"):
        raise RuntimeError("Unknown normal fee mode")
    group = config["evaluationGroups"][args.group]
    if (args.mode, args.delegate_case) != (group["mode"], group["delegateCase"]):
        raise RuntimeError("Group, attack mode or delegate mismatch")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "private").mkdir(mode=0o700, exist_ok=True)
    rpc = prior.Rpc(args.rpc)
    version = rpc.call("web3_clientVersion")
    prefix = "besu/v26.6.1/" if args.client == "besu" else "erigon/3.5.0/"
    if not version.startswith(prefix):
        raise RuntimeError(f"Expected {prefix}, received {version}")
    chain_id = int(rpc.call("eth_chainId"), 16)
    if chain_id != config.get("chainId", 13371349):
        raise RuntimeError("Wrong private devnet chain ID")
    genesis = rpc.call("eth_getBlockByNumber", ["0x0", False])
    genesis_timestamp = int(genesis["timestamp"], 16)
    normal = scale.derive_accounts("n", config["normalSenders"], args.seed + ":" + args.client)
    attackers = scale.derive_accounts("a", config["attackSenders"], args.seed + ":" + args.client)
    planned = schedule(config, args.seed, normal, args.client)
    original = args.schedule.read_bytes()
    local = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in planned).encode()
    if hashlib.sha256(original).digest() != hashlib.sha256(local).digest():
        raise RuntimeError("Group schedule differs from the root matched normal plan")
    shutil.copyfile(args.schedule, out / "normal_schedule.jsonl")
    budget = validate_plan(config, planned, normal, int(genesis["gasLimit"], 16))
    save(out / "normal_budget.json", budget)
    funder = load_funder(args.funder_file)
    save(out / "workload_manifest.json", dict(group=args.group, mode=args.mode,
         client=args.client, clientVersion=version, chainId=chain_id, genesis=genesis,
         scheduleSha256=hashlib.sha256(local).hexdigest(), config=config,
         accounts=[dict(label=a["label"], address=a["address"]) for a in normal + attackers]))
    receiver, _setup_state = setup(args, config, rpc, normal, attackers, funder, out, planned)
    receiver_before = int(rpc.call("eth_getBalance", [receiver, "latest"]), 16)
    anchor_block = prior.wait_block(rpc, rpc.height() + 1, timeout=config["secondsPerSlot"] * 3)
    header = rpc.call("eth_getBlockByNumber", [hex(anchor_block), False])
    delta = int(header["timestamp"], 16) - genesis_timestamp
    if delta < 0 or delta % config["secondsPerSlot"]:
        raise RuntimeError("Anchor block cannot be mapped to a PoS slot")
    anchor_slot = delta // config["secondsPerSlot"]
    anchor_mono = time.monotonic()
    anchor_unix_ms = int(time.time() * 1000)
    anchor_lag_ms = anchor_unix_ms - int(header["timestamp"], 16) * 1000
    if not 0 <= anchor_lag_ms < 60_000:
        raise RuntimeError(f"Anchor header is stale or host clock differs: lag={anchor_lag_ms} ms")
    phase = config["phase2"]
    total_slots = phase["preSlots"] + phase["attackSlots"] + phase["postSlots"]
    duration = total_slots * config["secondsPerSlot"]
    save(out / "anchor.json", dict(blockNumber=anchor_block, blockHash=header["hash"],
         slotNumber=anchor_slot, blockTimestamp=int(header["timestamp"], 16),
         unixMillis=anchor_unix_ms, anchorLagMillis=anchor_lag_ms,
         scheduleSha256=hashlib.sha256(local).hexdigest()))
    stop = threading.Event()
    errors = []
    sender = threading.Thread(target=send_normal,
        args=(args.rpc, chain_id, normal, planned, config, anchor_mono, anchor_unix_ms,
              out / "normal.jsonl", stop, errors), daemon=True)
    observer = threading.Thread(target=watch_pool,
        args=(rpc, out / "pool_snapshots.jsonl", stop), daemon=True)
    sender.start()
    observer.start()
    markers = dict(anchorUnixMillis=anchor_unix_ms, anchorBlock=anchor_block, anchorSlot=anchor_slot,
                   plannedInjectionOffsetSlots=phase["preSlots"], group=args.group)
    try:
        inject_at = anchor_mono + phase["preSlots"] * config["secondsPerSlot"]
        if not sleep_until(inject_at, stop):
            raise RuntimeError("Interrupted before attack phase")
        markers["injectionWindowStartUnixMillis"] = int(time.time() * 1000)
        markers["injectionWindowStartBlock"] = rpc.height()
        if args.mode == "attack":
            prior.send(args.helper, rpc, out, "attack", attackers,
                ["--mode", "attack", "--receiver", receiver, "--value-wei", "0",
                 "--gas", str(config["attackGas"]), "--gas-price-wei",
                 str(config["priceUnitWei"] * config["attackPriceMultiplier"]),
                 "--calldata-bytes", str(config["attackTailPaddingBytes"]),
                 "--first-calldata-bytes", str(config["attackFirstPaddingBytes"]),
                 "--send-order", config["attackSendOrder"], "--txs-per-sender",
                 str(config["txsPerSender"])],
                len(attackers) * config["txsPerSender"],
                allow_rpc_errors=(args.client == "erigon"))
        else:
            (out / "attack.jsonl").write_text("", encoding="utf-8")
        markers["injectionEndUnixMillis"] = int(time.time() * 1000)
        markers["injectionEndBlock"] = rpc.height()
        markers["attackWasBroadcast"] = args.mode == "attack"
        save(out / "phase_markers.json", markers)
        if not sleep_until(anchor_mono + duration, stop):
            raise RuntimeError("Interrupted during observation")
        end_block = rpc.height()
        horizon_slot = anchor_slot + total_slots
        while end_block >= anchor_block:
            candidate = rpc.call("eth_getBlockByNumber", [hex(end_block), False])
            candidate_slot = (int(candidate["timestamp"], 16) - genesis_timestamp) // config["secondsPerSlot"]
            if candidate_slot < horizon_slot:
                break
            end_block -= 1
        if end_block < anchor_block:
            raise RuntimeError("No canonical block inside the observation horizon")
        markers["observationEndUnixMillis"] = int(time.time() * 1000)
        markers["observationEndBlock"] = end_block
        markers["attackBroadcastOverranObservation"] = (
            args.mode == "attack" and markers["injectionEndUnixMillis"] >
            anchor_unix_ms + duration * 1000)
        save(out / "phase_markers.json", markers)
        sender.join(timeout=60)
        if sender.is_alive():
            raise RuntimeError("Normal sender did not complete after observation horizon")
        if errors:
            raise RuntimeError(f"Normal sender failed: {errors[0]}")
        attempt_count = len((out / "normal.jsonl").read_text().splitlines())
        if attempt_count != len(planned):
            raise RuntimeError(f"Normal attempts {attempt_count} differ from schedule {len(planned)}")
    finally:
        stop.set()
        sender.join(timeout=35)
        observer.join(timeout=35)
    receiver_after = int(rpc.call("eth_getBalance", [receiver, hex(end_block)]), 16)
    save(out / "receiver_balance.json", dict(address=receiver,
         beforeWei=str(receiver_before), afterWei=str(receiver_after),
         observedThroughBlock=end_block))
    if args.delegate_case == "alice-no-drain" and receiver_after != receiver_before:
        raise RuntimeError("No-drain receiver balance changed")
    summary = collect(rpc, out, config, args.client, args.group, genesis_timestamp,
                      anchor_block, anchor_slot, anchor_unix_ms, end_block)
    setup_by_stage = {}
    for item in json.loads((out / "setup_receipts.json").read_text()):
        stage = item["stage"]
        setup_by_stage[stage] = setup_by_stage.get(stage, 0) + prior.fee_wei(item["receipt"])
    summary.update(receiverBalanceDeltaWei=str(receiver_after - receiver_before),
                   setupFeeWei=str(sum(setup_by_stage.values())),
                   setupFeeByStageWei={k: str(v) for k, v in setup_by_stage.items()},
                   attackFeeScope="post-setup attack transaction receipts within observation horizon")
    save(out / "phase2_summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("besu", "erigon"), required=True)
    parser.add_argument("--group", choices=("G0", "G1", "G2"), required=True)
    parser.add_argument("--mode", choices=("baseline", "attack"), required=True)
    parser.add_argument("--delegate-case", choices=("alice-sender", "alice-no-drain"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--key-csv", type=Path, required=True)
    parser.add_argument("--funder-file", type=Path, required=True)
    parser.add_argument("--seed", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
