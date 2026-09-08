#!/usr/bin/env python3
"""Render a paper-like tx-fee-per-block figure for phased Deter-Z2 runs."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any


ETHER = 10**18

COLORS = {
    "baseline": "#2563eb",
    "attack_normal": "#dc2626",
    "attack_tx": "#ea580c",
    "baseline_marker": "#2563eb",
    "attack_normal_marker": "#dc2626",
    "attack_tx_marker": "#ea580c",
    "axis": "#27272a",
    "grid": "#d4d4d8",
    "text": "#18181b",
    "muted": "#52525b",
    "phase": "#f8fafc",
    "attack_phase": "#fff1f2",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot paper-like tx fee per block for paired phased runs.")
    parser.add_argument("--baseline-dir", required=True, help="Baseline run directory.")
    parser.add_argument("--attack-dir", required=True, help="Attack run directory.")
    parser.add_argument("--rpc", default=None, help="RPC URL for receipt lookup. Defaults to metrics.csv rpc.")
    parser.add_argument("--out", required=True, help="Output SVG path.")
    parser.add_argument("--csv-out", default=None, help="Derived fee-per-block CSV path.")
    parser.add_argument("--title", default="Paper-like Tx Fee per Block for Deter-Z2 on Besu")
    parser.add_argument("--cache", action="store_true", help="Write receipt-backed fee caches in each run directory.")
    parser.add_argument("--baseline-reference-estimate", action="store_true", help="Explicitly allow a dashed baseline proxy from pool balance and mean submitted price; not receipt-backed.")
    parser.add_argument("--baseline-gas-used", type=int, default=None, help="Measured per-normal-transaction gas used for the baseline proxy (not gas limit).")
    return parser.parse_args()


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text_value = str(value).strip()
    if text_value == "":
        return 0
    return int(text_value, 16) if text_value.startswith("0x") else int(text_value)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_metrics(run_dir: Path) -> dict[str, str]:
    path = run_dir / "metrics.csv"
    if not path.exists():
        return {}
    rows = read_csv(path)
    return rows[0] if rows else {}


def read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            tx_hash = row.get("hash")
            if tx_hash:
                records[str(tx_hash).lower()] = row
    return records


def rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["result"]


def receipt_fee_wei(receipt: dict[str, Any]) -> int:
    return as_int(receipt.get("gasUsed")) * as_int(receipt.get("effectiveGasPrice") or receipt.get("gasPrice"))


def block_to_tick(rows: list[dict[str, str]]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for row in rows:
        tick = as_int(row["globalTick"])
        start_block = as_int(row.get("startBlock"))
        target_block = as_int(row.get("targetBlock") or row.get("blockNumber"))
        for block_number in range(start_block + 1, target_block + 1):
            mapping[block_number] = tick
        observed_block = as_int(row.get("blockNumber"))
        if observed_block > 0:
            mapping[observed_block] = tick
    return mapping


def records_fee_by_tick_from_rpc(
    run_dir: Path,
    rows: list[dict[str, str]],
    record_file: str,
    rpc_url: str,
    cache_name: str,
    write_cache: bool,
) -> dict[int, int]:
    cache_path = run_dir / cache_name
    if cache_path.exists():
        cached_rows = read_csv(cache_path)
        return {as_int(row["globalTick"]): as_int(row["feeWei"]) for row in cached_rows}

    records_by_hash = read_records(run_dir / record_file)
    if not records_by_hash:
        return {}

    mapping = block_to_tick(rows)
    first_block = min(mapping)
    last_block = max(mapping)
    fees_by_tick: dict[int, int] = defaultdict(int)
    is_normal = record_file.startswith("normal")
    sampled_normal_gas_used: int | None = None

    for block_number in range(first_block, last_block + 1):
        block = rpc_call(rpc_url, "eth_getBlockByNumber", [hex(block_number), True])
        if not block:
            continue
        tick = mapping.get(block_number)
        if tick is None:
            continue
        for tx in block.get("transactions", []):
            if isinstance(tx, str):
                tx_hash = tx
                tx_obj: dict[str, Any] = {}
            else:
                tx_hash = tx.get("hash", "")
                tx_obj = tx
            key = str(tx_hash).lower()
            if key not in records_by_hash:
                continue
            if is_normal:
                if sampled_normal_gas_used is None:
                    receipt = rpc_call(rpc_url, "eth_getTransactionReceipt", [tx_hash])
                    if not receipt:
                        continue
                    sampled_normal_gas_used = as_int(receipt.get("gasUsed"))
                gas_price = as_int(tx_obj.get("gasPrice") or records_by_hash[key].get("gasPriceWei"))
                fees_by_tick[tick] += sampled_normal_gas_used * gas_price
            else:
                receipt = rpc_call(rpc_url, "eth_getTransactionReceipt", [tx_hash])
                if not receipt:
                    continue
                fees_by_tick[tick] += receipt_fee_wei(receipt)

    if write_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["globalTick", "feeWei"])
            writer.writeheader()
            for tick in sorted({as_int(row["globalTick"]) for row in rows}):
                writer.writerow({"globalTick": tick, "feeWei": fees_by_tick.get(tick, 0)})

    return dict(fees_by_tick)


def receipts_file_fee_by_tick(run_dir: Path, rows: list[dict[str, str]], receipt_file: str) -> dict[int, int]:
    receipts = read_json(run_dir / receipt_file)
    if not isinstance(receipts, dict) or not receipts:
        return {}
    mapping = block_to_tick(rows)
    fees_by_tick: dict[int, int] = defaultdict(int)
    for receipt in receipts.values():
        if not isinstance(receipt, dict):
            continue
        tick = mapping.get(as_int(receipt.get("blockNumber")))
        if tick is None:
            continue
        fees_by_tick[tick] += receipt_fee_wei(receipt)
    return dict(fees_by_tick)


def fee_by_tick(
    run_dir: Path,
    rows: list[dict[str, str]],
    receipt_file: str,
    record_file: str,
    rpc_url: str | None,
    cache_name: str,
    write_cache: bool,
) -> tuple[dict[int, int], str]:
    from_receipts = receipts_file_fee_by_tick(run_dir, rows, receipt_file)
    if from_receipts:
        return from_receipts, "receipt-json"
    archived_file = Path(receipt_file).stem + "_rpc.json"
    from_archive = receipts_file_fee_by_tick(run_dir, rows, archived_file)
    if from_archive:
        return from_archive, "archived-receipts"
    if not rpc_url:
        return {}, "missing"
    try:
        from_rpc = records_fee_by_tick_from_rpc(run_dir, rows, record_file, rpc_url, cache_name, write_cache)
    except (OSError, TimeoutError, urllib.error.URLError, RuntimeError):
        return {}, "missing"
    return from_rpc, "rpc-receipts" if from_rpc else "missing"


def baseline_reference(run_dir: Path, rows: list[dict[str, str]], gas_used: int) -> tuple[dict[int, int], list[dict[str, int]]]:
    """Pool-flow proxy assuming no baseline drops, replacements or foreign traffic."""
    records = read_records(run_dir / "normal_records.jsonl")
    if not records or gas_used <= 0:
        raise ValueError("Baseline reference needs normal records and measured gas used")
    total_price = sum(as_int(row["gasPriceWei"]) for row in records.values())
    accepted_before = 0
    pool_before = 0
    values = {}
    audit = []
    for row in rows:
        accepted = as_int(row["normalAcceptedTotal"])
        pool = as_int(row["txpoolPendingAfterBlock"]) + as_int(row["txpoolQueuedAfterBlock"])
        inferred = pool_before + accepted - accepted_before - pool
        if inferred < 0:
            raise ValueError("Baseline pool balance cannot support this fee proxy")
        tick = as_int(row["globalTick"])
        values[tick] = inferred * gas_used * total_price // len(records)
        audit.append({"globalTick": tick, "inferredDepartures": inferred,
                      "observedIncluded": as_int(row["normalIncludedInObservedBlocks"])})
        accepted_before, pool_before = accepted, pool
    return values, audit


def nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    base = 10**exponent
    for step in (1, 2, 5, 10):
        candidate = step * base
        if candidate >= value:
            return float(candidate)
    return float(10 * base)


def format_number(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    if abs(value) >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def eth(value_wei: int) -> float:
    return value_wei / ETHER


def centieth(value_wei: int) -> float:
    return eth(value_wei) * 100


def escape(value: str) -> str:
    return html.escape(value, quote=True)


def text(
    x: float,
    y: float,
    value: str,
    size: int = 13,
    weight: int = 400,
    anchor: str = "start",
    color: str = "text",
) -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" font-size="{size}" '
        f'font-weight="{weight}" fill="{COLORS[color]}">{escape(value)}</text>'
    )


class XScale:
    def __init__(self, left: float, width: float, min_tick: int, max_tick: int):
        self.left = left
        self.width = width
        self.min_tick = min_tick
        self.max_tick = max_tick
        self.count = max(1, max_tick - min_tick + 1)
        self.step = width / self.count

    def x(self, tick: int | float) -> float:
        return self.left + ((tick - self.min_tick) + 0.5) * self.step

    def boundary(self, tick: int | float) -> float:
        return self.left + (tick - self.min_tick) * self.step


class DualAxisPanel:
    def __init__(self, x: float, y: float, width: float, height: float, left_max: float, right_max: float):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.left_max = max(1e-12, left_max)
        self.right_max = max(1e-12, right_max)

    def y_left(self, value: float) -> float:
        return self.y + self.height - (value / self.left_max) * self.height

    def y_right(self, value: float) -> float:
        return self.y + self.height - (value / self.right_max) * self.height


def marker(x: float, y: float, color: str, shape: str, size: float = 5) -> str:
    if shape == "triangle":
        points = [(x, y - size), (x - size, y + size * 0.85), (x + size, y + size * 0.85)]
        return f'<polygon points="{" ".join(f"{px:.2f},{py:.2f}" for px, py in points)}" fill="{color}"/>'
    if shape == "cross":
        return (
            f'<line x1="{x - size:.2f}" y1="{y - size:.2f}" x2="{x + size:.2f}" y2="{y + size:.2f}" '
            f'stroke="{color}" stroke-width="2"/>'
            f'<line x1="{x - size:.2f}" y1="{y + size:.2f}" x2="{x + size:.2f}" y2="{y - size:.2f}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
    return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{size:.2f}" fill="{color}"/>'


def draw_line(points: list[tuple[float, float]], color: str, shape: str, dash: str = "") -> list[str]:
    if not points:
        return []
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    out = [
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.7" '
        f'stroke-linejoin="round" stroke-linecap="round"{dash_attr}/>'
    ]
    out.extend(marker(x, y, color, shape, 4.8) for x, y in points)
    return out


def draw_phase_bands(panel: DualAxisPanel, markers: list[dict[str, str]], xscale: XScale) -> list[str]:
    out: list[str] = []
    for row in markers:
        start = as_int(row["startGlobalTick"])
        end = as_int(row["endGlobalTick"])
        x1 = xscale.boundary(start)
        x2 = xscale.boundary(end + 1)
        phase = row["phase"]
        fill = COLORS["attack_phase"] if phase == "attack_on" else COLORS["phase"]
        opacity = 0.95 if phase == "attack_on" else 0.45
        out.append(
            f'<rect x="{x1:.2f}" y="{panel.y:.2f}" width="{x2 - x1:.2f}" height="{panel.height:.2f}" '
            f'fill="{fill}" opacity="{opacity:.2f}"/>'
        )
        out.append(
            f'<line x1="{x1:.2f}" y1="{panel.y:.2f}" x2="{x1:.2f}" y2="{panel.y + panel.height:.2f}" '
            f'stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
    if markers:
        x = xscale.boundary(as_int(markers[-1]["endGlobalTick"]) + 1)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y:.2f}" x2="{x:.2f}" y2="{panel.y + panel.height:.2f}" '
            f'stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
    return out


def draw_phase_labels(markers: list[dict[str, str]], xscale: XScale, y: float) -> list[str]:
    labels = {
        "warmup": "Warmup",
        "saturation": "Saturation",
        "control": "Control",
        "attack_on": "Attack on",
        "recovery": "Recovery",
        "drain": "Drain",
    }
    out: list[str] = []
    for row in markers:
        start = as_int(row["startGlobalTick"])
        end = as_int(row["endGlobalTick"])
        x1 = xscale.boundary(start)
        x2 = xscale.boundary(end + 1)
        out.append(text((x1 + x2) / 2, y, labels.get(row["phase"], row["phase"]), 12, 700, anchor="middle", color="muted"))
    return out


def draw_axes(panel: DualAxisPanel, xscale: XScale) -> list[str]:
    out = [
        f'<rect x="{panel.x:.2f}" y="{panel.y:.2f}" width="{panel.width:.2f}" height="{panel.height:.2f}" '
        f'fill="white" stroke="{COLORS["axis"]}" stroke-width="1"/>'
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = panel.y + panel.height - frac * panel.height
        out.append(
            f'<line x1="{panel.x:.2f}" y1="{y:.2f}" x2="{panel.x + panel.width:.2f}" y2="{y:.2f}" '
            f'stroke="{COLORS["grid"]}" stroke-width="1"/>'
        )
        out.append(text(panel.x - 12, y + 4, format_number(frac * panel.left_max), 12, anchor="end", color="muted"))
        out.append(text(panel.x + panel.width + 12, y + 4, format_number(frac * panel.right_max), 12, anchor="start", color="muted"))
    for tick in range(xscale.min_tick, xscale.max_tick + 1):
        x = xscale.x(tick)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y + panel.height:.2f}" x2="{x:.2f}" '
            f'y2="{panel.y + panel.height + 5:.2f}" stroke="{COLORS["axis"]}" stroke-width="1"/>'
        )
        if tick == xscale.min_tick or tick == xscale.max_tick or tick % 2 == 0:
            out.append(text(x, panel.y + panel.height + 22, str(tick), 12, anchor="middle", color="muted"))
    out.append(
        f'<text x="{panel.x - 66:.2f}" y="{panel.y + panel.height / 2:.2f}" text-anchor="middle" '
        f'font-size="13" fill="{COLORS["muted"]}" transform="rotate(-90 {panel.x - 66:.2f} {panel.y + panel.height / 2:.2f})">'
        "Normal tx fee per block (10^-2 ETH)</text>"
    )
    out.append(
        f'<text x="{panel.x + panel.width + 78:.2f}" y="{panel.y + panel.height / 2:.2f}" text-anchor="middle" '
        f'font-size="13" fill="{COLORS["muted"]}" transform="rotate(90 {panel.x + panel.width + 78:.2f} {panel.y + panel.height / 2:.2f})">'
        "Adversarial tx fee per block (ETH)</text>"
    )
    out.append(text(panel.x + panel.width / 2, panel.y + panel.height + 55, "Aligned phase tick", 13, anchor="middle", color="muted"))
    return out


def legend_item(x: float, y: float, color: str, label: str, shape: str, dash: str = "") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x + 28:.2f}" y2="{y:.2f}" stroke="{color}" '
        f'stroke-width="2.7" stroke-linecap="round"{dash_attr}/>'
        f'{marker(x + 14, y, color, shape, 4.8)}'
        f'<text x="{x + 38:.2f}" y="{y + 4:.2f}" font-size="12" fill="{COLORS["text"]}">{escape(label)}</text>'
    )


def values_to_points(values_by_tick: dict[int, int], ticks: list[int], xscale: XScale, panel: DualAxisPanel, axis: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for tick in ticks:
        value = values_by_tick.get(tick, 0)
        y = panel.y_right(eth(value)) if axis == "right" else panel.y_left(centieth(value))
        out.append((xscale.x(tick), y))
    return out


def write_derived_csv(
    path: Path,
    ticks: list[int],
    phase_by_tick: dict[int, str],
    baseline_normal: dict[int, int],
    attack_normal: dict[int, int],
    attack_tx: dict[int, int],
    baseline_phase_by_tick: dict[int, str] | None = None,
    baseline_unknown_ticks: set[int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "globalTick",
                "phase",
                "baselineNormalFeeEth",
                "baselineNormalFeeCentiEth",
                "attackRunNormalFeeEth",
                "attackRunNormalFeeCentiEth",
                "attackTxFeeEth",
                "baselinePhase",
            ],
        )
        writer.writeheader()
        for tick in ticks:
            writer.writerow(
                {
                    "globalTick": tick,
                    "phase": phase_by_tick.get(tick, ""),
                    "baselineNormalFeeEth": f"{eth(baseline_normal.get(tick, 0)):.12f}" if (baseline_phase_by_tick is None or tick in baseline_phase_by_tick) and tick not in (baseline_unknown_ticks or set()) else "",
                    "baselineNormalFeeCentiEth": f"{centieth(baseline_normal.get(tick, 0)):.12f}" if (baseline_phase_by_tick is None or tick in baseline_phase_by_tick) and tick not in (baseline_unknown_ticks or set()) else "",
                    "attackRunNormalFeeEth": f"{eth(attack_normal.get(tick, 0)):.12f}",
                    "attackRunNormalFeeCentiEth": f"{centieth(attack_normal.get(tick, 0)):.12f}",
                    "attackTxFeeEth": f"{eth(attack_tx.get(tick, 0)):.12f}",
                    "baselinePhase": (baseline_phase_by_tick or {}).get(tick, ""),
                }
            )


def render(args: argparse.Namespace) -> None:
    baseline_dir = Path(args.baseline_dir)
    attack_dir = Path(args.attack_dir)
    baseline_rows = read_csv(baseline_dir / "timeseries.csv")
    attack_rows = read_csv(attack_dir / "timeseries.csv")
    markers = read_csv(attack_dir / "phase_markers.csv")
    baseline_metrics = read_metrics(baseline_dir)
    attack_metrics = read_metrics(attack_dir)
    rpc_url = args.rpc or attack_metrics.get("rpc") or baseline_metrics.get("rpc")

    baseline_audit = []
    baseline_unknown_ticks = set()
    if args.baseline_reference_estimate:
        if not args.baseline_gas_used:
            raise ValueError("--baseline-reference-estimate requires --baseline-gas-used")
        baseline_normal, baseline_audit = baseline_reference(baseline_dir, baseline_rows, args.baseline_gas_used)
        baseline_source = "estimated-pool-flow-reference"
        # Contradictory pool/count snapshots cannot locate departures in a block.
        for index, row in enumerate(baseline_audit):
            if row["inferredDepartures"] != row["observedIncluded"]:
                baseline_unknown_ticks.add(row["globalTick"])
                if index:
                    baseline_unknown_ticks.add(baseline_audit[index - 1]["globalTick"])
    else:
        baseline_normal, baseline_source = fee_by_tick(
            baseline_dir, baseline_rows, "normal_receipts.json", "normal_records.jsonl",
            rpc_url, "normal_fee_by_tick_from_rpc.csv", args.cache,
        )
    attack_normal, attack_normal_source = fee_by_tick(
        attack_dir,
        attack_rows,
        "normal_receipts.json",
        "normal_records.jsonl",
        rpc_url,
        "normal_fee_by_tick_from_rpc.csv",
        args.cache,
    )
    attack_tx, attack_source = fee_by_tick(
        attack_dir,
        attack_rows,
        "attack_receipts.json",
        "attack_records.jsonl",
        rpc_url,
        "attack_fee_by_tick_from_rpc.csv",
        args.cache,
    )
    if not baseline_normal or not attack_tx:
        raise RuntimeError(
            "not enough receipt-backed fee data; keep the enclave running and pass --rpc, or collect receipts first"
        )

    ticks = sorted({as_int(row["globalTick"]) for row in baseline_rows + attack_rows})
    phase_by_tick = {as_int(row["globalTick"]): row["phase"] for row in attack_rows}
    xscale = XScale(112, 910, min(ticks), max(ticks))
    normal_y_max = nice_max(max([centieth(value) for tick, value in baseline_normal.items() if tick not in baseline_unknown_ticks] + [centieth(value) for value in attack_normal.values()] + [0.0]))
    attack_y_max = nice_max(max([eth(value) for value in attack_tx.values()] + [0.0]))
    panel = DualAxisPanel(112, 170, 910, 360, normal_y_max, attack_y_max)

    width = 1180
    height = 720
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(112, 46, args.title, 26, 800),
        text(112, 76, "Attack-run fees: sum of receipt gasUsed x effectiveGasPrice.", 13, color="muted"),
        text(112, 98, "Baseline: estimated reference (dashed)." if args.baseline_reference_estimate else "Baseline fees: receipt-backed.", 12, color="muted"),
    ]
    out.extend(draw_phase_labels(markers, xscale, 153))
    out.extend(draw_axes(panel, xscale))
    out.extend(draw_phase_bands(panel, markers, xscale))

    baseline_ticks = sorted({as_int(row["globalTick"]) for row in baseline_rows})
    baseline_dash = "6 4" if args.baseline_reference_estimate else ""
    segment = []
    for tick in baseline_ticks:
        if tick in baseline_unknown_ticks:
            out.extend(draw_line(values_to_points(baseline_normal, segment, xscale, panel, "left"), COLORS["baseline"], "circle", baseline_dash))
            segment = []
        else:
            segment.append(tick)
    out.extend(draw_line(values_to_points(baseline_normal, segment, xscale, panel, "left"), COLORS["baseline"], "circle", baseline_dash))
    out.extend(draw_line(values_to_points(attack_normal, ticks, xscale, panel, "left"), COLORS["attack_normal"], "triangle"))
    out.extend(draw_line(values_to_points(attack_tx, ticks, xscale, panel, "right"), COLORS["attack_tx"], "cross"))

    baseline_label = "Normal tx fee (baseline; estimated)" if args.baseline_reference_estimate else "Normal tx fee (baseline)"
    out.append(legend_item(675, 73, COLORS["baseline"], baseline_label, "circle", baseline_dash))
    out.append(legend_item(675, 97, COLORS["attack_normal"], "Normal tx fee (attack run)", "triangle"))
    out.append(legend_item(675, 121, COLORS["attack_tx"], "Adversarial tx fee (attack run)", "cross"))

    peak_tick = max(attack_tx, key=lambda tick: attack_tx[tick])
    peak_fee = eth(attack_tx[peak_tick])
    if peak_fee > 0:
        fee_ticks = sorted(tick for tick in attack_tx if attack_tx[tick] > 0)
        label = f"Attack fee peak: {peak_fee:.3f} ETH" if len(fee_ticks) == 1 else f"Attack fees across {len(fee_ticks)} blocks: {eth(sum(attack_tx.values())):.4f} ETH"
        out.append(text(xscale.x(peak_tick) - 10, panel.y_right(peak_fee) - 18, label, 12, 700, color="attack_tx"))

    stall_candidates = [
        tick
        for tick in ticks
        if baseline_normal.get(tick, 0) > 0 and attack_normal.get(tick, 0) == 0 and phase_by_tick.get(tick) == "attack_on"
    ]
    if stall_candidates:
        tick = stall_candidates[0]
        out.append(text(xscale.x(tick) + 14, panel.y_left(normal_y_max * 0.11), "normal fee drops to 0 at attack start", 12, 700, color="attack_normal"))

    baseline_prefix = "Baseline estimated total" if args.baseline_reference_estimate else "Baseline normal total"
    out.append(text(112, 612, f"{baseline_prefix}: {eth(sum(baseline_normal.values())):.4f} ETH", 13, 700))
    out.append(text(450, 612, f"Attack-run normal total: {eth(sum(attack_normal.values())):.4f} ETH", 13, 700))
    out.append(text(800, 612, f"Adversarial total: {eth(sum(attack_tx.values())):.4f} ETH", 13, 700))
    if args.baseline_reference_estimate:
        out.append(text(112, 641, "Baseline proxy: inferred pool departures x measured normal gas used x mean submitted gas price.", 12, color="muted"))
        missing_note = ", ".join(map(str, sorted(baseline_unknown_ticks)))
        out.append(text(112, 662, f"Assumes no baseline drops; uncertain snapshot ticks ({missing_note}) are left blank. Baseline per-block fees are not measured.", 12, color="muted"))
    def schedule(rows):
        return [(r["phase"].replace("control", "attack_on"), as_int(r["phaseTick"])) for r in rows]
    unmatched = schedule(baseline_rows) != schedule(attack_rows)
    if unmatched:
        out.append(text(112, 688, f"Unmatched reference: baseline {len(baseline_rows)} ticks / {baseline_metrics.get('normalSubmitted')} normal txs; attack {len(attack_rows)} ticks / {attack_metrics.get('normalSubmitted')} normal txs. Phase labels refer to attack.", 12, color="muted"))
    out.append("</svg>\n")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")

    csv_out = Path(args.csv_out) if args.csv_out else out_path.with_suffix(".csv")
    baseline_phase_by_tick = {as_int(row["globalTick"]): row["phase"] for row in baseline_rows}
    write_derived_csv(csv_out, ticks, phase_by_tick, baseline_normal, attack_normal, attack_tx, baseline_phase_by_tick, baseline_unknown_ticks)
    provenance = {
        "baselineDir": str(baseline_dir), "attackDir": str(attack_dir),
        "baselineSource": baseline_source, "attackNormalSource": attack_normal_source,
        "attackSource": attack_source, "unmatchedSchedules": unmatched,
        "baselineGasUsedForEstimate": args.baseline_gas_used, "baselinePoolFlowAudit": baseline_audit,
        "baselineUnknownTicks": sorted(baseline_unknown_ticks),
        "baselineEstimated": args.baseline_reference_estimate,
        "baselineFeeWei": sum(baseline_normal.values()), "attackNormalFeeWei": sum(attack_normal.values()),
        "attackFeeWei": sum(attack_tx.values()),
    }
    out_path.with_suffix(".provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(out_path)
    print(csv_out)


def main() -> int:
    render(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
