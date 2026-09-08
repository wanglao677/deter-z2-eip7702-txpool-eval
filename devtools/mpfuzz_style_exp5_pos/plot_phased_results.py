#!/usr/bin/env python3
"""Render a dependency-free SVG comparison for phased Deter-Z2 runs."""

from __future__ import annotations

import argparse
import csv
import html
import math
from pathlib import Path


PALETTE = {
    "baseline": "#2563eb",
    "attack_normal": "#dc2626",
    "attack_tx": "#f97316",
    "baseline_pending": "#0f766e",
    "attack_pending": "#7c3aed",
    "attack_queued": "#9333ea",
    "baseline_backlog": "#0891b2",
    "attack_backlog": "#be123c",
    "grid": "#d4d4d8",
    "axis": "#27272a",
    "text": "#18181b",
    "muted": "#71717a",
    "band_even": "#fafafa",
    "band_odd": "#f4f4f5",
    "attack_band": "#fff1f2",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot phased Deter-Z2 timeseries CSVs as SVG.")
    parser.add_argument("--baseline-dir", required=True, help="Directory containing baseline timeseries.csv.")
    parser.add_argument("--attack-dir", required=True, help="Directory containing attack timeseries.csv.")
    parser.add_argument("--out", required=True, help="Output SVG path.")
    parser.add_argument("--title", default="Deter-Z2 Phased Evaluation on Besu")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_int(value: str | None) -> int:
    if value is None or value == "":
        return 0
    value = value.strip()
    if value.startswith("0x"):
        return int(value, 16)
    return int(value)


def polyline(points: list[tuple[float, float]], color: str, width: float = 2.4, dash: str = "") -> str:
    if not points:
        return ""
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<polyline points="{coords}" fill="none" stroke="{color}" '
        f'stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"{dash_attr}/>'
    )


def circles(points: list[tuple[float, float]], color: str, radius: float = 3.4) -> str:
    return "\n".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius}" fill="{color}" />' for x, y in points
    )


def nice_max(value: int) -> int:
    if value <= 0:
        return 1
    exponent = math.floor(math.log10(value))
    base = 10**exponent
    for step in (1, 2, 5, 10):
        candidate = step * base
        if candidate >= value:
            return candidate
    return 10 * base


class Panel:
    def __init__(self, x: float, y: float, width: float, height: float, y_max: int, label: str):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.y_max = max(1, y_max)
        self.label = label

    def y_of(self, value: int) -> float:
        return self.y + self.height - (value / self.y_max) * self.height


def x_scale(ticks: list[int], left: float, width: float):
    min_tick = min(ticks)
    max_tick = max(ticks)
    span = max(1, max_tick - min_tick)

    def x_of(tick: int | float) -> float:
        return left + ((tick - min_tick) / span) * width

    return x_of, min_tick, max_tick


def draw_axes(panel: Panel, x_of, min_tick: int, max_tick: int) -> list[str]:
    out: list[str] = []
    out.append(
        f'<rect x="{panel.x}" y="{panel.y}" width="{panel.width}" height="{panel.height}" '
        f'fill="white" stroke="{PALETTE["axis"]}" stroke-width="1"/>'
    )
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = panel.y + panel.height - frac * panel.height
        value = round(frac * panel.y_max)
        out.append(
            f'<line x1="{panel.x}" y1="{y:.2f}" x2="{panel.x + panel.width}" y2="{y:.2f}" '
            f'stroke="{PALETTE["grid"]}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{panel.x - 10}" y="{y + 4:.2f}" text-anchor="end" '
            f'font-size="12" fill="{PALETTE["muted"]}">{value}</text>'
        )
    for tick in range(min_tick, max_tick + 1):
        x = x_of(tick)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y + panel.height}" x2="{x:.2f}" '
            f'y2="{panel.y + panel.height + 5}" stroke="{PALETTE["axis"]}" stroke-width="1"/>'
        )
        if tick == min_tick or tick == max_tick or tick % 2 == 0:
            out.append(
                f'<text x="{x:.2f}" y="{panel.y + panel.height + 20}" text-anchor="middle" '
                f'font-size="11" fill="{PALETTE["muted"]}">{tick}</text>'
            )
    out.append(
        f'<text x="{panel.x}" y="{panel.y - 12}" font-size="15" font-weight="700" '
        f'fill="{PALETTE["text"]}">{html.escape(panel.label)}</text>'
    )
    return out


