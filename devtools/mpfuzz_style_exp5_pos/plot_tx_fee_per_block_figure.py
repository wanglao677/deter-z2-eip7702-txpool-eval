#!/usr/bin/env python3
"""Render a paper-style tx-fee-per-block SVG from Deter-Z2 run receipts.

The plotted fee is the actual on-chain fee paid by included transactions:

    tx fee = gasUsed * effectiveGasPrice

Failed transactions with receipts are included because they still pay gas.
"""

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
    "axis": "#27272a",
    "grid": "#d4d4d8",
    "text": "#18181b",
    "muted": "#52525b",
    "phase": "#f8fafc",
    "attack_phase": "#fff1f2",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot Deter-Z2 tx fee per block from receipt JSON files.")
    parser.add_argument("--baseline-dir", required=True, help="Baseline run directory.")
    parser.add_argument("--attack-dir", default=None, help="Attack run directory. If omitted, plot baseline only.")
    parser.add_argument("--out", required=True, help="Output SVG path.")
    parser.add_argument("--csv-out", default=None, help="Optional derived fee-per-block CSV path.")
    parser.add_argument("--title", default="Tx Fee per Block for Deter-Z2")
    parser.add_argument(
        "--x-mode",
        choices=["window", "block", "phase-tick"],
        default="window",
        help="window aligns each run by workload start; block uses actual block numbers; phase-tick maps phased runs to globalTick.",
    )
    parser.add_argument(
        "--unit",
        choices=["eth", "centieth"],
        default="eth",
        help="Combined-layout unit: ETH per block or 10^-2 ETH per block.",
    )
    parser.add_argument(
        "--layout",
        choices=["split", "combined"],
        default="split",
        help="split uses separate normal/adversarial panels; combined overlays all series on one axis.",
    )
    return parser.parse_args()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_receipt_json(path: Path, default: Any) -> Any:
    receipts = read_json(path, default)
    if receipts:
        return receipts
    rpc_path = path.with_name(path.stem + "_rpc" + path.suffix)
    return read_json(rpc_path, default)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, int):
        return value
    value = str(value).strip()
    if value.startswith("0x"):
        return int(value, 16)
    return int(value)


def fee_wei(receipt: dict[str, Any]) -> int:
    gas_used = as_int(receipt.get("gasUsed"))
    gas_price = as_int(receipt.get("effectiveGasPrice") or receipt.get("gasPrice"))
    return gas_used * gas_price


def receipts_by_block(path: Path) -> dict[int, int]:
    receipts = read_receipt_json(path, {})
    fees: dict[int, int] = defaultdict(int)
    for receipt in receipts.values():
        if not isinstance(receipt, dict):
            continue
        block_number = as_int(receipt.get("blockNumber"))
        if block_number <= 0:
            continue
        fees[block_number] += fee_wei(receipt)
    return dict(fees)


def summary_start_block(run_dir: Path, series: list[dict[int, int]]) -> int | None:
    summary = read_json(run_dir / "summary.json", {})
    nonempty_blocks = sorted(block for data in series for block in data)
    start = summary.get("executionWindowStartBlock")
    if start is not None:
        candidate = as_int(start) + 1
        if nonempty_blocks and nonempty_blocks[0] < candidate:
            return nonempty_blocks[0]
        return candidate
    if nonempty_blocks:
        return nonempty_blocks[0]
    return None


def phase_tick_map(run_dir: Path) -> dict[int, int]:
    rows = read_csv(run_dir / "timeseries.csv")
    mapping: dict[int, int] = {}
    for row in rows:
        tick = as_int(row.get("globalTick"))
        start_block = as_int(row.get("startBlock"))
        target_block = as_int(row.get("targetBlock") or row.get("blockNumber"))
        if tick <= 0:
            continue
        for block_number in range(start_block + 1, target_block + 1):
            mapping[block_number] = tick
        observed_block = as_int(row.get("blockNumber"))
        if observed_block > 0:
            mapping[observed_block] = tick
    return mapping


