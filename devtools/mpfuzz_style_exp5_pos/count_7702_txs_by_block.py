#!/usr/bin/env python3
"""Count EIP-7702 transactions and sender concentration by block.

The tool scans an execution-layer JSON-RPC endpoint with eth_getBlockByNumber
and writes per-block, per-sender, and per-transaction CSV files. It identifies
EIP-7702 set-code transactions by transaction type 0x04, with authorizationList
as a compatibility fallback.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan blocks and count EIP-7702 transactions grouped by transaction sender."
    )
    parser.add_argument(
        "--rpc",
        default=None,
        help="Execution-layer JSON-RPC URL. Defaults to RPC, ETH_RPC_URL, MAINNET_RPC, or ALCHEMY_API_KEY.",
    )
    parser.add_argument("--start-block", type=int, default=None, help="First block number to scan.")
    parser.add_argument("--end-block", type=int, default=None, help="Last block number to scan. Defaults to latest.")
    parser.add_argument(
        "--blocks",
        type=int,
        default=32,
        help="Number of latest blocks to scan when --start-block is omitted. Default: 32.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory. Defaults to scale_runs/rpc_7702_block_scan_<range>_<timestamp>.",
    )
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=3, help="RPC retry count per request.")
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Optional sleep in seconds between block RPC requests.",
    )
    parser.add_argument(
        "--include-rpc-url",
        action="store_true",
        help="Store the full RPC URL in summary.json. Off by default to avoid leaking API keys.",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=100,
        help="Write partial CSV outputs every N scanned blocks. Use 0 to disable. Default: 100.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Record failed blocks and keep scanning after all retries are exhausted.",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_default_env_files() -> None:
    script_dir = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / ".env",
        script_dir / ".env",
        script_dir.parent / ".env",
        script_dir.parent.parent / ".env",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        load_env_file(resolved)


def resolve_rpc_url(cli_rpc: str | None) -> str:
    if cli_rpc:
        return cli_rpc
    for key in ("RPC", "ETH_RPC_URL", "MAINNET_RPC"):
        value = os.environ.get(key)
        if value:
            return value
    alchemy_key = os.environ.get("ALCHEMY_API_KEY")
    if alchemy_key:
        return f"https://eth-mainnet.g.alchemy.com/v2/{alchemy_key}"
    raise SystemExit("missing RPC endpoint: pass --rpc, set RPC/ETH_RPC_URL/MAINNET_RPC, or set ALCHEMY_API_KEY")


def rpc(url: str, method: str, params: list[Any], timeout: int, retries: int) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            decoded = json.loads(body)
            if "error" in decoded:
                raise RuntimeError(f"{method} error: {decoded['error']}")
            return decoded["result"]
        except (
            urllib.error.URLError,
            TimeoutError,
            RuntimeError,
            json.JSONDecodeError,
            http.client.HTTPException,
            ConnectionError,
            OSError,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 8))

    raise RuntimeError(f"{method} failed after {retries} attempt(s): {last_error}")


def hex_to_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    raise TypeError(f"cannot parse integer from {value!r}")


def normalize_address(value: Any) -> str:
    if not value:
        return ""
    return str(value).lower()


def normalize_tx_type(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return hex(value)
    raw = str(value).lower()
    if raw.startswith("0x"):
        try:
            return hex(int(raw, 16))
        except ValueError:
            return raw
    return raw


def is_eip7702_tx(tx: dict[str, Any]) -> bool:
    tx_type = normalize_tx_type(tx.get("type"))
    if tx_type == "0x4":
        return True
    auth_list = tx.get("authorizationList")
    if auth_list is None:
        auth_list = tx.get("authorization_list")
    return isinstance(auth_list, list) and len(auth_list) > 0


def format_hash_list(values: list[str]) -> str:
    return ";".join(v for v in values if v)


def sanitize_rpc_url(url: str, include_full: bool) -> str:
    if include_full:
        return url
    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "/...", "", ""))


def default_out_dir(start_block: int, end_block: int) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return (
        Path(__file__).resolve().parent
        / "scale_runs"
        / f"rpc_7702_block_scan_{start_block}_{end_block}_{stamp}"
    )


def resolve_block_range(args: argparse.Namespace, rpc_url: str) -> tuple[int, int, int]:
    latest = hex_to_int(rpc(rpc_url, "eth_blockNumber", [], args.timeout, args.retries))

    end_block = args.end_block if args.end_block is not None else latest
    if end_block > latest:
        raise SystemExit(f"--end-block {end_block} is greater than latest block {latest}")

    if args.start_block is not None:
        start_block = args.start_block
    else:
        if args.blocks <= 0:
            raise SystemExit("--blocks must be positive when --start-block is omitted")
        start_block = max(0, end_block - args.blocks + 1)

    if start_block > end_block:
        raise SystemExit(f"invalid block range: start {start_block} > end {end_block}")
    return latest, start_block, end_block


BLOCK_FIELDNAMES = [
    "blockNumber",
    "blockHash",
    "timestamp",
    "totalTxs",
    "eip7702Txs",
    "unique7702Senders",
    "max7702TxsPerSender",
    "sendersWithMultiple7702Txs",
    "txsFromRepeated7702Senders",
    "top7702Sender",
    "top7702SenderTxs",
    "error",
]

SENDER_FIELDNAMES = ["blockNumber", "sender", "eip7702TxsFromSender", "nonces", "txHashes"]

TX_FIELDNAMES = [
    "blockNumber",
    "txIndexInScan7702Only",
    "transactionIndex",
    "hash",
    "from",
    "to",
    "type",
    "nonce",
    "gas",
    "gasPriceWei",
    "maxFeePerGasWei",
    "maxPriorityFeePerGasWei",
    "authorizationListLen",
]


def scan_blocks(
    rpc_url: str,
    start_block: int,
    end_block: int,
    timeout: int,
    retries: int,
    sleep_seconds: float,
    continue_on_error: bool,
    checkpoint_every: int,
    checkpoint_paths: tuple[Path, Path, Path] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    block_rows: list[dict[str, Any]] = []
    sender_rows: list[dict[str, Any]] = []
    tx_rows: list[dict[str, Any]] = []

    for block_number in range(start_block, end_block + 1):
        try:
            block = rpc(rpc_url, "eth_getBlockByNumber", [hex(block_number), True], timeout, retries)
            if block is None:
                raise RuntimeError(f"block {block_number} not found")

            transactions = block.get("transactions") or []
            eip7702_txs = [tx for tx in transactions if isinstance(tx, dict) and is_eip7702_tx(tx)]
            sender_counts = Counter(normalize_address(tx.get("from")) for tx in eip7702_txs)
            sender_counts.pop("", None)

            by_sender: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for tx in eip7702_txs:
                by_sender[normalize_address(tx.get("from"))].append(tx)

            top_sender = ""
            top_sender_count = 0
            if sender_counts:
                top_sender, top_sender_count = sender_counts.most_common(1)[0]

            repeated_sender_count = sum(1 for count in sender_counts.values() if count > 1)
            repeated_tx_count = sum(count for count in sender_counts.values() if count > 1)

            block_row = {
                "blockNumber": block_number,
                "blockHash": block.get("hash") or "",
                "timestamp": hex_to_int(block.get("timestamp")),
                "totalTxs": len(transactions),
                "eip7702Txs": len(eip7702_txs),
                "unique7702Senders": len(sender_counts),
                "max7702TxsPerSender": top_sender_count,
                "sendersWithMultiple7702Txs": repeated_sender_count,
                "txsFromRepeated7702Senders": repeated_tx_count,
                "top7702Sender": top_sender,
                "top7702SenderTxs": top_sender_count,
                "error": "",
            }
            block_rows.append(block_row)

            for sender, sender_txs in sorted(by_sender.items()):
                if not sender:
                    continue
                sender_rows.append(
                    {
                        "blockNumber": block_number,
                        "sender": sender,
                        "eip7702TxsFromSender": len(sender_txs),
                        "nonces": ";".join(str(hex_to_int(tx.get("nonce"))) for tx in sender_txs),
                        "txHashes": format_hash_list([str(tx.get("hash") or "") for tx in sender_txs]),
                    }
                )

            for tx_index, tx in enumerate(eip7702_txs):
                auth_list = tx.get("authorizationList")
                if auth_list is None:
                    auth_list = tx.get("authorization_list") or []
                tx_rows.append(
                    {
                        "blockNumber": block_number,
                        "txIndexInScan7702Only": tx_index,
                        "transactionIndex": hex_to_int(tx.get("transactionIndex")),
                        "hash": tx.get("hash") or "",
                        "from": normalize_address(tx.get("from")),
                        "to": normalize_address(tx.get("to")),
                        "type": normalize_tx_type(tx.get("type")),
                        "nonce": hex_to_int(tx.get("nonce")),
                        "gas": hex_to_int(tx.get("gas")),
                        "gasPriceWei": hex_to_int(tx.get("gasPrice")) if tx.get("gasPrice") is not None else "",
                        "maxFeePerGasWei": hex_to_int(tx.get("maxFeePerGas"))
                        if tx.get("maxFeePerGas") is not None
                        else "",
                        "maxPriorityFeePerGasWei": hex_to_int(tx.get("maxPriorityFeePerGas"))
                        if tx.get("maxPriorityFeePerGas") is not None
                        else "",
                        "authorizationListLen": len(auth_list) if isinstance(auth_list, list) else "",
                    }
                )

            print(
                "block={blockNumber} totalTxs={totalTxs} eip7702Txs={eip7702Txs} "
                "uniqueSenders={unique7702Senders} maxPerSender={max7702TxsPerSender}".format(**block_row)
            )
        except Exception as exc:
            if not continue_on_error:
                raise
            block_row = {
                "blockNumber": block_number,
                "blockHash": "",
                "timestamp": "",
                "totalTxs": "",
                "eip7702Txs": "",
                "unique7702Senders": "",
                "max7702TxsPerSender": "",
                "sendersWithMultiple7702Txs": "",
                "txsFromRepeated7702Senders": "",
                "top7702Sender": "",
                "top7702SenderTxs": "",
                "error": f"{type(exc).__name__}: {exc}",
            }
            block_rows.append(block_row)
            print(f"block={block_number} error={block_row['error']}")

        processed_blocks = block_number - start_block + 1
        if checkpoint_every > 0 and checkpoint_paths and processed_blocks % checkpoint_every == 0:
            write_scan_csvs(checkpoint_paths, block_rows, sender_rows, tx_rows)
            print(f"checkpoint blocksScanned={processed_blocks} blockSummary={checkpoint_paths[0]}")

        if sleep_seconds:
            time.sleep(sleep_seconds)

    return block_rows, sender_rows, tx_rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_scan_csvs(
    paths: tuple[Path, Path, Path],
    block_rows: list[dict[str, Any]],
    sender_rows: list[dict[str, Any]],
    tx_rows: list[dict[str, Any]],
) -> None:
    block_csv, sender_csv, tx_csv = paths
    write_csv(block_csv, block_rows, BLOCK_FIELDNAMES)
    write_csv(sender_csv, sender_rows, SENDER_FIELDNAMES)
    write_csv(tx_csv, tx_rows, TX_FIELDNAMES)


def main() -> int:
    load_default_env_files()
    args = parse_args()
    rpc_url = resolve_rpc_url(args.rpc)
    latest, start_block, end_block = resolve_block_range(args, rpc_url)
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir(start_block, end_block)
    out_dir.mkdir(parents=True, exist_ok=True)
    block_csv = out_dir / "block_7702_summary.csv"
    sender_csv = out_dir / "block_7702_senders.csv"
    tx_csv = out_dir / "eip7702_txs.csv"
    summary_json = out_dir / "summary.json"

    print(f"RPC: {sanitize_rpc_url(rpc_url, args.include_rpc_url)}")
    print(f"latestBlock={latest}")
    print(f"scanRange={start_block}..{end_block}")
    print(f"outDir={out_dir}")

    block_rows, sender_rows, tx_rows = scan_blocks(
        rpc_url=rpc_url,
        start_block=start_block,
        end_block=end_block,
        timeout=args.timeout,
        retries=args.retries,
        sleep_seconds=args.sleep,
        continue_on_error=args.continue_on_error,
        checkpoint_every=args.checkpoint_every,
        checkpoint_paths=(block_csv, sender_csv, tx_csv),
    )

    write_scan_csvs((block_csv, sender_csv, tx_csv), block_rows, sender_rows, tx_rows)

    successful_rows = [row for row in block_rows if not row.get("error")]
    failed_rows = [row for row in block_rows if row.get("error")]
    blocks_with_7702 = [row for row in successful_rows if row["eip7702Txs"]]
    max_row = max(successful_rows, key=lambda row: row["max7702TxsPerSender"], default=None)
    summary = {
        "rpc": sanitize_rpc_url(rpc_url, args.include_rpc_url),
        "latestBlockAtScanStart": latest,
        "startBlock": start_block,
        "endBlock": end_block,
        "blocksScanned": len(block_rows),
        "blocksSucceeded": len(successful_rows),
        "blocksFailed": len(failed_rows),
        "failedBlocks": [row["blockNumber"] for row in failed_rows],
        "blocksWith7702": len(blocks_with_7702),
        "total7702Txs": sum(row["eip7702Txs"] for row in successful_rows),
        "blocksWithRepeated7702Sender": sum(
            1 for row in successful_rows if row["sendersWithMultiple7702Txs"]
        ),
        "max7702TxsPerSenderInOneBlock": max_row["max7702TxsPerSender"] if max_row else 0,
        "max7702SenderBlock": max_row["blockNumber"] if max_row else None,
        "max7702Sender": max_row["top7702Sender"] if max_row else "",
        "continueOnError": args.continue_on_error,
        "checkpointEvery": args.checkpoint_every,
        "outputs": {
            "blockSummaryCsv": str(block_csv),
            "senderCsv": str(sender_csv),
            "txCsv": str(tx_csv),
        },
        "notes": [
            "EIP-7702 detection uses tx.type == 0x4, with authorizationList as fallback.",
            "Sender grouping uses the transaction 'from' address returned by JSON-RPC.",
            "Authorization-list signer recovery is not performed by this script.",
        ],
    }
    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"blockSummary={block_csv}")
    print(f"senderSummary={sender_csv}")
    print(f"txDetails={tx_csv}")
    print(f"summary={summary_json}")
    print(
        "total7702Txs={total7702Txs} blocksWith7702={blocksWith7702} "
        "blocksWithRepeated7702Sender={blocksWithRepeated7702Sender} "
        "max7702TxsPerSenderInOneBlock={max7702TxsPerSenderInOneBlock} "
        "blocksFailed={blocksFailed}".format(**summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