def draw_phase_bands(panel: Panel, markers: list[dict[str, str]], x_of) -> list[str]:
    out: list[str] = []
    for i, marker in enumerate(markers):
        start = as_int(marker["startGlobalTick"]) - 0.5
        end = as_int(marker["endGlobalTick"]) + 0.5
        phase = marker["phase"]
        color = PALETTE["attack_band"] if phase == "attack_on" else (
            PALETTE["band_even"] if i % 2 == 0 else PALETTE["band_odd"]
        )
        x1 = x_of(start)
        x2 = x_of(end)
        out.append(
            f'<rect x="{x1:.2f}" y="{panel.y}" width="{x2 - x1:.2f}" height="{panel.height}" '
            f'fill="{color}" opacity="0.72"/>'
        )
        out.append(
            f'<line x1="{x1:.2f}" y1="{panel.y}" x2="{x1:.2f}" y2="{panel.y + panel.height}" '
            f'stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
    if markers:
        final_x = x_of(as_int(markers[-1]["endGlobalTick"]) + 0.5)
        out.append(
            f'<line x1="{final_x:.2f}" y1="{panel.y}" x2="{final_x:.2f}" '
            f'y2="{panel.y + panel.height}" stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
    return out


def draw_phase_labels(top_y: float, markers: list[dict[str, str]], x_of) -> list[str]:
    out: list[str] = []
    for marker in markers:
        start = as_int(marker["startGlobalTick"]) - 0.5
        end = as_int(marker["endGlobalTick"]) + 0.5
        x = (x_of(start) + x_of(end)) / 2
        label = "attack-on" if marker["phase"] == "attack_on" else marker["phase"]
        out.append(
            f'<text x="{x:.2f}" y="{top_y:.2f}" text-anchor="middle" '
            f'font-size="12" font-weight="700" fill="{PALETTE["muted"]}">{html.escape(label)}</text>'
        )
    return out


def legend_item(x: float, y: float, color: str, label: str, dash: str = "") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x}" y1="{y}" x2="{x + 24}" y2="{y}" stroke="{color}" '
        f'stroke-width="3" stroke-linecap="round"{dash_attr}/>'
        f'<text x="{x + 32}" y="{y + 4}" font-size="13" fill="{PALETTE["text"]}">'
        f'{html.escape(label)}</text>'
    )


def series(rows: list[dict[str, str]], field: str) -> list[tuple[int, int]]:
    return [(as_int(row["globalTick"]), as_int(row[field])) for row in rows]


def points_for(rows: list[dict[str, str]], field: str, x_of, panel: Panel) -> list[tuple[float, float]]:
    return [(x_of(tick), panel.y_of(value)) for tick, value in series(rows, field)]