def align_series(run_dir: Path, by_block: dict[int, int], x_mode: str, sibling_series: list[dict[int, int]]) -> dict[int, int]:
    if x_mode == "block":
        return by_block
    if x_mode == "phase-tick":
        mapping = phase_tick_map(run_dir)
        if not mapping:
            raise RuntimeError(f"{run_dir} does not contain a usable timeseries.csv for --x-mode phase-tick")
        aligned: dict[int, int] = defaultdict(int)
        for block_number, fee in by_block.items():
            tick = mapping.get(block_number)
            if tick is not None:
                aligned[tick] += fee
        return dict(aligned)

    start_block = summary_start_block(run_dir, sibling_series + [by_block])
    if start_block is None:
        return {}
    return {block_number - start_block + 1: fee for block_number, fee in by_block.items() if block_number >= start_block}


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


def convert_fee(value_wei: int, unit: str) -> float:
    eth = value_wei / ETHER
    if unit == "centieth":
        return eth * 100
    return eth


class Scale:
    def __init__(self, left: float, width: float, min_x: int, max_x: int):
        self.left = left
        self.width = width
        self.min_x = min_x
        self.max_x = max_x
        self.span = max(1, max_x - min_x)

    def x(self, value: int | float) -> float:
        return self.left + ((value - self.min_x) / self.span) * self.width


class Panel:
    def __init__(self, x: float, y: float, width: float, height: float, y_max: float):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.y_max = max(1e-12, y_max)

    def y_value(self, value: float) -> float:
        return self.y + self.height - (value / self.y_max) * self.height


def text(x: float, y: float, value: str, size: int = 13, weight: int = 400, anchor: str = "start", color: str = "text") -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" font-size="{size}" '
        f'font-weight="{weight}" fill="{COLORS[color]}">{html.escape(value)}</text>'
    )


def marker(x: float, y: float, color: str, shape: str) -> str:
    if shape == "triangle":
        points = [(x, y - 5), (x - 5, y + 4), (x + 5, y + 4)]
        rendered = " ".join(f"{px:.2f},{py:.2f}" for px, py in points)
        return f'<polygon points="{rendered}" fill="{color}"/>'
    if shape == "cross":
        return (
            f'<line x1="{x - 5:.2f}" y1="{y - 5:.2f}" x2="{x + 5:.2f}" y2="{y + 5:.2f}" '
            f'stroke="{color}" stroke-width="2"/>'
            f'<line x1="{x - 5:.2f}" y1="{y + 5:.2f}" x2="{x + 5:.2f}" y2="{y - 5:.2f}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
    return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4.2" fill="{color}"/>'


def draw_series(points: list[tuple[float, float]], color: str, shape: str, dash: str = "") -> list[str]:
    if not points:
        return []
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    out = [
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.7" '
        f'stroke-linejoin="round" stroke-linecap="round"{dash_attr}/>'
    ]
    out.extend(marker(x, y, color, shape) for x, y in points)
    return out


def legend_item(x: float, y: float, color: str, label: str, shape: str, dash: str = "") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    icon = marker(x + 12, y, color, shape)
    return (
        f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x + 24:.2f}" y2="{y:.2f}" stroke="{color}" '
        f'stroke-width="2.7" stroke-linecap="round"{dash_attr}/>'
        f'{icon}'
        f'<text x="{x + 34:.2f}" y="{y + 4:.2f}" font-size="12" fill="{COLORS["text"]}">{html.escape(label)}</text>'
    )


def draw_axes(panel: Panel, xscale: Scale, y_label: str, x_label: str, show_x_ticks: bool = True, show_x_label: bool = True) -> list[str]:
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

    span = max(1, xscale.max_x - xscale.min_x)
    tick_step = max(1, math.ceil(span / 12))
    for x_value in range(xscale.min_x, xscale.max_x + 1):
        x = xscale.x(x_value)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y + panel.height:.2f}" x2="{x:.2f}" '
            f'y2="{panel.y + panel.height + 5:.2f}" stroke="{COLORS["axis"]}" stroke-width="1"/>'
        )
        if show_x_ticks and (x_value == xscale.min_x or x_value == xscale.max_x or (x_value - xscale.min_x) % tick_step == 0):
            out.append(text(x, panel.y + panel.height + 22, str(x_value), 12, anchor="middle", color="muted"))

    out.append(
        f'<text x="{panel.x - 58:.2f}" y="{panel.y + panel.height / 2:.2f}" text-anchor="middle" '
        f'font-size="13" fill="{COLORS["muted"]}" transform="rotate(-90 {panel.x - 58:.2f} {panel.y + panel.height / 2:.2f})">'
        f'{html.escape(y_label)}</text>'
    )
    if show_x_label:
        out.append(text(panel.x + panel.width / 2, panel.y + panel.height + 54, x_label, 13, anchor="middle", color="muted"))
    return out


