#!/usr/bin/env python3
"""Plot normal-transaction gas price distribution for a phased run."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
from pathlib import Path
from typing import Any


GWEI = 10**9

COLORS = {
    "axis": "#27272a",
    "grid": "#d4d4d8",
    "text": "#18181b",
    "muted": "#52525b",
    "bar": "#2563eb",
    "bar_cap": "#dc2626",
    "box": "#93c5fd",
    "box_edge": "#1d4ed8",
    "median": "#dc2626",
    "whisker": "#0f766e",
    "phase": "#f8fafc",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot sampled normal gas price distribution.")
    parser.add_argument("--run-dir", required=True, help="Phased run directory containing normal_records.jsonl.")
    parser.add_argument("--out", required=True, help="Output SVG path.")
    parser.add_argument("--title", default="Normal Gas Price Distribution")
    parser.add_argument("--bins", type=int, default=20)
    parser.add_argument("--derived-csv-out", default=None)
    return parser.parse_args()


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    value = str(value).strip()
    return int(value, 16) if value.startswith("0x") else int(value)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_metrics(run_dir: Path) -> dict[str, str]:
    path = run_dir / "metrics.csv"
    if not path.exists():
        return {}
    rows = read_csv(path)
    return rows[0] if rows else {}


def read_records(run_dir: Path) -> list[dict[str, Any]]:
    records = []
    with (run_dir / "normal_records.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


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
    return f"{value:.2f}".rstrip("0").rstrip(".")


def text(x: float, y: float, value: str, size: int = 13, weight: int = 400, anchor: str = "start", color: str = "text") -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" font-size="{size}" '
        f'font-weight="{weight}" fill="{COLORS[color]}">{html.escape(value)}</text>'
    )


class LinearScale:
    def __init__(self, start: float, end: float, domain_min: float, domain_max: float):
        self.start = start
        self.end = end
        self.domain_min = domain_min
        self.domain_max = domain_max
        self.span = max(1e-12, domain_max - domain_min)

    def value(self, raw: float) -> float:
        return self.start + ((raw - self.domain_min) / self.span) * (self.end - self.start)


class YScale:
    def __init__(self, top: float, height: float, max_value: float):
        self.top = top
        self.height = height
        self.max_value = max(1e-12, max_value)

    def value(self, raw: float) -> float:
        return self.top + self.height - (raw / self.max_value) * self.height


def draw_axes(x: float, y: float, width: float, height: float, y_max: float, y_label: str, title: str) -> list[str]:
    out = [
        f'<rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" fill="white" stroke="{COLORS["axis"]}" stroke-width="1"/>',
        text(x, y - 14, title, 15, 700),
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1):
        yy = y + height - frac * height
        out.append(f'<line x1="{x:.2f}" y1="{yy:.2f}" x2="{x + width:.2f}" y2="{yy:.2f}" stroke="{COLORS["grid"]}" stroke-width="1"/>')
        out.append(text(x - 10, yy + 4, format_number(frac * y_max), 12, anchor="end", color="muted"))
    out.append(
        f'<text x="{x - 58:.2f}" y="{y + height / 2:.2f}" text-anchor="middle" font-size="12" '
        f'fill="{COLORS["muted"]}" transform="rotate(-90 {x - 58:.2f} {y + height / 2:.2f})">{html.escape(y_label)}</text>'
    )
    return out


def histogram(values: list[float], bins: int, min_value: float, max_value: float) -> list[tuple[float, float, int]]:
    width = (max_value - min_value) / bins
    counts = [0 for _ in range(bins)]
    for value in values:
        index = bins - 1 if value >= max_value else int((value - min_value) / width)
        index = max(0, min(bins - 1, index))
        counts[index] += 1
    return [(min_value + index * width, min_value + (index + 1) * width, count) for index, count in enumerate(counts)]


def tick_stats(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_tick: dict[int, list[float]] = {}
    phases: dict[int, str] = {}
    for record in records:
        tick = as_int(record.get("phaseTick")) + 1
        gas = as_int(record["gasPriceWei"]) / GWEI
        by_tick.setdefault(tick, []).append(gas)
        phases[tick] = str(record.get("phase", ""))
    rows = []
    for tick in sorted(by_tick):
        values = by_tick[tick]
        rows.append(
            {
                "globalTick": tick,
                "phase": phases.get(tick, ""),
                "count": len(values),
                "minGwei": min(values),
                "p25Gwei": percentile(values, 0.25),
                "medianGwei": percentile(values, 0.50),
                "p75Gwei": percentile(values, 0.75),
                "maxGwei": max(values),
                "avgGwei": sum(values) / len(values),
            }
        )
    return rows


def write_stats_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["globalTick", "phase", "count", "minGwei", "p25Gwei", "medianGwei", "p75Gwei", "maxGwei", "avgGwei"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: f"{row[key]:.9f}" if key.endswith("Gwei") else row[key]
                    for key in fieldnames
                }
            )


def render(run_dir: Path, out_path: Path, title_value: str, bins: int, derived_csv_out: Path) -> None:
    metrics = read_metrics(run_dir)
    records = [record for record in read_records(run_dir) if record.get("gasPriceWei") and not record.get("error")]
    if not records:
        raise RuntimeError(f"no normal gas price records found in {run_dir}")

    values = [as_int(record["gasPriceWei"]) / GWEI for record in records]
    min_gwei = min(values)
    max_gwei = max(values)
    median_gwei = percentile(values, 0.5)
    avg_gwei = sum(values) / len(values)
    floor_gwei = as_int(metrics.get("normalFeeFloorWei")) / GWEI if metrics.get("normalFeeFloorWei") else min_gwei
    cap_gwei = as_int(metrics.get("normalFeeCapWei")) / GWEI if metrics.get("normalFeeCapWei") else max_gwei
    cap_count = sum(1 for value in values if abs(value - cap_gwei) < 1e-9)
    floor_count = sum(1 for value in values if abs(value - floor_gwei) < 1e-9)
    stats_by_tick = tick_stats(records)
    write_stats_csv(derived_csv_out, stats_by_tick)

    hist = histogram(values, bins, min_gwei, max_gwei)
    hist_y_max = nice_max(max(count for _, _, count in hist))
    stat_y_max = nice_max(max(row["maxGwei"] for row in stats_by_tick))

    width = 1200
    height = 760
    left = 96
    panel_width = 1016
    hist_top = 155
    hist_height = 215
    tick_top = 485
    tick_height = 165
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(left, 48, title_value, 26, 800),
        text(left, 75, "Normal gas prices are sampled from mainnet fee history, then scaled, jittered, floored, capped, and assigned by the configured strategy.", 12, color="muted"),
        text(left, 96, f"count={len(values)}; min={format_number(min_gwei)} gwei; median={format_number(median_gwei)} gwei; avg={format_number(avg_gwei)} gwei; max={format_number(max_gwei)} gwei", 12, color="muted"),
        text(left, 117, f"floor hits={floor_count}; cap hits={cap_count}; strategy={metrics.get('normalFeeSamplingStrategy', '')}", 12, color="muted"),
    ]

    out.extend(draw_axes(left, hist_top, panel_width, hist_height, hist_y_max, "Transactions", "Overall normal gas price distribution"))
    hist_x = LinearScale(left, left + panel_width, min_gwei, max_gwei)
    hist_y = YScale(hist_top, hist_height, hist_y_max)
    for low, high, count in hist:
        x1 = hist_x.value(low)
        x2 = hist_x.value(high)
        yy = hist_y.value(count)
        color = COLORS["bar_cap"] if high >= cap_gwei else COLORS["bar"]
        out.append(
            f'<rect x="{x1 + 2:.2f}" y="{yy:.2f}" width="{max(1, x2 - x1 - 4):.2f}" height="{hist_top + hist_height - yy:.2f}" '
            f'fill="{color}" opacity="0.78"/>'
        )
    for label_value in (min_gwei, median_gwei, cap_gwei):
        x = hist_x.value(label_value)
        out.append(f'<line x1="{x:.2f}" y1="{hist_top:.2f}" x2="{x:.2f}" y2="{hist_top + hist_height:.2f}" stroke="#71717a" stroke-width="1" stroke-dasharray="4 4"/>')
        out.append(text(x, hist_top + hist_height + 20, f"{format_number(label_value)}", 12, anchor="middle", color="muted"))
    out.append(text(left + panel_width / 2, hist_top + hist_height + 42, "Gas price (gwei)", 13, anchor="middle", color="muted"))
    out.append(text(left + panel_width - 250, hist_top + 24, "blue: sampled prices", 12, color="muted"))
    out.append(text(left + panel_width - 250, hist_top + 45, "red: cap bucket", 12, color="muted"))

    out.extend(draw_axes(left, tick_top, panel_width, tick_height, stat_y_max, "Gas price (gwei)", "Per-phase-tick gas price spread"))
    ticks = [row["globalTick"] for row in stats_by_tick]
    xscale = LinearScale(left, left + panel_width, min(ticks), max(ticks))
    yscale = YScale(tick_top, tick_height, stat_y_max)
    step = panel_width / max(1, max(ticks) - min(ticks))
    box_width = min(34, step * 0.45)
    for row in stats_by_tick:
        tick = row["globalTick"]
        x = xscale.value(tick)
        y_min = yscale.value(row["minGwei"])
        y_p25 = yscale.value(row["p25Gwei"])
        y_med = yscale.value(row["medianGwei"])
        y_p75 = yscale.value(row["p75Gwei"])
        y_max = yscale.value(row["maxGwei"])
        out.append(f'<line x1="{x:.2f}" y1="{y_min:.2f}" x2="{x:.2f}" y2="{y_max:.2f}" stroke="{COLORS["whisker"]}" stroke-width="2"/>')
        out.append(f'<rect x="{x - box_width / 2:.2f}" y="{y_p75:.2f}" width="{box_width:.2f}" height="{max(1, y_p25 - y_p75):.2f}" fill="{COLORS["box"]}" stroke="{COLORS["box_edge"]}" stroke-width="1.3" opacity="0.9"/>')
        out.append(f'<line x1="{x - box_width / 2:.2f}" y1="{y_med:.2f}" x2="{x + box_width / 2:.2f}" y2="{y_med:.2f}" stroke="{COLORS["median"]}" stroke-width="2.4"/>')
        out.append(f'<circle cx="{x:.2f}" cy="{yscale.value(row["avgGwei"]):.2f}" r="2.8" fill="#111827"/>')
        if tick == min(ticks) or tick == max(ticks) or tick % 2 == 0:
            out.append(text(x, tick_top + tick_height + 21, str(tick), 12, anchor="middle", color="muted"))
    out.append(text(left + panel_width / 2, tick_top + tick_height + 43, "Aligned phase tick", 13, anchor="middle", color="muted"))
    out.append(text(left + panel_width - 330, tick_top + 24, "box: p25-p75; red: median; dot: mean", 12, color="muted"))
    out.append(text(left, 725, f"Output data: {derived_csv_out}", 12, color="muted"))
    out.append("</svg>\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")


def main() -> int:
    args = parse_args()
    out_path = Path(args.out)
    derived_csv = Path(args.derived_csv_out) if args.derived_csv_out else out_path.with_suffix(".csv")
    render(Path(args.run_dir), out_path, args.title, args.bins, derived_csv)
    print(out_path)
    print(derived_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
