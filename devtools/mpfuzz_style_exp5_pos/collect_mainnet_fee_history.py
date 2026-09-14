#!/usr/bin/env python3
"""Collect recent Ethereum mainnet fee history via eth_feeHistory.

This script is intended to calibrate local Deter-Z2 experiment gas prices
against recent mainnet-like fee levels. It uses only Python's standard library.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


GWEI = 10**9
DEFAULT_PERCENTILES = [10, 25, 50, 75, 90]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect mainnet gas fee samples using eth_feeHistory.")
    parser.add_argument(
        "--rpc",
        default=None,
        help="Ethereum mainnet JSON-RPC URL. Defaults to MAINNET_RPC, or Alchemy mainnet when ALCHEMY_API_KEY is set.",
    )
    parser.add_argument("--blocks", type=int, default=1024, help="Number of recent blocks to sample.")
    parser.add_argument(
        "--end-block",
        default="latest",
        help="Newest block to sample. Accepts latest, safe, finalized, decimal, or hex block number.",
    )
    parser.add_argument(
        "--percentiles",
        default="10,25,50,75,90",
        help="Comma-separated effective priority fee percentiles requested from eth_feeHistory.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=256,
        help="Number of blocks per eth_feeHistory call. Lower this if your RPC provider rejects large ranges.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory. Defaults to fee_data/mainnet_latest_<blocks>_<timestamp> next to this script.",
    )
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=3, help="RPC retry count per request.")
    parser.add_argument(
        "--reference-gas-used",
        default="21000,102920",
        help=(
            "Comma-separated gasUsed values used only for summary cost estimates. "
            "Defaults to a simple ETH transfer and the current 8192-byte normal transaction."
        ),
    )
    parser.add_argument(
        "--include-rpc-url",
        action="store_true",
        help="Store the full RPC URL in summary.json. Off by default to avoid leaking API keys.",
    )
    return parser.parse_args()


def resolve_rpc_url(cli_rpc: str | None) -> str:
    if cli_rpc:
        return cli_rpc
    env_rpc = os.environ.get("MAINNET_RPC")
    if env_rpc:
        return env_rpc
    alchemy_key = os.environ.get("ALCHEMY_API_KEY")
    if alchemy_key:
        return f"https://eth-mainnet.g.alchemy.com/v2/{alchemy_key}"
    raise SystemExit("missing RPC endpoint: pass --rpc, set MAINNET_RPC, or set ALCHEMY_API_KEY")


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
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        load_env_file(resolved)


def parse_percentiles(raw: str) -> list[int]:
    if not raw.strip():
        return DEFAULT_PERCENTILES
    values = []
    for item in raw.split(","):
        percentile = int(item.strip())
        if percentile < 0 or percentile > 100:
            raise ValueError(f"percentile must be between 0 and 100: {percentile}")
        values.append(percentile)
    if values != sorted(values):
        raise ValueError("percentiles must be sorted in ascending order")
    if len(set(values)) != len(values):
        raise ValueError("percentiles must not contain duplicates")
    return values


def parse_int_auto(raw: str) -> int:
    return int(raw, 16) if raw.lower().startswith("0x") else int(raw)


def parse_reference_gas_used(raw: str) -> list[int]:
    if not raw.strip():
        return []
    values = []
    for item in raw.split(","):
        gas_used = parse_int_auto(item.strip())
        if gas_used <= 0:
            raise ValueError(f"reference gasUsed must be positive: {gas_used}")
        values.append(gas_used)
    return values


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
            UnicodeDecodeError,
            http.client.HTTPException,
            OSError,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 8))

    raise RuntimeError(f"{method} failed after {retries} attempt(s): {last_error}")


def hex_to_int(value: str) -> int:
    return int(value, 16)


def wei_to_gwei(value: int) -> float:
    return value / GWEI


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "count": 0,
            "min": 0.0,
            "max": 0.0,
            "avg": 0.0,
            "median": 0.0,
            "p10": 0.0,
            "p25": 0.0,
            "p50": 0.0,
            "p75": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
        }

    return {
        "count": len(values),
        "min": round(min(values), 9),
        "max": round(max(values), 9),
        "avg": round(sum(values) / len(values), 9),
        "median": round(statistics.median(values), 9),
        "p10": round(percentile(values, 10), 9),
        "p25": round(percentile(values, 25), 9),
        "p50": round(percentile(values, 50), 9),
        "p75": round(percentile(values, 75), 9),
        "p90": round(percentile(values, 90), 9),
        "p95": round(percentile(values, 95), 9),
        "p99": round(percentile(values, 99), 9),
    }


def default_out_dir(blocks: int) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Path(__file__).resolve().parent / "fee_data" / f"mainnet_latest_{blocks}_{stamp}"


def sanitize_rpc_url(url: str, include_full: bool) -> str:
    if include_full:
        return url
    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "/...", "", ""))


def resolve_end_block(rpc_url: str, end_block: str, timeout: int, retries: int) -> tuple[int, str]:
    normalized = end_block.strip().lower()
    if normalized in {"latest", "safe", "finalized"}:
        if normalized == "latest":
            latest_hex = rpc(rpc_url, "eth_blockNumber", [], timeout, retries)
            return hex_to_int(latest_hex), normalized
        block = rpc(rpc_url, "eth_getBlockByNumber", [normalized, False], timeout, retries)
        if not block:
            raise RuntimeError(f"RPC returned no block for tag {normalized!r}")
        return hex_to_int(block["number"]), normalized
    return parse_int_auto(end_block), "number"


def collect_fee_history(
    rpc_url: str,
    blocks: int,
    end_block: int,
    percentiles: list[int],
    chunk_size: int,
    timeout: int,
    retries: int,
    checkpoint_path: Path | None,
) -> list[dict[str, Any]]:
    first = max(0, end_block - blocks + 1)

    rows: list[dict[str, Any]] = []
    next_start = first
    while next_start <= end_block:
        next_end = min(end_block, next_start + chunk_size - 1)
        count = next_end - next_start + 1
        result = rpc(
            rpc_url,
            "eth_feeHistory",
            [hex(count), hex(next_end), percentiles],
            timeout,
            retries,
        )

        oldest = hex_to_int(result["oldestBlock"])
        if oldest != next_start:
            raise RuntimeError(f"eth_feeHistory returned oldestBlock={oldest}, expected {next_start}")

        base_fees = [hex_to_int(item) for item in result["baseFeePerGas"]]
        rewards = [[hex_to_int(value) for value in row] for row in result.get("reward", [])]
        gas_used_ratios = result["gasUsedRatio"]
        if len(base_fees) != count + 1:
            raise RuntimeError(f"eth_feeHistory returned {len(base_fees)} baseFee entries, expected {count + 1}")
        if len(gas_used_ratios) != count:
            raise RuntimeError(f"eth_feeHistory returned {len(gas_used_ratios)} gasUsedRatio entries, expected {count}")
        if len(rewards) != count:
            raise RuntimeError(f"eth_feeHistory returned {len(rewards)} reward rows, expected {count}")

        for index in range(count):
            block_number = oldest + index
            base_fee_wei = base_fees[index]
            reward_values = rewards[index]
            if len(reward_values) != len(percentiles):
                raise RuntimeError(
                    f"eth_feeHistory reward row for block {block_number} has "
                    f"{len(reward_values)} values, expected {len(percentiles)}"
                )
            row: dict[str, Any] = {
                "blockNumber": block_number,
                "baseFeeWei": base_fee_wei,
                "baseFeeGwei": round(wei_to_gwei(base_fee_wei), 9),
                "gasUsedRatio": gas_used_ratios[index],
            }
            for percentile_value, reward_wei in zip(percentiles, reward_values):
                effective_wei = base_fee_wei + reward_wei
                row[f"rewardP{percentile_value}Wei"] = reward_wei
                row[f"rewardP{percentile_value}Gwei"] = round(wei_to_gwei(reward_wei), 9)
                row[f"effectiveP{percentile_value}Wei"] = effective_wei
                row[f"effectiveP{percentile_value}Gwei"] = round(wei_to_gwei(effective_wei), 9)
            rows.append(row)

        if checkpoint_path:
            write_history_csv(checkpoint_path, rows, percentiles)

        print(f"collected blocks {oldest}..{oldest + count - 1} ({len(rows)}/{blocks})")
        next_start = next_end + 1

    return rows


def write_history_csv(path: Path, rows: list[dict[str, Any]], percentiles: list[int]) -> None:
    fields = ["blockNumber", "baseFeeWei", "baseFeeGwei", "gasUsedRatio"]
    for percentile_value in percentiles:
        fields.extend(
            [
                f"rewardP{percentile_value}Wei",
                f"rewardP{percentile_value}Gwei",
                f"effectiveP{percentile_value}Wei",
                f"effectiveP{percentile_value}Gwei",
            ]
        )

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def estimate_fee_eth(gas_price_gwei: float, gas_used: int) -> float:
    return round(gas_price_gwei * GWEI * gas_used / 10**18, 12)


def build_estimated_tx_fees(reference_gas_used: list[int], effective_stats: dict[str, dict[str, float]]) -> dict[str, Any]:
    estimates: dict[str, Any] = {}
    for gas_used in reference_gas_used:
        row: dict[str, Any] = {}
        for field in ("effectiveP50Gwei", "effectiveP75Gwei", "effectiveP90Gwei"):
            stats = effective_stats.get(field)
            if not stats:
                continue
            row[field] = {
                "avg": estimate_fee_eth(float(stats["avg"]), gas_used),
                "median": estimate_fee_eth(float(stats["median"]), gas_used),
                "p75": estimate_fee_eth(float(stats["p75"]), gas_used),
                "p90": estimate_fee_eth(float(stats["p90"]), gas_used),
            }
        estimates[str(gas_used)] = row
    return estimates


def build_summary(
    args: argparse.Namespace,
    rpc_url: str,
    rows: list[dict[str, Any]],
    percentiles: list[int],
    reference_gas_used: list[int],
    end_block_source: str,
) -> dict[str, Any]:
    effective_stats = {}
    reward_stats = {}
    for percentile_value in percentiles:
        effective_key = f"effectiveP{percentile_value}Gwei"
        reward_key = f"rewardP{percentile_value}Gwei"
        effective_stats[effective_key] = summarize([float(row[effective_key]) for row in rows])
        reward_stats[reward_key] = summarize([float(row[reward_key]) for row in rows])

    base_stats = summarize([float(row["baseFeeGwei"]) for row in rows])
    gas_used_ratio_stats = summarize([float(row["gasUsedRatio"]) for row in rows])

    normal_reference = effective_stats.get("effectiveP50Gwei", {}).get("median", 0.0)
    normal_average_reference = effective_stats.get("effectiveP50Gwei", {}).get("avg", normal_reference)
    normal_p75_reference = effective_stats.get("effectiveP50Gwei", {}).get("p75", normal_reference)
    attacker_reference = effective_stats.get("effectiveP90Gwei", {}).get("median", normal_reference)

    return {
        "source": "eth_feeHistory",
        "dataKind": "block-level gas price percentile history",
        "rpcEndpoint": sanitize_rpc_url(rpc_url, args.include_rpc_url),
        "requestedBlocks": args.blocks,
        "collectedBlocks": len(rows),
        "requestedEndBlock": args.end_block,
        "resolvedEndBlockSource": end_block_source,
        "firstBlock": rows[0]["blockNumber"] if rows else None,
        "lastBlock": rows[-1]["blockNumber"] if rows else None,
        "percentiles": percentiles,
        "baseFeeGwei": base_stats,
        "gasUsedRatio": gas_used_ratio_stats,
        "priorityFeeGwei": reward_stats,
        "effectiveGasPriceGwei": effective_stats,
        "estimatedTransactionFeeEth": build_estimated_tx_fees(reference_gas_used, effective_stats),
        "recommendedExperimentGasPriceGwei": {
            "normalFromMedianEffectiveP50": normal_reference,
            "normalFromAverageEffectiveP50": normal_average_reference,
            "normalFromP75EffectiveP50": normal_p75_reference,
            "attackerFromMedianEffectiveP90": attacker_reference,
        },
        "unitNotes": {
            "baseFeeGwei": "Per-gas base fee for each sampled block.",
            "priorityFeeGwei": "Per-gas effective priority fee percentiles from eth_feeHistory.reward.",
            "effectiveGasPriceGwei": "Per-gas estimate: baseFeePerGas + priority fee percentile.",
            "estimatedTransactionFeeEth": "Derived estimate only: effective gas price multiplied by reference gasUsed.",
        },
        "notes": [
            "effectiveGasPrice is estimated as baseFeePerGas + effective priority fee percentile.",
            "This is block-level fee-market history, not per-transaction receipt-level average cost.",
            "Use BigQuery or receipts for formal gasUsed * effectiveGasPrice transaction-fee statistics.",
            "fee_history.partial.csv is written after every chunk so long scans keep usable intermediate output.",
        ],
    }


def main() -> int:
    args = parse_args()
    if args.blocks <= 0:
        raise SystemExit("--blocks must be positive")
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size must be positive")

    percentiles = parse_percentiles(args.percentiles)
    reference_gas_used = parse_reference_gas_used(args.reference_gas_used)
    load_default_env_files()
    rpc_url = resolve_rpc_url(args.rpc)
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir(args.blocks)
    out_dir.mkdir(parents=True, exist_ok=True)
    end_block, end_block_source = resolve_end_block(rpc_url, args.end_block, args.timeout, args.retries)

    rows = collect_fee_history(
        rpc_url=rpc_url,
        blocks=args.blocks,
        end_block=end_block,
        percentiles=percentiles,
        chunk_size=args.chunk_size,
        timeout=args.timeout,
        retries=args.retries,
        checkpoint_path=out_dir / "fee_history.partial.csv",
    )
    if not rows:
        raise RuntimeError("no fee history rows collected")

    history_path = out_dir / "fee_history.csv"
    summary_path = out_dir / "fee_summary.json"
    write_history_csv(history_path, rows, percentiles)
    summary = build_summary(args, rpc_url, rows, percentiles, reference_gas_used, end_block_source)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    print(f"history={history_path}")
    print(f"summary={summary_path}")
    print("recommendedExperimentGasPriceGwei=", json.dumps(summary["recommendedExperimentGasPriceGwei"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