def draw_phase_bands(panel: Panel, markers: list[dict[str, str]], xscale: Scale) -> list[str]:
    out: list[str] = []
    for marker_row in markers:
        phase = marker_row["phase"]
        x1 = xscale.x(as_int(marker_row["startGlobalTick"]) - 0.5)
        x2 = xscale.x(as_int(marker_row["endGlobalTick"]) + 0.5)
        fill = COLORS["attack_phase"] if phase == "attack_on" else COLORS["phase"]
        opacity = "0.95" if phase == "attack_on" else "0.45"
        out.append(
            f'<rect x="{x1:.2f}" y="{panel.y:.2f}" width="{x2 - x1:.2f}" height="{panel.height:.2f}" '
            f'fill="{fill}" opacity="{opacity}"/>'
        )
        out.append(
            f'<line x1="{x1:.2f}" y1="{panel.y:.2f}" x2="{x1:.2f}" y2="{panel.y + panel.height:.2f}" '
            f'stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
        label = "Attack on" if phase == "attack_on" else phase.replace("_", " ").title()
        out.append(text((x1 + x2) / 2, panel.y - 22, label, 12, 700, anchor="middle", color="muted"))
    if markers:
        x = xscale.x(as_int(markers[-1]["endGlobalTick"]) + 0.5)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y:.2f}" x2="{x:.2f}" y2="{panel.y + panel.height:.2f}" '
            f'stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
    return out


def to_points(values_by_x: dict[int, int], xscale: Scale, panel: Panel, unit: str) -> list[tuple[float, float]]:
    points = []
    for x_value in range(xscale.min_x, xscale.max_x + 1):
        fee = convert_fee(values_by_x.get(x_value, 0), unit)
        points.append((xscale.x(x_value), panel.y_value(fee)))
    return points


def total_fee(values_by_x: dict[int, int]) -> int:
    return sum(values_by_x.values())


def max_fee(series: list[dict[int, int]], unit: str) -> float:
    values = [convert_fee(fee, unit) for values_by_x in series for fee in values_by_x.values()]
    return max(values) if values else 0.0


def annotate(x: float, y: float, label: str, color: str = "muted", anchor: str = "start") -> str:
    return text(x, y, label, 12, 700, anchor=anchor, color=color)


def write_derived_csv(path: Path, xs: list[int], series: dict[str, dict[int, int]], unit: str) -> None:
    fieldnames = ["x"]
    for name in series:
        fieldnames.extend([f"{name}FeeWei", f"{name}Fee{unit.title()}"])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for x_value in xs:
            row: dict[str, Any] = {"x": x_value}
            for name, values in series.items():
                fee = values.get(x_value, 0)
                row[f"{name}FeeWei"] = fee
                row[f"{name}Fee{unit.title()}"] = f"{convert_fee(fee, unit):.12f}"
            writer.writerow(row)


