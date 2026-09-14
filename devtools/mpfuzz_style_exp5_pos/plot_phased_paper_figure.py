#!/usr/bin/env python3
"""Render a compact paper-style SVG for phased Deter-Z2 evaluations."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


ETHER = 10**18

COLORS = {
    "baseline": "#1d4ed8",
    "attack_normal": "#dc2626",
    "attack_tx": "#ea580c",
    "baseline_pool": "#0f766e",
    "attack_pool": "#7c3aed",
    "attack_queue": "#9333ea",
    "axis": "#27272a",
    "grid": "#d4d4d8",
    "text": "#18181b",
    "muted": "#52525b",
    "phase": "#f8fafc",
    "attack_phase": "#fff1f2",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot phased Deter-Z2 CSVs as a paper-style SVG.")
    parser.add_argument("--baseline-dir", required=True, help="Directory containing baseline timeseries.csv.")
    parser.add_argument("--attack-dir", default=None, help="Directory containing attack timeseries.csv. Omit for a baseline-only preview.")
    parser.add_argument("--out", required=True, help="Output SVG path.")
    parser.add_argument("--title", default="Phased Evaluation of Deter-Z2 on Besu")
    parser.add_argument(
        "--top-metric",
        choices=["fee", "count"],
        default="fee",
        help="Top panel metric. fee plots gasUsed x effectiveGasPrice per observed block window; count plots included tx count.",
    )
    parser.add_argument(
        "--fee-unit",
        choices=["eth", "centieth"],
        default="eth",
        help="Fee axis unit for --fee-layout combined. Split fee layout uses 10^-2 ETH for normal tx and ETH for attack tx.",
    )
    parser.add_argument(
        "--fee-layout",
        choices=["split", "combined"],
        default="split",
        help="Fee plot layout. split renders normal fee, adversarial fee, and txpool pressure as separate panels.",
    )
    parser.add_argument(
        "--pool-metric",
        choices=["count", "gas"],
        default="count",
        help="Bottom-panel txpool pressure metric. count plots transaction count; gas plots pending/queued gas limit in million gas.",
    )
    parser.add_argument("--derived-csv-out", default=None, help="Optional CSV for top-panel derived values.")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_optional_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return read_csv(path)


def read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_receipt_json(run_dir: Path, filename: str) -> Any:
    receipts = read_json(run_dir / filename)
    if receipts:
        return receipts
    rpc_filename = filename.replace(".json", "_rpc.json")
    return read_json(run_dir / rpc_filename)


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    value = str(value).strip()
    if value == "":
        return 0
    if value.startswith("0x"):
        return int(value, 16)
    return int(value)


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
        return f"{value:.2f}"
    return f"{value:.3f}".rstrip("0").rstrip(".")


class Scale:
    def __init__(self, left: float, width: float, ticks: list[int]):
        self.left = left
        self.width = width
        self.min_tick = min(ticks)
        self.max_tick = max(ticks)
        self.span = max(1, self.max_tick - self.min_tick)

    def x(self, tick: int | float) -> float:
        return self.left + ((tick - self.min_tick) / self.span) * self.width


class Panel:
    def __init__(self, x: float, y: float, width: float, height: float, y_max: float, title: str, y_label: str):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.y_max = max(1e-12, y_max)
        self.title = title
        self.y_label = y_label

    def y_value(self, value: float) -> float:
        return self.y + self.height - (value / self.y_max) * self.height


def row_points(rows: list[dict[str, str]], field: str, xscale: Scale, panel: Panel) -> list[tuple[float, float]]:
    return [(xscale.x(as_int(row["globalTick"])), panel.y_value(float(as_int(row[field])))) for row in rows]


def value_points(values_by_tick: dict[int, float], ticks: list[int], xscale: Scale, panel: Panel) -> list[tuple[float, float]]:
    return [(xscale.x(tick), panel.y_value(values_by_tick.get(tick, 0.0))) for tick in ticks]


def polyline(points: list[tuple[float, float]], color: str, width: float = 2.6, dash: str = "") -> str:
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linejoin="round" stroke-linecap="round"{dash_attr}/>'
    )


def circles(points: list[tuple[float, float]], color: str, radius: float = 3.3) -> str:
    return "\n".join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius}" fill="{color}"/>' for x, y in points)


def text(x: float, y: float, value: str, size: int = 13, weight: int = 400, anchor: str = "start", color: str = "text") -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" font-size="{size}" '
        f'font-weight="{weight}" fill="{COLORS[color]}">{html.escape(value)}</text>'
    )


def draw_axes(panel: Panel, xscale: Scale, show_x_labels: bool) -> list[str]:
    out: list[str] = []
    out.append(
        f'<rect x="{panel.x:.2f}" y="{panel.y:.2f}" width="{panel.width:.2f}" height="{panel.height:.2f}" '
        f'fill="white" stroke="{COLORS["axis"]}" stroke-width="1"/>'
    )
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = panel.y + panel.height - frac * panel.height
        value = frac * panel.y_max
        out.append(
            f'<line x1="{panel.x:.2f}" y1="{y:.2f}" x2="{panel.x + panel.width:.2f}" y2="{y:.2f}" '
            f'stroke="{COLORS["grid"]}" stroke-width="1"/>'
        )
        out.append(text(panel.x - 10, y + 4, format_number(value), 12, anchor="end", color="muted"))

    for tick in range(xscale.min_tick, xscale.max_tick + 1):
        x = xscale.x(tick)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y + panel.height:.2f}" x2="{x:.2f}" '
            f'y2="{panel.y + panel.height + 5:.2f}" stroke="{COLORS["axis"]}" stroke-width="1"/>'
        )
        if show_x_labels and (tick == xscale.min_tick or tick == xscale.max_tick or tick % 2 == 0):
            out.append(text(x, panel.y + panel.height + 21, str(tick), 12, anchor="middle", color="muted"))

    out.append(text(panel.x, panel.y - 13, panel.title, 15, 700))
    out.append(
        f'<text x="{panel.x - 58:.2f}" y="{panel.y + panel.height / 2:.2f}" text-anchor="middle" '
        f'font-size="12" fill="{COLORS["muted"]}" transform="rotate(-90 {panel.x - 58:.2f} {panel.y + panel.height / 2:.2f})">'
        f'{html.escape(panel.y_label)}</text>'
    )
    return out


def draw_phase_bands(panel: Panel, markers: list[dict[str, str]], xscale: Scale) -> list[str]:
    out: list[str] = []
    for marker in markers:
        phase = marker["phase"]
        x1 = xscale.x(as_int(marker["startGlobalTick"]) - 0.5)
        x2 = xscale.x(as_int(marker["endGlobalTick"]) + 0.5)
        fill = COLORS["attack_phase"] if phase == "attack_on" else COLORS["phase"]
        opacity = "0.92" if phase == "attack_on" else "0.45"
        out.append(
            f'<rect x="{x1:.2f}" y="{panel.y:.2f}" width="{x2 - x1:.2f}" height="{panel.height:.2f}" '
            f'fill="{fill}" opacity="{opacity}"/>'
        )
        out.append(
            f'<line x1="{x1:.2f}" y1="{panel.y:.2f}" x2="{x1:.2f}" y2="{panel.y + panel.height:.2f}" '
            f'stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
    if markers:
        x = xscale.x(as_int(markers[-1]["endGlobalTick"]) + 0.5)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y:.2f}" x2="{x:.2f}" y2="{panel.y + panel.height:.2f}" '
            f'stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
    return out


def draw_phase_labels(markers: list[dict[str, str]], xscale: Scale, y: float) -> list[str]:
    labels = {
        "warmup": "Warmup",
        "saturation": "Saturation",
        "attack_on": "Attack on",
        "recovery": "Recovery",
        "drain": "Drain",
        "control": "Control",
    }
    out: list[str] = []
    for marker in markers:
        x1 = xscale.x(as_int(marker["startGlobalTick"]) - 0.5)
        x2 = xscale.x(as_int(marker["endGlobalTick"]) + 0.5)
        phase = labels.get(marker["phase"], marker["phase"])
        out.append(text((x1 + x2) / 2, y, phase, 12, 700, anchor="middle", color="muted"))
    return out


def legend_item(x: float, y: float, color: str, label: str, dash: str = "") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x + 26:.2f}" y2="{y:.2f}" stroke="{color}" '
        f'stroke-width="3" stroke-linecap="round"{dash_attr}/>'
        f'<text x="{x + 34:.2f}" y="{y + 4:.2f}" font-size="12" fill="{COLORS["text"]}">{html.escape(label)}</text>'
    )


def draw_bars(rows: list[dict[str, str]], field: str, xscale: Scale, panel: Panel, color: str) -> list[str]:
    out: list[str] = []
    step = panel.width / max(1, xscale.span)
    bar_width = step * 0.36
    for row in rows:
        value = as_int(row[field])
        if value <= 0:
            continue
        x = xscale.x(as_int(row["globalTick"])) - bar_width / 2
        y = panel.y_value(value)
        out.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{panel.y + panel.height - y:.2f}" '
            f'fill="{color}" opacity="0.68"/>'
        )
    return out


def draw_value_bars(values_by_tick: dict[int, float], ticks: list[int], xscale: Scale, panel: Panel, color: str) -> list[str]:
    out: list[str] = []
    step = panel.width / max(1, xscale.span)
    bar_width = step * 0.36
    for tick in ticks:
        value = values_by_tick.get(tick, 0.0)
        if value <= 0:
            continue
        x = xscale.x(tick) - bar_width / 2
        y = panel.y_value(value)
        out.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{panel.y + panel.height - y:.2f}" '
            f'fill="{color}" opacity="0.68"/>'
        )
    return out


def annotate(x: float, y: float, label: str, color: str = "muted") -> str:
    safe = html.escape(label)
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-size="12" font-weight="700" fill="{COLORS[color]}">{safe}</text>'
    )


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


def receipt_fee_wei(receipt: dict[str, Any]) -> int:
    gas_used = as_int(receipt.get("gasUsed"))
    gas_price = as_int(receipt.get("effectiveGasPrice") or receipt.get("gasPrice"))
    return gas_used * gas_price


def convert_fee_wei(fee_wei: int, fee_unit: str) -> float:
    multiplier = 100 if fee_unit == "centieth" else 1
    return (fee_wei / ETHER) * multiplier


def receipt_fees_by_tick(run_dir: Path, rows: list[dict[str, str]], filename: str, fee_unit: str) -> dict[int, float]:
    mapping = block_to_tick(rows)
    receipts = read_receipt_json(run_dir, filename)
    fees: dict[int, int] = defaultdict(int)
    for receipt in receipts.values():
        if not isinstance(receipt, dict):
            continue
        tick = mapping.get(as_int(receipt.get("blockNumber")))
        if tick is None:
            continue
        fees[tick] += receipt_fee_wei(receipt)
    return {tick: convert_fee_wei(fee_wei, fee_unit) for tick, fee_wei in fees.items()}


def metrics_row(run_dir: Path) -> dict[str, str]:
    rows = read_optional_csv(run_dir / "metrics.csv")
    return rows[0] if rows else {}


def metric_int(row: dict[str, str], key: str, default: int) -> int:
    value = row.get(key)
    if value is None or value == "":
        return default
    return as_int(value)


def gas_price_wei(metrics: dict[str, str], kind: str) -> int:
    price_unit = metric_int(metrics, "priceUnitWei", 571428571428)
    if kind == "normal" and metrics.get("normalGasPriceMode") == "sampled":
        sampled_avg = metrics.get("normalGasPriceAvgWei")
        if sampled_avg not in (None, ""):
            return as_int(sampled_avg)
    price_multiplier = metric_int(metrics, "attackerPrice" if kind == "attack" else "normalPrice", 7 if kind == "attack" else 3)
    return price_multiplier * price_unit


def estimated_fees_by_tick(
    rows: list[dict[str, str]],
    count_field: str,
    gas_limit: int,
    gas_price: int,
    fee_unit: str,
) -> dict[int, float]:
    unit_fee_wei = gas_limit * gas_price
    return {
        as_int(row["globalTick"]): convert_fee_wei(as_int(row[count_field]) * unit_fee_wei, fee_unit)
        for row in rows
    }


def fee_values_by_tick(
    run_dir: Path,
    rows: list[dict[str, str]],
    metrics: dict[str, str],
    receipt_file: str,
    count_field: str,
    kind: str,
    fee_unit: str,
) -> tuple[dict[int, float], str]:
    actual_values = receipt_fees_by_tick(run_dir, rows, receipt_file, fee_unit)
    included_count = sum(as_int(row[count_field]) for row in rows)
    if actual_values or included_count == 0:
        return actual_values, "actual"

    gas_field = "attackGas" if kind == "attack" else "normalGas"
    default_gas = 500000 if kind == "attack" else 150000
    estimated_values = estimated_fees_by_tick(
        rows,
        count_field,
        metric_int(metrics, gas_field, default_gas),
        gas_price_wei(metrics, kind),
        fee_unit,
    )
    return estimated_values, "estimated"


def txpool_values_by_tick(
    rows: list[dict[str, str]],
    metrics: dict[str, str],
    field: str,
    pool_metric: str,
    kind: str = "normal",
) -> dict[int, float]:
    if pool_metric == "count":
        return {as_int(row["globalTick"]): float(as_int(row[field])) for row in rows}

    gas_field = "attackGas" if kind == "attack" else "normalGas"
    default_gas = 500000 if kind == "attack" else 150000
    gas_limit = metric_int(metrics, gas_field, default_gas)
    return {
        as_int(row["globalTick"]): (as_int(row[field]) * gas_limit) / 1_000_000
        for row in rows
    }


def pool_axis_title(pool_metric: str) -> str:
    return "Txpool gas pressure after each block" if pool_metric == "gas" else "Transaction-pool pressure after each block"


def pool_axis_label(pool_metric: str) -> str:
    return "Gas in txpool (M gas)" if pool_metric == "gas" else "Txs in txpool"


def pool_legend_label(prefix: str, pool_metric: str, queued: bool = False) -> str:
    suffix = "queued txpool" if queued else "pending txpool"
    if pool_metric == "gas":
        return f"{prefix} {suffix} gas"
    return f"{prefix} {suffix}"


def write_top_metric_csv(
    out_path: Path,
    ticks: list[int],
    top_metric: str,
    fee_unit: str,
    baseline_values: dict[int, float],
    attack_normal_values: dict[int, float],
    attack_values: dict[int, float],
) -> None:
    value_suffix = "FeeEth" if fee_unit == "eth" else "FeeCentiEth"
    if top_metric == "count":
        value_suffix = "Count"
    fieldnames = [
        "globalTick",
        f"baselineNormal{value_suffix}",
        f"attackRunNormal{value_suffix}",
        f"attackTx{value_suffix}",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for tick in ticks:
            writer.writerow(
                {
                    "globalTick": tick,
                    f"baselineNormal{value_suffix}": f"{baseline_values.get(tick, 0.0):.12f}",
                    f"attackRunNormal{value_suffix}": f"{attack_normal_values.get(tick, 0.0):.12f}",
                    f"attackTx{value_suffix}": f"{attack_values.get(tick, 0.0):.12f}",
                }
            )


def scale_values(values: dict[int, float], multiplier: float) -> dict[int, float]:
    return {tick: value * multiplier for tick, value in values.items()}


def max_value(values_by_tick: dict[int, float], ticks: list[int]) -> float:
    return max([values_by_tick.get(tick, 0.0) for tick in ticks] + [0.0])


def sum_values(values_by_tick: dict[int, float]) -> float:
    return sum(values_by_tick.values())


def write_split_fee_csv(
    out_path: Path,
    ticks: list[int],
    baseline_normal_fee_eth: dict[int, float],
    attack_normal_fee_eth: dict[int, float],
    attack_fee_eth: dict[int, float],
) -> None:
    fieldnames = [
        "globalTick",
        "baselineNormalFeeEth",
        "baselineNormalFeeCentiEth",
        "attackRunNormalFeeEth",
        "attackRunNormalFeeCentiEth",
        "attackTxFeeEth",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for tick in ticks:
            baseline_normal = baseline_normal_fee_eth.get(tick, 0.0)
            attack_normal = attack_normal_fee_eth.get(tick, 0.0)
            attack_fee = attack_fee_eth.get(tick, 0.0)
            writer.writerow(
                {
                    "globalTick": tick,
                    "baselineNormalFeeEth": f"{baseline_normal:.12f}",
                    "baselineNormalFeeCentiEth": f"{baseline_normal * 100:.12f}",
                    "attackRunNormalFeeEth": f"{attack_normal:.12f}",
                    "attackRunNormalFeeCentiEth": f"{attack_normal * 100:.12f}",
                    "attackTxFeeEth": f"{attack_fee:.12f}",
                }
            )


def render_fee_split(
    baseline: list[dict[str, str]],
    attack: list[dict[str, str]],
    markers: list[dict[str, str]],
    baseline_metrics: dict[str, str],
    attack_metrics: dict[str, str],
    baseline_normal_fee_eth: dict[int, float],
    attack_normal_fee_eth: dict[int, float],
    attack_fee_eth: dict[int, float],
    pool_max: float,
    ticks: list[int],
    xscale: Scale,
    out_path: Path,
    title_value: str,
    source_note: str,
    pool_metric: str,
    derived_csv_out: Path | None,
) -> None:
    baseline_normal_fee_centieth = scale_values(baseline_normal_fee_eth, 100)
    attack_normal_fee_centieth = scale_values(attack_normal_fee_eth, 100)
    normal_fee_max = nice_max(
        max(
            [
                max_value(baseline_normal_fee_centieth, ticks),
                max_value(attack_normal_fee_centieth, ticks),
            ]
        )
    )
    attack_fee_max = nice_max(max_value(attack_fee_eth, ticks))

    width = 1200
    height = 950
    normal_panel = Panel(96, 165, 1016, 190, normal_fee_max, "Normal transaction fee", "Normal tx fee per block (10^-2 ETH)")
    attack_panel = Panel(96, 435, 1016, 150, attack_fee_max, "Adversarial transaction fee", "Adversarial tx fee per block (ETH)")
    pool_panel = Panel(96, 680, 1016, 155, pool_max, pool_axis_title(pool_metric), pool_axis_label(pool_metric))

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(96, 48, title_value, 26, 800),
        text(96, 75, source_note, 13, color="muted"),
        text(96, 96, "Aligned phase tick; each tick corresponds to one observed block window in the phased workload.", 12, color="muted"),
    ]

    out.extend(draw_phase_labels(markers, xscale, 130))
    for panel in (normal_panel, attack_panel, pool_panel):
        out.extend(draw_phase_bands(panel, markers, xscale))
        out.extend(draw_axes(panel, xscale, show_x_labels=(panel is pool_panel)))

    baseline_normal_points = value_points(baseline_normal_fee_centieth, ticks, xscale, normal_panel)
    attack_normal_points = value_points(attack_normal_fee_centieth, ticks, xscale, normal_panel)
    out.append(polyline(baseline_normal_points, COLORS["baseline"], 2.8))
    out.append(circles(baseline_normal_points, COLORS["baseline"], 3.1))
    out.append(polyline(attack_normal_points, COLORS["attack_normal"], 2.8))
    out.append(circles(attack_normal_points, COLORS["attack_normal"], 3.1))
    out.append(legend_item(720, normal_panel.y + 24, COLORS["baseline"], "normal tx fee (baseline)"))
    out.append(legend_item(720, normal_panel.y + 46, COLORS["attack_normal"], "normal tx fee (attack run)"))
    out.append(annotate(106, normal_panel.y + normal_panel.height - 16, "normal fee collapses during attack burst", "attack_normal"))

    attack_fee_points = value_points(attack_fee_eth, ticks, xscale, attack_panel)
    out.append(polyline(attack_fee_points, COLORS["attack_tx"], 2.8))
    out.append(circles(attack_fee_points, COLORS["attack_tx"], 3.1))
    out.append(legend_item(720, attack_panel.y + 24, COLORS["attack_tx"], "adversarial tx fee (attack run)"))
    peak_tick = max(ticks, key=lambda tick: attack_fee_eth.get(tick, 0.0))
    peak_fee = attack_fee_eth.get(peak_tick, 0.0)
    if peak_fee > 0:
        y = max(attack_panel.y + 24, attack_panel.y_value(peak_fee) + 18)
        out.append(annotate(xscale.x(peak_tick) + 12, y, f"attack fee spike: {format_number(peak_fee)} ETH", "attack_tx"))

    baseline_pool_values = txpool_values_by_tick(baseline, baseline_metrics, "txpoolPendingAfterBlock", pool_metric, "normal")
    attack_pool_values = txpool_values_by_tick(attack, attack_metrics, "txpoolPendingAfterBlock", pool_metric, "attack")
    attack_queue_values = txpool_values_by_tick(attack, attack_metrics, "txpoolQueuedAfterBlock", pool_metric, "attack")
    baseline_pool = value_points(baseline_pool_values, ticks, xscale, pool_panel)
    attack_pool = value_points(attack_pool_values, ticks, xscale, pool_panel)
    attack_queue = value_points(attack_queue_values, ticks, xscale, pool_panel)
    out.append(polyline(baseline_pool, COLORS["baseline_pool"], 2.8))
    out.append(circles(baseline_pool, COLORS["baseline_pool"], 3.0))
    out.append(polyline(attack_pool, COLORS["attack_pool"], 2.8))
    out.append(circles(attack_pool, COLORS["attack_pool"], 3.0))
    out.append(polyline(attack_queue, COLORS["attack_queue"], 2.2, "6 4"))
    out.append(circles(attack_queue, COLORS["attack_queue"], 2.8))
    out.append(legend_item(720, pool_panel.y + 24, COLORS["baseline_pool"], pool_legend_label("baseline", pool_metric)))
    out.append(legend_item(720, pool_panel.y + 46, COLORS["attack_pool"], pool_legend_label("attack-run", pool_metric)))
    out.append(legend_item(720, pool_panel.y + 68, COLORS["attack_queue"], pool_legend_label("attack-run", pool_metric, queued=True), "6 4"))
    out.append(annotate(106, pool_panel.y + 24, "txpool pressure remains elevated after attack burst", "attack_pool"))

    out.append(text(604, 878, "Aligned phase tick", 13, anchor="middle", color="muted"))
    summary_y = 915
    out.append(text(96, summary_y, f"Baseline normal total: {format_number(sum_values(baseline_normal_fee_eth))} ETH", 13, 700))
    out.append(text(380, summary_y, f"Attack-run normal total: {format_number(sum_values(attack_normal_fee_eth))} ETH", 13, 700))
    out.append(text(690, summary_y, f"Adversarial total: {format_number(sum_values(attack_fee_eth))} ETH", 13, 700))
    out.append(text(96, summary_y + 24, "Read with txpool eviction metrics: fee behavior explains inclusion impact, not eviction by itself.", 12, color="muted"))
    out.append("</svg>\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")
    if derived_csv_out is not None:
        derived_csv_out.parent.mkdir(parents=True, exist_ok=True)
        write_split_fee_csv(derived_csv_out, ticks, baseline_normal_fee_eth, attack_normal_fee_eth, attack_fee_eth)


def render_baseline_only(
    baseline: list[dict[str, str]],
    markers: list[dict[str, str]],
    baseline_metrics: dict[str, str],
    baseline_values: dict[int, float],
    pool_max: float,
    ticks: list[int],
    xscale: Scale,
    out_path: Path,
    title_value: str,
    source_note: str,
    top_metric: str,
    pool_metric: str,
    derived_csv_out: Path | None,
) -> None:
    width = 1200
    height = 710
    if top_metric == "fee":
        plotted_values = scale_values(baseline_values, 100)
        top_y_max = nice_max(max_value(plotted_values, ticks))
        top_title = "Normal transaction fee"
        top_y_label = "Normal tx fee per block (10^-2 ETH)"
    else:
        plotted_values = baseline_values
        top_y_max = nice_max(max_value(plotted_values, ticks))
        top_title = "Normal transaction inclusion"
        top_y_label = "Normal txs included"

    top_panel = Panel(96, 160, 1016, 205, top_y_max, top_title, top_y_label)
    pool_panel = Panel(96, 470, 1016, 145, pool_max, pool_axis_title(pool_metric), pool_axis_label(pool_metric))

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(96, 48, title_value, 26, 800),
        text(96, 75, source_note, 13, color="muted"),
        text(96, 96, "Baseline-only preview; attack-run data has not been added to this figure.", 12, color="muted"),
    ]

    out.extend(draw_phase_labels(markers, xscale, 130))
    for panel in (top_panel, pool_panel):
        out.extend(draw_phase_bands(panel, markers, xscale))
        out.extend(draw_axes(panel, xscale, show_x_labels=(panel is pool_panel)))

    baseline_points = value_points(plotted_values, ticks, xscale, top_panel)
    out.append(polyline(baseline_points, COLORS["baseline"], 2.8))
    out.append(circles(baseline_points, COLORS["baseline"], 3.1))
    label = "normal tx fee (baseline)" if top_metric == "fee" else "normal tx included (baseline)"
    out.append(legend_item(720, top_panel.y + 24, COLORS["baseline"], label))

    baseline_pool_values = txpool_values_by_tick(baseline, baseline_metrics, "txpoolPendingAfterBlock", pool_metric, "normal")
    baseline_pool = value_points(baseline_pool_values, ticks, xscale, pool_panel)
    out.append(polyline(baseline_pool, COLORS["baseline_pool"], 2.8))
    out.append(circles(baseline_pool, COLORS["baseline_pool"], 3.0))
    out.append(legend_item(720, pool_panel.y + 24, COLORS["baseline_pool"], pool_legend_label("baseline", pool_metric)))
    annotation = "pending txpool gas rises while normal input exceeds block capacity" if pool_metric == "gas" else "pending txpool rises while normal input exceeds block capacity"
    out.append(annotate(106, pool_panel.y + 24, annotation, "baseline_pool"))

    out.append(text(604, 658, "Aligned phase tick", 13, anchor="middle", color="muted"))
    summary_y = 690
    if top_metric == "fee":
        out.append(text(96, summary_y, f"Baseline normal total: {format_number(sum_values(baseline_values))} ETH", 13, 700))
    else:
        out.append(text(96, summary_y, f"Baseline normal total included: {format_number(sum_values(baseline_values))} txs", 13, 700))
    out.append("</svg>\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")
    if derived_csv_out is not None:
        derived_csv_out.parent.mkdir(parents=True, exist_ok=True)
        if top_metric == "fee":
            write_split_fee_csv(derived_csv_out, ticks, baseline_values, {}, {})
        else:
            write_top_metric_csv(derived_csv_out, ticks, top_metric, "eth", baseline_values, {}, {})


def render(
    baseline_dir: Path,
    attack_dir: Path | None,
    out_path: Path,
    title_value: str,
    top_metric: str,
    fee_unit: str,
    fee_layout: str,
    pool_metric: str,
    derived_csv_out: Path | None,
) -> None:
    baseline = read_csv(baseline_dir / "timeseries.csv")
    baseline_metrics = metrics_row(baseline_dir)
    if attack_dir is None:
        markers = read_csv(baseline_dir / "phase_markers.csv")
        ticks = sorted({as_int(row["globalTick"]) for row in baseline})
        xscale = Scale(96, 1016, ticks)
        if top_metric == "fee":
            baseline_top_values, baseline_fee_source = fee_values_by_tick(
                baseline_dir,
                baseline,
                baseline_metrics,
                "normal_receipts.json",
                "normalIncludedInObservedBlocks",
                "normal",
                "eth",
            )
            source_note = (
                "Fee comes from receipts when present; empty receipt-scope runs use included-count based estimates."
                if baseline_fee_source == "estimated"
                else "Fee is aggregated from receipts as gasUsed x effectiveGasPrice."
            )
        else:
            baseline_top_values = {
                as_int(row["globalTick"]): float(as_int(row["normalIncludedInObservedBlocks"]))
                for row in baseline
            }
            source_note = "Top panel shows included transaction counts."
        baseline_pool_values = txpool_values_by_tick(baseline, baseline_metrics, "txpoolPendingAfterBlock", pool_metric, "normal")
        baseline_queue_values = txpool_values_by_tick(baseline, baseline_metrics, "txpoolQueuedAfterBlock", pool_metric, "normal")
        pool_max = nice_max(
            max(
                [baseline_pool_values.get(tick, 0.0) + baseline_queue_values.get(tick, 0.0) for tick in ticks]
                + [0.0]
            )
        )
        render_baseline_only(
            baseline,
            markers,
            baseline_metrics,
            baseline_top_values,
            pool_max,
            ticks,
            xscale,
            out_path,
            title_value,
            source_note,
            top_metric,
            pool_metric,
            derived_csv_out,
        )
        return

    attack = read_csv(attack_dir / "timeseries.csv")
    markers = read_csv(attack_dir / "phase_markers.csv")
    attack_metrics = metrics_row(attack_dir)

    ticks = sorted({as_int(row["globalTick"]) for row in baseline + attack})
    xscale = Scale(96, 1016, ticks)

    if top_metric == "fee":
        calculation_unit = "eth" if fee_layout == "split" else fee_unit
        baseline_top_values, baseline_fee_source = fee_values_by_tick(
            baseline_dir,
            baseline,
            baseline_metrics,
            "normal_receipts.json",
            "normalIncludedInObservedBlocks",
            "normal",
            calculation_unit,
        )
        attack_normal_top_values, attack_normal_fee_source = fee_values_by_tick(
            attack_dir,
            attack,
            attack_metrics,
            "normal_receipts.json",
            "normalIncludedInObservedBlocks",
            "normal",
            calculation_unit,
        )
        attack_top_values, attack_fee_source = fee_values_by_tick(
            attack_dir,
            attack,
            attack_metrics,
            "attack_receipts.json",
            "attackIncludedInObservedBlocks",
            "attack",
            calculation_unit,
        )
        fee_sources = {baseline_fee_source, attack_normal_fee_source, attack_fee_source}
        include_max = nice_max(
            max(
                [baseline_top_values.get(tick, 0.0) for tick in ticks]
                + [attack_normal_top_values.get(tick, 0.0) for tick in ticks]
                + [attack_top_values.get(tick, 0.0) for tick in ticks]
                + [0.0]
            )
        )
        top_title = "Tx fee per observed block window"
        top_y_label = "Tx fee per block (ETH)" if fee_unit == "eth" else "Tx fee per block (10^-2 ETH)"
        source_note = (
            "Fee comes from receipts when present; empty receipt-scope runs use included-count based estimates."
            if "estimated" in fee_sources
            else "Fee is aggregated from receipts as gasUsed x effectiveGasPrice."
        )
    else:
        baseline_top_values = {as_int(row["globalTick"]): float(as_int(row["normalIncludedInObservedBlocks"])) for row in baseline}
        attack_normal_top_values = {as_int(row["globalTick"]): float(as_int(row["normalIncludedInObservedBlocks"])) for row in attack}
        attack_top_values = {as_int(row["globalTick"]): float(as_int(row["attackIncludedInObservedBlocks"])) for row in attack}
        include_max = nice_max(max(list(baseline_top_values.values()) + list(attack_normal_top_values.values()) + list(attack_top_values.values())))
        top_title = "Block inclusion during the phased workload"
        top_y_label = "Txs included"
        source_note = "Top panel shows included transaction counts."

    baseline_pool_values = txpool_values_by_tick(baseline, baseline_metrics, "txpoolPendingAfterBlock", pool_metric, "normal")
    attack_pool_values = txpool_values_by_tick(attack, attack_metrics, "txpoolPendingAfterBlock", pool_metric, "attack")
    attack_queue_values = txpool_values_by_tick(attack, attack_metrics, "txpoolQueuedAfterBlock", pool_metric, "attack")
    pool_max = nice_max(
        max(
            [baseline_pool_values.get(tick, 0.0) for tick in ticks]
            + [attack_pool_values.get(tick, 0.0) + attack_queue_values.get(tick, 0.0) for tick in ticks]
            + [0.0]
        )
    )

    if top_metric == "fee" and fee_layout == "split":
        render_fee_split(
            baseline,
            attack,
            markers,
            baseline_metrics,
            attack_metrics,
            baseline_top_values,
            attack_normal_top_values,
            attack_top_values,
            pool_max,
            ticks,
            xscale,
            out_path,
            title_value,
            source_note,
            pool_metric,
            derived_csv_out,
        )
        return

    width = 1200
    height = 735
    top = Panel(96, 150, 1016, 220, include_max, top_title, top_y_label)
    bottom = Panel(96, 480, 1016, 190, pool_max, pool_axis_title(pool_metric), pool_axis_label(pool_metric))

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(96, 48, title_value, 26, 800),
        text(
            96,
            75,
            source_note,
            13,
            color="muted",
        ),
        text(96, 96, "Aligned phase tick; each tick corresponds to one observed block window in the phased workload.", 12, color="muted"),
    ]

    out.extend(draw_phase_labels(markers, xscale, 128))
    for panel in (top, bottom):
        out.extend(draw_phase_bands(panel, markers, xscale))
        out.extend(draw_axes(panel, xscale, show_x_labels=(panel is bottom)))

    # Top panel.
    out.extend(draw_value_bars(attack_top_values, ticks, xscale, top, COLORS["attack_tx"]))
    baseline_points = value_points(baseline_top_values, ticks, xscale, top)
    attack_normal_points = value_points(attack_normal_top_values, ticks, xscale, top)
    out.append(polyline(baseline_points, COLORS["baseline"], 2.8))
    out.append(circles(baseline_points, COLORS["baseline"], 3.1))
    out.append(polyline(attack_normal_points, COLORS["attack_normal"], 2.8))
    out.append(circles(attack_normal_points, COLORS["attack_normal"], 3.1))
    if top_metric == "fee":
        out.append(legend_item(720, 118, COLORS["baseline"], "normal tx fee (baseline)"))
        out.append(legend_item(720, 140, COLORS["attack_normal"], "normal tx fee (attack run)"))
        out.append(legend_item(720, 162, COLORS["attack_tx"], "adversarial tx fee (attack run)"))
        out.append(annotate(xscale.x(7) + 12, top.y_value(include_max * 0.17), "normal tx fee collapses", "attack_normal"))
        out.append(annotate(xscale.x(7) + 12, top.y_value(include_max * 0.30), "adversarial fee appears", "attack_tx"))
    else:
        out.append(legend_item(720, 118, COLORS["baseline"], "baseline normal included"))
        out.append(legend_item(720, 140, COLORS["attack_normal"], "attack-run normal included"))
        out.append(legend_item(720, 162, COLORS["attack_tx"], "attack tx included as bars"))
        out.append(annotate(xscale.x(7) + 12, top.y_value(85), "normal inclusion stalls", "attack_normal"))
        out.append(annotate(xscale.x(7) + 12, top.y_value(160), "125 attack tx included", "attack_tx"))

    # Bottom panel.
    baseline_pool = value_points(baseline_pool_values, ticks, xscale, bottom)
    attack_pool = value_points(attack_pool_values, ticks, xscale, bottom)
    attack_queue = value_points(attack_queue_values, ticks, xscale, bottom)
    out.append(polyline(baseline_pool, COLORS["baseline_pool"], 2.8))
    out.append(circles(baseline_pool, COLORS["baseline_pool"], 3.0))
    out.append(polyline(attack_pool, COLORS["attack_pool"], 2.8))
    out.append(circles(attack_pool, COLORS["attack_pool"], 3.0))
    out.append(polyline(attack_queue, COLORS["attack_queue"], 2.2, "6 4"))
    out.append(circles(attack_queue, COLORS["attack_queue"], 2.8))
    out.append(legend_item(720, 448, COLORS["baseline_pool"], pool_legend_label("baseline", pool_metric)))
    out.append(legend_item(720, 470, COLORS["attack_pool"], pool_legend_label("attack-run", pool_metric)))
    out.append(legend_item(720, 492, COLORS["attack_queue"], pool_legend_label("attack-run", pool_metric, queued=True), "6 4"))
    out.append(annotate(xscale.x(8) + 16, bottom.y_value(pool_max * 0.64), "txpool pressure jumps during attack burst", "attack_pool"))
    out.append(annotate(xscale.x(14), bottom.y_value(pool_max * 0.06), "baseline drains normally", "baseline_pool"))

    out.append(text(604, 718, "Aligned phase tick", 13, anchor="middle", color="muted"))
    out.append("</svg>\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")
    if derived_csv_out is not None:
        derived_csv_out.parent.mkdir(parents=True, exist_ok=True)
        write_top_metric_csv(derived_csv_out, ticks, top_metric, fee_unit, baseline_top_values, attack_normal_top_values, attack_top_values)


def main() -> int:
    args = parse_args()
    derived_csv = Path(args.derived_csv_out) if args.derived_csv_out else Path(args.out).with_suffix(".csv")
    attack_dir = Path(args.attack_dir) if args.attack_dir else None
    render(
        Path(args.baseline_dir),
        attack_dir,
        Path(args.out),
        args.title,
        args.top_metric,
        args.fee_unit,
        args.fee_layout,
        args.pool_metric,
        derived_csv,
    )
    print(args.out)
    print(derived_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