def render(baseline_dir: Path, attack_dir: Path, out_path: Path, title: str) -> None:
    baseline = read_csv(baseline_dir / "timeseries.csv")
    attack = read_csv(attack_dir / "timeseries.csv")
    markers = read_csv(attack_dir / "phase_markers.csv")

    ticks = sorted({as_int(row["globalTick"]) for row in baseline + attack})
    x_of, min_tick, max_tick = x_scale(ticks, 92, 1080)

    include_max = nice_max(
        max(
            [as_int(row["normalIncludedInObservedBlocks"]) for row in baseline + attack]
            + [as_int(row["attackIncludedInObservedBlocks"]) for row in attack]
        )
    )
    txpool_max = nice_max(
        max(
            [as_int(row["txpoolPendingAfterBlock"]) + as_int(row["txpoolQueuedAfterBlock"]) for row in baseline + attack]
        )
    )
    backlog_max = nice_max(
        max([as_int(row["normalBacklog"]) for row in baseline + attack] + [as_int(row["attackBacklog"]) for row in attack])
    )

    width = 1280
    height = 930
    panels = [
        Panel(92, 150, 1080, 190, include_max, "Included transactions per observed block window"),
        Panel(92, 430, 1080, 190, txpool_max, "Transaction-pool pressure after block"),
        Panel(92, 710, 1080, 150, backlog_max, "Accepted but not yet included backlog"),
    ]

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;} .small{font-size:12px;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="92" y="48" font-size="25" font-weight="800" fill="{PALETTE["text"]}">{html.escape(title)}</text>',
        (
            f'<text x="92" y="76" font-size="13" fill="{PALETTE["muted"]}">'
            f'Baseline: {html.escape(baseline_dir.name)} | Attack: {html.escape(attack_dir.name)}</text>'
        ),
        (
            f'<text x="92" y="98" font-size="13" fill="{PALETTE["muted"]}">'
            "x-axis uses aligned phase tick, not absolute chain block height, so baseline and attack phases are directly comparable.</text>"
        ),
    ]

    out.extend(draw_phase_labels(126, markers, x_of))

    for panel in panels:
        out.extend(draw_phase_bands(panel, markers, x_of))
        out.extend(draw_axes(panel, x_of, min_tick, max_tick))

    # Panel 1: inclusion.
    p = panels[0]
    out.append(polyline(points_for(baseline, "normalIncludedInObservedBlocks", x_of, p), PALETTE["baseline"], 2.6))
    out.append(circles(points_for(baseline, "normalIncludedInObservedBlocks", x_of, p), PALETTE["baseline"]))
    out.append(polyline(points_for(attack, "normalIncludedInObservedBlocks", x_of, p), PALETTE["attack_normal"], 2.6))
    out.append(circles(points_for(attack, "normalIncludedInObservedBlocks", x_of, p), PALETTE["attack_normal"]))
    out.append(polyline(points_for(attack, "attackIncludedInObservedBlocks", x_of, p), PALETTE["attack_tx"], 2.4, "6 4"))
    out.append(circles(points_for(attack, "attackIncludedInObservedBlocks", x_of, p), PALETTE["attack_tx"], 3.0))
    out.append(legend_item(815, 118, PALETTE["baseline"], "baseline normal included"))
    out.append(legend_item(815, 140, PALETTE["attack_normal"], "attack-run normal included"))
    out.append(legend_item(815, 162, PALETTE["attack_tx"], "attack tx included", "6 4"))

    # Panel 2: txpool pressure.
    p = panels[1]
    out.append(polyline(points_for(baseline, "txpoolPendingAfterBlock", x_of, p), PALETTE["baseline_pending"], 2.6))
    out.append(circles(points_for(baseline, "txpoolPendingAfterBlock", x_of, p), PALETTE["baseline_pending"]))
    out.append(polyline(points_for(attack, "txpoolPendingAfterBlock", x_of, p), PALETTE["attack_pending"], 2.6))
    out.append(circles(points_for(attack, "txpoolPendingAfterBlock", x_of, p), PALETTE["attack_pending"]))
    out.append(polyline(points_for(attack, "txpoolQueuedAfterBlock", x_of, p), PALETTE["attack_queued"], 2.2, "5 4"))
    out.append(circles(points_for(attack, "txpoolQueuedAfterBlock", x_of, p), PALETTE["attack_queued"], 3.0))
    out.append(legend_item(815, 398, PALETTE["baseline_pending"], "baseline txpool pending"))
    out.append(legend_item(815, 420, PALETTE["attack_pending"], "attack-run txpool pending"))
    out.append(legend_item(815, 442, PALETTE["attack_queued"], "attack-run txpool queued", "5 4"))

    # Panel 3: backlog.
    p = panels[2]
    out.append(polyline(points_for(baseline, "normalBacklog", x_of, p), PALETTE["baseline_backlog"], 2.6))
    out.append(circles(points_for(baseline, "normalBacklog", x_of, p), PALETTE["baseline_backlog"]))
    out.append(polyline(points_for(attack, "normalBacklog", x_of, p), PALETTE["attack_backlog"], 2.6))
    out.append(circles(points_for(attack, "normalBacklog", x_of, p), PALETTE["attack_backlog"]))
    out.append(polyline(points_for(attack, "attackBacklog", x_of, p), PALETTE["attack_tx"], 2.2, "6 4"))
    out.append(circles(points_for(attack, "attackBacklog", x_of, p), PALETTE["attack_tx"], 3.0))
    out.append(legend_item(815, 678, PALETTE["baseline_backlog"], "baseline normal backlog"))
    out.append(legend_item(815, 700, PALETTE["attack_backlog"], "attack-run normal backlog"))
    out.append(legend_item(815, 722, PALETTE["attack_tx"], "attack backlog", "6 4"))

    out.append(
        f'<text x="632" y="910" text-anchor="middle" font-size="13" fill="{PALETTE["muted"]}">'
        "Aligned phase tick</text>"
    )
    out.append("</svg>\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")


def main() -> int:
    args = parse_args()
    render(Path(args.baseline_dir), Path(args.attack_dir), Path(args.out), args.title)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