def render_split(
    args: argparse.Namespace,
    baseline_normal: dict[int, int],
    attack_normal: dict[int, int],
    attack_tx: dict[int, int],
    xs: list[int],
    x_label: str,
    attack_dir: Path | None,
) -> None:
    if not attack_dir:
        raise RuntimeError("--layout split requires --attack-dir so normal and adversarial fees can be separated")

    min_x, max_x = min(xs), max(xs)
    xscale = Scale(98, 1000, min_x, max_x)
    normal_unit = "centieth"
    attack_unit = "eth"
    normal_y_max = nice_max(max_fee([baseline_normal, attack_normal], normal_unit))
    attack_y_max = nice_max(max_fee([attack_tx], attack_unit))

    normal_panel = Panel(98, 150, 1000, 245, normal_y_max)
    attack_panel = Panel(98, 500, 1000, 170, attack_y_max)
    width = 1180
    height = 820
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(98, 48, args.title, 26, 800),
        text(98, 76, "Fee is aggregated from transaction receipts as gasUsed x effectiveGasPrice.", 13, color="muted"),
        text(98, 112, "Normal transaction fee", 15, 700),
    ]

    if args.x_mode == "phase-tick" and attack_dir:
        out.extend(draw_phase_bands(normal_panel, read_csv(attack_dir / "phase_markers.csv"), xscale))
    out.extend(draw_axes(normal_panel, xscale, "Normal tx fee per block (10^-2 ETH)", x_label, show_x_ticks=False, show_x_label=False))
    out.extend(draw_series(to_points(baseline_normal, xscale, normal_panel, normal_unit), COLORS["baseline"], "circle"))
    out.extend(draw_series(to_points(attack_normal, xscale, normal_panel, normal_unit), COLORS["attack_normal"], "triangle"))
    out.append(legend_item(710, 112, COLORS["baseline"], "Normal tx fee (baseline)", "circle"))
    out.append(legend_item(710, 136, COLORS["attack_normal"], "Normal tx fee (attack run)", "triangle"))
    out.append(annotate(xscale.x(min_x) + 10, normal_panel.y_value(convert_fee(total_fee({min_x: attack_normal.get(min_x, 0)}), normal_unit)) + 28, "attack-run normal fee stays at 0", "attack_normal"))

    out.append(text(98, 462, "Adversarial transaction fee", 15, 700))
    if args.x_mode == "phase-tick" and attack_dir:
        out.extend(draw_phase_bands(attack_panel, read_csv(attack_dir / "phase_markers.csv"), xscale))
    out.extend(draw_axes(attack_panel, xscale, "Adversarial tx fee per block (ETH)", x_label))
    out.extend(draw_series(to_points(attack_tx, xscale, attack_panel, attack_unit), COLORS["attack_tx"], "cross"))
    out.append(legend_item(710, 462, COLORS["attack_tx"], "Adversarial tx fee (attack run)", "cross"))
    attack_peak_tick = max(attack_tx, key=lambda key: attack_tx[key]) if attack_tx else min_x
    attack_peak = convert_fee(attack_tx.get(attack_peak_tick, 0), attack_unit)
    if attack_peak > 0:
        out.append(annotate(xscale.x(attack_peak_tick) + 14, attack_panel.y_value(attack_peak) + 18, f"attack fee spike: {format_number(attack_peak)} ETH", "attack_tx"))

    summary_y = 765
    out.append(text(98, summary_y, f"Baseline normal total: {format_number(convert_fee(total_fee(baseline_normal), 'eth'))} ETH", 13, 700))
    out.append(text(380, summary_y, f"Attack-run normal total: {format_number(convert_fee(total_fee(attack_normal), 'eth'))} ETH", 13, 700))
    out.append(text(690, summary_y, f"Adversarial total: {format_number(convert_fee(total_fee(attack_tx), 'eth'))} ETH", 13, 700))
    out.append(text(98, summary_y + 24, "Read with txpool eviction metrics: fee behavior explains inclusion impact, not eviction by itself.", 12, color="muted"))
    out.append("</svg>\n")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")

    csv_out = Path(args.csv_out) if args.csv_out else out_path.with_suffix(".csv")
    write_derived_csv(
        csv_out,
        list(range(min_x, max_x + 1)),
        {"baselineNormal": baseline_normal, "attackRunNormal": attack_normal, "attackTx": attack_tx},
        "eth",
    )
    print(f"svg={out_path}")
    print(f"csv={csv_out}")


def render(args: argparse.Namespace) -> None:
    baseline_dir = Path(args.baseline_dir)
    attack_dir = Path(args.attack_dir) if args.attack_dir else None

    baseline_normal_raw = receipts_by_block(baseline_dir / "normal_receipts.json")
    attack_normal_raw: dict[int, int] = {}
    attack_tx_raw: dict[int, int] = {}
    if attack_dir:
        attack_normal_raw = receipts_by_block(attack_dir / "normal_receipts.json")
        attack_tx_raw = receipts_by_block(attack_dir / "attack_receipts.json")

    baseline_normal = align_series(baseline_dir, baseline_normal_raw, args.x_mode, [])
    attack_normal: dict[int, int] = {}
    attack_tx: dict[int, int] = {}
    if attack_dir:
        attack_normal = align_series(attack_dir, attack_normal_raw, args.x_mode, [attack_tx_raw])
        attack_tx = align_series(attack_dir, attack_tx_raw, args.x_mode, [attack_normal_raw])

    series = {
        "baselineNormal": baseline_normal,
        "attackRunNormal": attack_normal,
        "attackTx": attack_tx,
    }
    if not attack_dir:
        series = {"baselineNormal": baseline_normal}

    xs = sorted({x_value for values in series.values() for x_value in values})
    if not xs:
        raise RuntimeError("no receipt-backed fee data found; rerun the experiment with receipt collection enabled")
    min_x, max_x = min(xs), max(xs)
    if args.layout == "split":
        render_split(args, baseline_normal, attack_normal, attack_tx, xs, x_label_from_mode(args.x_mode), attack_dir)
        return

    xscale = Scale(98, 1000, min_x, max_x)

    converted_values = [convert_fee(fee, args.unit) for values in series.values() for fee in values.values()]
    y_max = nice_max(max(converted_values) if converted_values else 0)
    panel = Panel(98, 150, 1000, 360, y_max)

    y_label = "Tx fee per block (ETH)" if args.unit == "eth" else "Tx fee per block (10^-2 ETH)"
    x_label = x_label_from_mode(args.x_mode)

    width = 1180
    height = 650
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(98, 48, args.title, 26, 800),
        text(98, 76, "Fee is aggregated from transaction receipts as gasUsed x effectiveGasPrice.", 13, color="muted"),
    ]

    if args.x_mode == "phase-tick" and attack_dir:
        out.extend(draw_phase_bands(panel, read_csv(attack_dir / "phase_markers.csv"), xscale))
    out.extend(draw_axes(panel, xscale, y_label, x_label))

    out.extend(draw_series(to_points(baseline_normal, xscale, panel, args.unit), COLORS["baseline"], "circle"))
    if attack_dir:
        out.extend(draw_series(to_points(attack_normal, xscale, panel, args.unit), COLORS["attack_normal"], "triangle"))
        out.extend(draw_series(to_points(attack_tx, xscale, panel, args.unit), COLORS["attack_tx"], "cross"))

    legend_y = 112
    out.append(legend_item(690, legend_y, COLORS["baseline"], "Normal tx fee (baseline)", "circle"))
    if attack_dir:
        out.append(legend_item(690, legend_y + 24, COLORS["attack_normal"], "Normal tx fee (attack run)", "triangle"))
        out.append(legend_item(690, legend_y + 48, COLORS["attack_tx"], "Adversarial tx fee (attack run)", "cross"))

    summary_y = 585
    out.append(text(98, summary_y, f"Baseline normal total: {format_number(convert_fee(total_fee(baseline_normal), args.unit))} {args.unit}", 13, 700))
    if attack_dir:
        out.append(text(380, summary_y, f"Attack-run normal total: {format_number(convert_fee(total_fee(attack_normal), args.unit))} {args.unit}", 13, 700))
        out.append(text(690, summary_y, f"Adversarial total: {format_number(convert_fee(total_fee(attack_tx), args.unit))} {args.unit}", 13, 700))
    out.append(text(98, summary_y + 24, "Use this fee figure together with txpool eviction metrics; fee alone does not prove eviction.", 12, color="muted"))
    out.append("</svg>\n")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")

    csv_out = Path(args.csv_out) if args.csv_out else out_path.with_suffix(".csv")
    write_derived_csv(csv_out, list(range(min_x, max_x + 1)), series, args.unit)
    print(f"svg={out_path}")
    print(f"csv={csv_out}")


def x_label_from_mode(x_mode: str) -> str:
    if x_mode == "block":
        return "Block height"
    if x_mode == "phase-tick":
        return "Aligned phase tick"
    return "Block index after workload start"


def main() -> int:
    args = parse_args()
    render(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
