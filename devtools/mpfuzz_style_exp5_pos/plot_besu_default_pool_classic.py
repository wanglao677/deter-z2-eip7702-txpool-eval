#!/usr/bin/env python3
"""Draw the three core figures for a default-pool Besu paired experiment."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ETHER = 10**18

COLORS = {
    "baseline": "#2563eb",
    "attack_normal": "#dc2626",
    "attack_tx": "#ea580c",
    "baseline_pool": "#0f766e",
    "attack_pool": "#7c3aed",
    "attack_queue": "#a855f7",
    "included": "#16a34a",
    "absent": "#dc2626",
    "queued": "#f59e0b",
    "pending": "#2563eb",
    "unknown": "#9ca3af",
    "axis": "#27272a",
    "grid": "#d4d4d8",
    "text": "#18181b",
    "muted": "#52525b",
    "phase": "#f8fafc",
    "attack_phase": "#fff1f2",
}

PHASE_LABELS = {
    "warmup": "Warmup",
    "saturation": "Saturation",
    "control": "Control",
    "attack_on": "Attack on",
    "recovery": "Recovery",
    "drain": "Drain",
}

STATE_LABELS = {
    "INCLUDED_SUCCESS": "Included success",
    "INCLUDED_FAILED": "Included failed",
    "ABSENT_NO_RECEIPT_CONFIRMED": "Confirmed absent / no receipt",
    "STILL_QUEUED_AT_FINAL_AUDIT": "Still queued",
    "STILL_PENDING_AT_FINAL_AUDIT": "Still pending",
    "UNKNOWN_NEEDS_MORE_OBSERVATIONS": "Needs more observations",
}

STATE_COLORS = {
    "INCLUDED_SUCCESS": COLORS["included"],
    "INCLUDED_FAILED": "#7f1d1d",
    "ABSENT_NO_RECEIPT_CONFIRMED": COLORS["absent"],
    "STILL_QUEUED_AT_FINAL_AUDIT": COLORS["queued"],
    "STILL_PENDING_AT_FINAL_AUDIT": COLORS["pending"],
    "UNKNOWN_NEEDS_MORE_OBSERVATIONS": COLORS["unknown"],
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def as_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, int):
        return value
    text_value = str(value).strip()
    return int(text_value, 16) if text_value.startswith("0x") else int(text_value)


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


def fmt(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    if abs(value) >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def text(
    x: float,
    y: float,
    value: Any,
    size: int = 13,
    weight: int = 400,
    anchor: str = "start",
    color: str = "text",
) -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" font-size="{size}" '
        f'font-weight="{weight}" fill="{COLORS[color]}">{esc(value)}</text>'
    )


def rect(
    x: float,
    y: float,
    width: float,
    height: float,
    fill: str,
    stroke: str = "none",
    opacity: float = 1.0,
    radius: float = 0,
) -> str:
    return (
        f'<rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" '
        f'fill="{fill}" stroke="{stroke}" opacity="{opacity:.2f}" rx="{radius:.2f}"/>'
    )


def line(x1: float, y1: float, x2: float, y2: float, color: str = "axis", width: float = 1.0, dash: str = "") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{COLORS[color]}" stroke-width="{width:.2f}"{dash_attr}/>'
    )


def polyline(points: list[tuple[float, float]], color: str, width: float = 2.8, dash: str = "") -> str:
    if not points:
        return ""
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return (
        f'<polyline points="{coords}" fill="none" stroke="{COLORS[color]}" stroke-width="{width:.2f}" '
        f'stroke-linejoin="round" stroke-linecap="round"{dash_attr}/>'
    )


def circles(points: list[tuple[float, float]], color: str, radius: float = 3.1) -> str:
    return "\n".join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius}" fill="{COLORS[color]}"/>' for x, y in points)


def shaped_marker(x: float, y: float, color: str, shape: str, size: float = 4.8) -> str:
    if shape == "triangle":
        points = [(x, y - size), (x - size, y + size * 0.85), (x + size, y + size * 0.85)]
        rendered = " ".join(f"{px:.2f},{py:.2f}" for px, py in points)
        return f'<polygon points="{rendered}" fill="{color}"/>'
    if shape == "cross":
        return (
            f'<line x1="{x - size:.2f}" y1="{y - size:.2f}" x2="{x + size:.2f}" y2="{y + size:.2f}" '
            f'stroke="{color}" stroke-width="2"/>'
            f'<line x1="{x - size:.2f}" y1="{y + size:.2f}" x2="{x + size:.2f}" y2="{y - size:.2f}" '
            f'stroke="{color}" stroke-width="2"/>'
        )
    return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{size:.2f}" fill="{color}"/>'


def marker_line(points: list[tuple[float, float]], color_key: str, shape: str) -> list[str]:
    if not points:
        return []
    color = COLORS[color_key]
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    out = [
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.7" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
    ]
    out.extend(shaped_marker(x, y, color, shape) for x, y in points)
    return out


class TickScale:
    def __init__(self, left: float, width: float, ticks: list[int]) -> None:
        self.left = left
        self.width = width
        self.min_tick = min(ticks)
        self.max_tick = max(ticks)
        self.count = max(1, self.max_tick - self.min_tick + 1)
        self.step = width / self.count

    def x(self, tick: int | float) -> float:
        return self.left + ((tick - self.min_tick) + 0.5) * self.step

    def band_start(self, tick: int | float) -> float:
        return self.left + (tick - self.min_tick) * self.step


class Panel:
    def __init__(self, x: float, y: float, width: float, height: float, y_max: float, title: str, y_label: str) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.y_max = max(1e-12, y_max)
        self.title = title
        self.y_label = y_label

    def y_value(self, value: float) -> float:
        return self.y + self.height - (value / self.y_max) * self.height


class DualPanel:
    def __init__(self, x: float, y: float, width: float, height: float, left_max: float, right_max: float) -> None:
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


def value_points(values: dict[int, float], ticks: list[int], xscale: TickScale, panel: Panel) -> list[tuple[float, float]]:
    return [(xscale.x(tick), panel.y_value(values.get(tick, 0.0))) for tick in ticks]


def draw_axes(panel: Panel, xscale: TickScale, show_x_labels: bool) -> list[str]:
    out = [
        rect(panel.x, panel.y, panel.width, panel.height, "#ffffff", COLORS["axis"]),
        text(panel.x, panel.y - 14, panel.title, 15, 700),
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = panel.y + panel.height - frac * panel.height
        out.append(line(panel.x, y, panel.x + panel.width, y, "grid"))
        out.append(text(panel.x - 10, y + 4, fmt(frac * panel.y_max), 12, 400, "end", "muted"))
    for tick in range(xscale.min_tick, xscale.max_tick + 1):
        x = xscale.x(tick)
        out.append(line(x, panel.y + panel.height, x, panel.y + panel.height + 5))
        if show_x_labels and (tick == xscale.min_tick or tick == xscale.max_tick or tick % 2 == 0):
            out.append(text(x, panel.y + panel.height + 21, tick, 12, 400, "middle", "muted"))
    out.append(
        f'<text x="{panel.x - 62:.2f}" y="{panel.y + panel.height / 2:.2f}" text-anchor="middle" '
        f'font-size="12" fill="{COLORS["muted"]}" transform="rotate(-90 {panel.x - 62:.2f} {panel.y + panel.height / 2:.2f})">'
        f'{esc(panel.y_label)}</text>'
    )
    return out


def draw_phase_bands(panel: Panel, markers: list[dict[str, str]], xscale: TickScale) -> list[str]:
    out: list[str] = []
    for marker in markers:
        phase = marker["phase"]
        start = as_int(marker["startGlobalTick"])
        end = as_int(marker["endGlobalTick"])
        x1 = xscale.band_start(start)
        x2 = xscale.band_start(end + 1)
        fill = COLORS["attack_phase"] if phase == "attack_on" else COLORS["phase"]
        opacity = 0.92 if phase == "attack_on" else 0.45
        out.append(rect(x1, panel.y, x2 - x1, panel.height, fill, opacity=opacity))
        out.append(line(x1, panel.y, x1, panel.y + panel.height, "muted", dash="4 4"))
    if markers:
        x = xscale.band_start(as_int(markers[-1]["endGlobalTick"]) + 1)
        out.append(line(x, panel.y, x, panel.y + panel.height, "muted", dash="4 4"))
    return out


def draw_phase_labels(markers: list[dict[str, str]], xscale: TickScale, y: float) -> list[str]:
    out: list[str] = []
    for marker in markers:
        start = as_int(marker["startGlobalTick"])
        end = as_int(marker["endGlobalTick"])
        x1 = xscale.band_start(start)
        x2 = xscale.band_start(end + 1)
        out.append(text((x1 + x2) / 2, y, PHASE_LABELS.get(marker["phase"], marker["phase"]), 12, 700, "middle", "muted"))
    return out


def legend_item(x: float, y: float, color: str, label: str, dash: str = "") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x + 26:.2f}" y2="{y:.2f}" '
        f'stroke="{COLORS[color]}" stroke-width="3" stroke-linecap="round"{dash_attr}/>'
        f'<text x="{x + 34:.2f}" y="{y + 4:.2f}" font-size="12" fill="{COLORS["text"]}">{esc(label)}</text>'
    )


def shape_legend_item(x: float, y: float, color_key: str, label: str, shape: str) -> str:
    color = COLORS[color_key]
    return (
        f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x + 28:.2f}" y2="{y:.2f}" '
        f'stroke="{color}" stroke-width="2.7" stroke-linecap="round"/>'
        f'{shaped_marker(x + 14, y, color, shape)}'
        f'<text x="{x + 38:.2f}" y="{y + 4:.2f}" font-size="12" fill="{COLORS["text"]}">{esc(label)}</text>'
    )


def marker_item(x: float, y: float, fill: str, label: str) -> str:
    return rect(x, y - 12, 14, 14, fill, radius=2) + text(x + 22, y, label, 12)


def bar_values(values: dict[int, float], ticks: list[int], xscale: TickScale, panel: Panel, color: str) -> list[str]:
    out: list[str] = []
    width = xscale.step * 0.42
    for tick in ticks:
        value = values.get(tick, 0.0)
        if value <= 0:
            continue
        x = xscale.x(tick) - width / 2
        y = panel.y_value(value)
        out.append(rect(x, y, width, panel.y + panel.height - y, COLORS[color], opacity=0.74))
    return out


def phase_by_tick(rows: list[dict[str, str]]) -> dict[int, str]:
    return {as_int(row["globalTick"]): row.get("phase", "") for row in rows}


def included_counts(rows: list[dict[str, str]], group: str) -> Counter[int]:
    out: Counter[int] = Counter()
    for row in rows:
        if row.get("group") == group and row.get("receiptFound") == "1":
            tick = as_int(row.get("includedGlobalTick"))
            if tick > 0:
                out[tick] += 1
    return out


def fee_by_tick(rows: list[dict[str, str]], group: str) -> defaultdict[int, float]:
    out: defaultdict[int, float] = defaultdict(float)
    for row in rows:
        if row.get("group") == group and row.get("receiptFound") == "1":
            tick = as_int(row.get("includedGlobalTick"))
            if tick > 0:
                out[tick] += as_int(row.get("feeWei")) / ETHER
    return out


def txpool_values(rows: list[dict[str, str]], field: str) -> dict[int, float]:
    return {as_int(row["globalTick"]): float(as_int(row.get(field))) for row in rows}


def state_order() -> list[str]:
    return [
        "INCLUDED_SUCCESS",
        "ABSENT_NO_RECEIPT_CONFIRMED",
        "STILL_QUEUED_AT_FINAL_AUDIT",
        "STILL_PENDING_AT_FINAL_AUDIT",
        "INCLUDED_FAILED",
        "UNKNOWN_NEEDS_MORE_OBSERVATIONS",
    ]


def normal_finality_counts(pair_dir: Path) -> tuple[Counter[str], Counter[str], dict[str, Counter[str]], dict[str, Any]]:
    baseline_summary = read_json(pair_dir / "baseline" / "tx_final_status_summary.json")
    attack_status = read_csv(pair_dir / "attack" / "tx_final_status.csv")
    finality = read_json(pair_dir / "attack" / "normal_finality_summary.json")

    baseline_counts = Counter(baseline_summary["byGroup"]["normal"]["states"])
    attack_counts: Counter[str] = Counter()
    phase_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in attack_status:
        if row.get("group") != "normal":
            continue
        phase = row.get("submittedPhase") or "unknown"
        if row.get("receiptFound") == "1":
            attack_counts["INCLUDED_SUCCESS"] += 1
            phase_counts[phase]["INCLUDED_SUCCESS"] += 1
    for state, count in finality.get("states", {}).items():
        attack_counts[state] += int(count)
    for phase, states in finality.get("submittedPhaseStates", {}).items():
        for state, count in states.items():
            phase_counts[phase][state] += int(count)
    return baseline_counts, attack_counts, phase_counts, finality


def draw_stacked_bar(label: str, counts: Counter[str], total: int, x: float, y: float, width: float, height: float) -> list[str]:
    out = [text(78, y + 27, label, 14, 700), text(260, y + 27, f"n={total}", 12, 400, "end", "muted")]
    cursor = x
    for state in state_order():
        count = counts.get(state, 0)
        if count <= 0:
            continue
        seg = width * count / max(1, total)
        out.append(rect(cursor, y, seg, height, STATE_COLORS[state], radius=4))
        if seg >= 54:
            out.append(text(cursor + seg / 2, y + height / 2 + 5, count, 12, 700, "middle"))
        else:
            out.append(text(cursor + seg + 5, y + height / 2 + 5, count, 11, 700, "start", "muted"))
        cursor += seg
    return out


def draw_phase_state_bars(phase_counts: dict[str, Counter[str]], x: float, y: float, width: float, height: float) -> list[str]:
    phases = ["warmup", "saturation", "attack_on", "recovery"]
    max_total = max([sum(phase_counts[phase].values()) for phase in phases] + [1])
    slot = width / len(phases)
    bar_width = min(118, slot * 0.58)
    out = [
        rect(x, y, width, height, "#ffffff", COLORS["axis"]),
        text(x, y - 16, "Attack-run normal final state by submission phase", 15, 700),
    ]
    for step in range(5):
        value = max_total * step / 4
        yy = y + height - height * step / 4
        out.append(line(x, yy, x + width, yy, "grid"))
        out.append(text(x - 10, yy + 4, f"{value:.0f}", 11, 400, "end", "muted"))
    out.append(
        f'<text x="{x - 54:.2f}" y="{y + height / 2:.2f}" text-anchor="middle" font-size="12" '
        f'fill="{COLORS["muted"]}" transform="rotate(-90 {x - 54:.2f} {y + height / 2:.2f})">Transactions</text>'
    )
    for idx, phase in enumerate(phases):
        center = x + idx * slot + slot / 2
        if phase == "attack_on":
            out.append(rect(center - slot / 2 + 4, y, slot - 8, height, COLORS["attack_phase"], opacity=0.72))
        cursor = y + height
        for state in state_order():
            count = phase_counts[phase].get(state, 0)
            if count <= 0:
                continue
            seg_h = height * count / max_total
            cursor -= seg_h
            out.append(rect(center - bar_width / 2, cursor, bar_width, seg_h, STATE_COLORS[state], "#ffffff", radius=2))
            if seg_h >= 18:
                out.append(text(center, cursor + seg_h / 2 + 5, count, 12, 700, "middle"))
        out.append(text(center, y + height + 25, PHASE_LABELS[phase], 12, 700, "middle", "muted"))
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def draw_figure1(pair_dir: Path, out_dir: Path) -> None:
    baseline_ts = read_csv(pair_dir / "baseline" / "timeseries.csv")
    attack_ts = read_csv(pair_dir / "attack" / "timeseries.csv")
    markers = read_csv(pair_dir / "attack" / "phase_markers.csv")
    baseline_status = read_csv(pair_dir / "baseline" / "tx_final_status.csv")
    attack_status = read_csv(pair_dir / "attack" / "tx_final_status.csv")
    ticks = sorted({as_int(row["globalTick"]) for row in baseline_ts + attack_ts})
    xscale = TickScale(96, 1016, ticks)
    baseline_normal = defaultdict(float, included_counts(baseline_status, "normal"))
    attack_normal = defaultdict(float, included_counts(attack_status, "normal"))
    attack_tx = defaultdict(float, included_counts(attack_status, "attack"))
    include_max = nice_max(max([baseline_normal[t] for t in ticks] + [attack_normal[t] for t in ticks] + [attack_tx[t] for t in ticks] + [0]))
    baseline_pool = txpool_values(baseline_ts, "txpoolPendingAfterBlock")
    attack_pool = txpool_values(attack_ts, "txpoolPendingAfterBlock")
    attack_queue = txpool_values(attack_ts, "txpoolQueuedAfterBlock")
    pool_max = nice_max(max([baseline_pool.get(t, 0) for t in ticks] + [attack_pool.get(t, 0) + attack_queue.get(t, 0) for t in ticks] + [0]))

    top = Panel(96, 165, 1016, 225, include_max, "Block inclusion during the phased workload", "Txs included")
    bottom = Panel(96, 505, 1016, 205, pool_max, "Transaction-pool pressure after each block", "Txs in txpool")
    out: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="835" viewBox="0 0 1200 835">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        rect(0, 0, 1200, 835, "#ffffff"),
        text(96, 48, "Besu default-pool v3 attack", 26, 800),
        text(96, 75, "Counts come from audited transaction receipts; txpool pressure comes from sampled txpool_status.", 13, 400, "start", "muted"),
        text(96, 96, "Baseline and attack runs use the same normal workload; attack run injects 16,000 EIP-7702 txs per block for four blocks.", 12, 400, "start", "muted"),
    ]
    out.extend(draw_phase_labels(markers, xscale, 130))
    for panel in (top, bottom):
        out.extend(draw_phase_bands(panel, markers, xscale))
        out.extend(draw_axes(panel, xscale, panel is bottom))
    out.extend(bar_values(attack_tx, ticks, xscale, top, "attack_tx"))
    baseline_points = value_points(baseline_normal, ticks, xscale, top)
    attack_points = value_points(attack_normal, ticks, xscale, top)
    out.append(polyline(baseline_points, "baseline"))
    out.append(circles(baseline_points, "baseline"))
    out.append(polyline(attack_points, "attack_normal"))
    out.append(circles(attack_points, "attack_normal"))
    out.append(legend_item(710, 185, "baseline", "normal included (baseline)"))
    out.append(legend_item(710, 208, "attack_normal", "normal included (attack run)"))
    out.append(legend_item(710, 231, "attack_tx", "attack tx included as bars"))
    out.append(text(xscale.x(7) + 12, top.y_value(include_max * 0.16), "normal inclusion drops to 0 during attack_on", 12, 700, "start", "attack_normal"))
    out.append(text(xscale.x(8), top.y_value(include_max * 0.56), f"{int(sum(attack_tx.values()))} attack txs included", 12, 700, "middle", "attack_tx"))

    for values, color, dash in ((baseline_pool, "baseline_pool", ""), (attack_pool, "attack_pool", ""), (attack_queue, "attack_queue", "6 4")):
        points = value_points(values, ticks, xscale, bottom)
        out.append(polyline(points, color, 2.8, dash))
        out.append(circles(points, color, 3.0))
    out.append(legend_item(710, 526, "baseline_pool", "baseline pending txpool"))
    out.append(legend_item(710, 549, "attack_pool", "attack-run pending txpool"))
    out.append(legend_item(710, 572, "attack_queue", "attack-run queued txpool", "6 4"))
    out.append(text(106, bottom.y + 29, "txpool pressure remains after the attack burst", 12, 700, "start", "attack_pool"))
    out.append(text(604, 755, "Aligned phase tick", 13, 400, "middle", "muted"))
    out.append(text(96, 792, f"Baseline normal included: {int(sum(baseline_normal.values())):,} txs", 13, 700))
    out.append(text(390, 792, f"Attack-run normal included: {int(sum(attack_normal.values())):,} txs", 13, 700))
    out.append(text(710, 792, f"Attack included: {int(sum(attack_tx.values())):,} txs", 13, 700))
    out.append("</svg>\n")
    svg_path = out_dir / "图1_上链与交易池压力.svg"
    svg_path.write_text("\n".join(out), encoding="utf-8")
    write_csv(
        out_dir / "图1_上链与交易池压力.csv",
        [
            {
                "globalTick": tick,
                "phase": phase_by_tick(attack_ts).get(tick, ""),
                "baselineNormalIncluded": int(baseline_normal.get(tick, 0)),
                "attackRunNormalIncluded": int(attack_normal.get(tick, 0)),
                "attackTxIncluded": int(attack_tx.get(tick, 0)),
                "baselinePendingTxpool": int(baseline_pool.get(tick, 0)),
                "attackRunPendingTxpool": int(attack_pool.get(tick, 0)),
                "attackRunQueuedTxpool": int(attack_queue.get(tick, 0)),
            }
            for tick in ticks
        ],
    )


def draw_figure2(pair_dir: Path, out_dir: Path) -> None:
    baseline_counts, attack_counts, phase_counts, finality = normal_finality_counts(pair_dir)
    baseline_total = sum(baseline_counts.values())
    attack_total = sum(attack_counts.values())
    out: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1180" height="760" viewBox="0 0 1180 760">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        rect(0, 0, 1180, 760, "#ffffff"),
        text(78, 50, "Besu default-pool v3 normal finality", 26, 800),
        text(78, 78, "Previously unresolved normal txs are re-audited across fresh blocks using receipt lookup plus txpool_content.", 13, 400, "start", "muted"),
    ]
    out.append(marker_item(78, 120, STATE_COLORS["INCLUDED_SUCCESS"], STATE_LABELS["INCLUDED_SUCCESS"]))
    out.append(marker_item(260, 120, STATE_COLORS["ABSENT_NO_RECEIPT_CONFIRMED"], STATE_LABELS["ABSENT_NO_RECEIPT_CONFIRMED"]))
    out.append(marker_item(520, 120, STATE_COLORS["STILL_QUEUED_AT_FINAL_AUDIT"], STATE_LABELS["STILL_QUEUED_AT_FINAL_AUDIT"]))
    out.append(marker_item(690, 120, STATE_COLORS["STILL_PENDING_AT_FINAL_AUDIT"], STATE_LABELS["STILL_PENDING_AT_FINAL_AUDIT"]))
    out.append(text(78, 170, "Overall normal-transaction outcome", 15, 700))
    out.extend(draw_stacked_bar("Baseline normal", baseline_counts, baseline_total, 292, 200, 760, 42))
    out.extend(draw_stacked_bar("Attack-run normal", attack_counts, attack_total, 292, 278, 760, 42))
    included = attack_counts.get("INCLUDED_SUCCESS", 0)
    absent = attack_counts.get("ABSENT_NO_RECEIPT_CONFIRMED", 0)
    queued = attack_counts.get("STILL_QUEUED_AT_FINAL_AUDIT", 0)
    out.append(rect(78, 354, 974, 58, "#f9fafb", "#e5e7eb", radius=6))
    out.append(text(100, 388, f"Key result: {included} normal txs included, {absent} confirmed absent/no receipt, {queued} still queued.", 14, 700))
    out.append(text(100, 408, "Confirmed-absent normal txs were submitted during attack_on; queued txs were submitted during recovery.", 12, 400, "start", "muted"))
    out.extend(draw_phase_state_bars(phase_counts, 128, 470, 924, 190))
    out.append(text(590, 720, f"Finality audit window: blocks {finality.get('startBlock')} to {finality.get('endBlock')}; confirmation threshold: {finality.get('confirmAbsentObservations')} absent observations.", 12, 400, "middle", "muted"))
    out.append("</svg>\n")
    (out_dir / "图2_normal最终状态复核.svg").write_text("\n".join(out), encoding="utf-8")


def draw_figure3(pair_dir: Path, out_dir: Path) -> None:
    baseline_ts = read_csv(pair_dir / "baseline" / "timeseries.csv")
    attack_ts = read_csv(pair_dir / "attack" / "timeseries.csv")
    markers = read_csv(pair_dir / "attack" / "phase_markers.csv")
    baseline_status = read_csv(pair_dir / "baseline" / "tx_final_status.csv")
    attack_status = read_csv(pair_dir / "attack" / "tx_final_status.csv")
    ticks = sorted({as_int(row["globalTick"]) for row in baseline_ts + attack_ts})
    xscale = TickScale(96, 1016, ticks)
    baseline_normal_fee = dict(fee_by_tick(baseline_status, "normal"))
    attack_normal_fee = dict(fee_by_tick(attack_status, "normal"))
    attack_fee = dict(fee_by_tick(attack_status, "attack"))
    baseline_normal_centi = {tick: baseline_normal_fee.get(tick, 0.0) * 100 for tick in ticks}
    attack_normal_centi = {tick: attack_normal_fee.get(tick, 0.0) * 100 for tick in ticks}
    normal_fee_max = nice_max(max([baseline_normal_centi.get(t, 0) for t in ticks] + [attack_normal_centi.get(t, 0) for t in ticks] + [0]))
    attack_fee_max = nice_max(max([attack_fee.get(t, 0) for t in ticks] + [0]))
    attack_pool = txpool_values(attack_ts, "txpoolPendingAfterBlock")
    attack_queue = txpool_values(attack_ts, "txpoolQueuedAfterBlock")
    baseline_pool = txpool_values(baseline_ts, "txpoolPendingAfterBlock")
    pool_max = nice_max(max([baseline_pool.get(t, 0) for t in ticks] + [attack_pool.get(t, 0) + attack_queue.get(t, 0) for t in ticks] + [0]))

    normal_panel = Panel(96, 165, 1016, 190, normal_fee_max, "Normal transaction fee", "Normal tx fee per block (10^-2 ETH)")
    attack_panel = Panel(96, 435, 1016, 150, attack_fee_max, "Adversarial transaction fee", "Adversarial tx fee per block (ETH)")
    pool_panel = Panel(96, 680, 1016, 155, pool_max, "Transaction-pool pressure after each block", "Txs in txpool")
    out: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="950" viewBox="0 0 1200 950">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        rect(0, 0, 1200, 950, "#ffffff"),
        text(96, 48, "Besu default-pool v3 fees", 26, 800),
        text(96, 75, "Fee is receipt-backed: gasUsed x effectiveGasPrice from tx_final_status.csv.", 13, 400, "start", "muted"),
        text(96, 96, "Aligned phase tick; each tick corresponds to one observed block window in the phased workload.", 12, 400, "start", "muted"),
    ]
    out.extend(draw_phase_labels(markers, xscale, 130))
    for panel in (normal_panel, attack_panel, pool_panel):
        out.extend(draw_phase_bands(panel, markers, xscale))
        out.extend(draw_axes(panel, xscale, panel is pool_panel))
    for values, color in ((baseline_normal_centi, "baseline"), (attack_normal_centi, "attack_normal")):
        points = value_points(values, ticks, xscale, normal_panel)
        out.append(polyline(points, color))
        out.append(circles(points, color))
    out.append(legend_item(720, normal_panel.y + 24, "baseline", "normal tx fee (baseline)"))
    out.append(legend_item(720, normal_panel.y + 47, "attack_normal", "normal tx fee (attack run)"))
    out.append(text(106, normal_panel.y + normal_panel.height - 16, "normal fee collapses during attack_on", 12, 700, "start", "attack_normal"))
    attack_points = value_points(attack_fee, ticks, xscale, attack_panel)
    out.append(polyline(attack_points, "attack_tx"))
    out.append(circles(attack_points, "attack_tx"))
    out.append(legend_item(720, attack_panel.y + 24, "attack_tx", "adversarial tx fee (attack run)"))
    peak_tick = max(ticks, key=lambda tick: attack_fee.get(tick, 0.0))
    peak_fee = attack_fee.get(peak_tick, 0.0)
    if peak_fee > 0:
        out.append(text(xscale.x(peak_tick) + 12, max(attack_panel.y + 26, attack_panel.y_value(peak_fee) + 18), f"attack fee peak: {fmt(peak_fee)} ETH", 12, 700, "start", "attack_tx"))
    for values, color, dash in ((baseline_pool, "baseline_pool", ""), (attack_pool, "attack_pool", ""), (attack_queue, "attack_queue", "6 4")):
        points = value_points(values, ticks, xscale, pool_panel)
        out.append(polyline(points, color, 2.8, dash))
        out.append(circles(points, color, 3.0))
    out.append(legend_item(720, pool_panel.y + 24, "baseline_pool", "baseline pending txpool"))
    out.append(legend_item(720, pool_panel.y + 47, "attack_pool", "attack-run pending txpool"))
    out.append(legend_item(720, pool_panel.y + 70, "attack_queue", "attack-run queued txpool", "6 4"))
    out.append(text(106, pool_panel.y + 24, "txpool pressure remains elevated after attack burst", 12, 700, "start", "attack_pool"))
    out.append(text(604, 878, "Aligned phase tick", 13, 400, "middle", "muted"))
    out.append(text(96, 915, f"Baseline normal total: {fmt(sum(baseline_normal_fee.values()))} ETH", 13, 700))
    out.append(text(380, 915, f"Attack-run normal total: {fmt(sum(attack_normal_fee.values()))} ETH", 13, 700))
    out.append(text(690, 915, f"Adversarial total: {fmt(sum(attack_fee.values()))} ETH", 13, 700))
    out.append(text(96, 939, "Read this fee figure together with the finality figure; fee alone does not prove eviction.", 12, 400, "start", "muted"))
    out.append("</svg>\n")
    (out_dir / "图3_每区块交易费用.svg").write_text("\n".join(out), encoding="utf-8")
    write_csv(
        out_dir / "图3_每区块交易费用.csv",
        [
            {
                "globalTick": tick,
                "phase": phase_by_tick(attack_ts).get(tick, ""),
                "baselineNormalFeeEth": f"{baseline_normal_fee.get(tick, 0.0):.12f}",
                "attackRunNormalFeeEth": f"{attack_normal_fee.get(tick, 0.0):.12f}",
                "attackTxFeeEth": f"{attack_fee.get(tick, 0.0):.12f}",
            }
            for tick in ticks
        ],
    )


def draw_dual_axes(panel: DualPanel, xscale: TickScale) -> list[str]:
    out = [rect(panel.x, panel.y, panel.width, panel.height, "#ffffff", COLORS["axis"])]
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = panel.y + panel.height - frac * panel.height
        out.append(line(panel.x, y, panel.x + panel.width, y, "grid"))
        out.append(text(panel.x - 12, y + 4, fmt(frac * panel.left_max), 12, 400, "end", "muted"))
        out.append(text(panel.x + panel.width + 12, y + 4, fmt(frac * panel.right_max), 12, 400, "start", "muted"))
    for tick in range(xscale.min_tick, xscale.max_tick + 1):
        x = xscale.x(tick)
        out.append(line(x, panel.y + panel.height, x, panel.y + panel.height + 5))
        if tick == xscale.min_tick or tick == xscale.max_tick or tick % 2 == 0:
            out.append(text(x, panel.y + panel.height + 22, tick, 12, 400, "middle", "muted"))
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
    out.append(text(panel.x + panel.width / 2, panel.y + panel.height + 55, "Aligned phase tick", 13, 400, "middle", "muted"))
    return out


def dual_value_points(
    values: dict[int, float],
    ticks: list[int],
    xscale: TickScale,
    panel: DualPanel,
    axis: str,
    multiplier: float,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for tick in ticks:
        value = values.get(tick, 0.0) * multiplier
        y = panel.y_right(value) if axis == "right" else panel.y_left(value)
        points.append((xscale.x(tick), y))
    return points


def draw_figure4(pair_dir: Path, out_dir: Path) -> None:
    baseline_ts = read_csv(pair_dir / "baseline" / "timeseries.csv")
    attack_ts = read_csv(pair_dir / "attack" / "timeseries.csv")
    markers = read_csv(pair_dir / "attack" / "phase_markers.csv")
    baseline_status = read_csv(pair_dir / "baseline" / "tx_final_status.csv")
    attack_status = read_csv(pair_dir / "attack" / "tx_final_status.csv")
    ticks = sorted({as_int(row["globalTick"]) for row in baseline_ts + attack_ts})
    xscale = TickScale(110, 912, ticks)

    baseline_normal_fee = dict(fee_by_tick(baseline_status, "normal"))
    attack_normal_fee = dict(fee_by_tick(attack_status, "normal"))
    attack_fee = dict(fee_by_tick(attack_status, "attack"))
    left_max = nice_max(max([value * 100 for value in baseline_normal_fee.values()] + [value * 100 for value in attack_normal_fee.values()] + [0.0]))
    right_max = nice_max(max(list(attack_fee.values()) + [0.0]))
    panel = DualPanel(110, 170, 912, 360, left_max, right_max)

    out: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1180" height="720" viewBox="0 0 1180 720">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        rect(0, 0, 1180, 720, "#ffffff"),
        text(110, 46, "Paired tx fee per block on Besu default-pool v3", 26, 800),
        text(110, 76, "Fee is receipt-backed: gasUsed x effectiveGasPrice from tx_final_status.csv.", 13, 400, "start", "muted"),
        text(110, 98, "Left axis scales normal tx fees by 10^-2 ETH; right axis shows adversarial tx fees in ETH.", 12, 400, "start", "muted"),
    ]
    out.extend(draw_phase_labels(markers, xscale, 153))
    out.extend(draw_phase_bands(Panel(panel.x, panel.y, panel.width, panel.height, 1, "", ""), markers, xscale))
    out.extend(draw_dual_axes(panel, xscale))

    out.extend(marker_line(dual_value_points(baseline_normal_fee, ticks, xscale, panel, "left", 100), "baseline", "circle"))
    out.extend(marker_line(dual_value_points(attack_normal_fee, ticks, xscale, panel, "left", 100), "attack_normal", "triangle"))
    out.extend(marker_line(dual_value_points(attack_fee, ticks, xscale, panel, "right", 1), "attack_tx", "cross"))

    out.append(shape_legend_item(690, 76, "baseline", "Normal tx fee (baseline)", "circle"))
    out.append(shape_legend_item(690, 100, "attack_normal", "Normal tx fee (attack run)", "triangle"))
    out.append(shape_legend_item(690, 124, "attack_tx", "Adversarial tx fee (attack run)", "cross"))
    nonzero_attack_ticks = [tick for tick in ticks if attack_fee.get(tick, 0.0) > 0]
    if nonzero_attack_ticks:
        total_attack_fee = sum(attack_fee.values())
        peak_tick = max(nonzero_attack_ticks, key=lambda tick: attack_fee.get(tick, 0.0))
        out.append(text(xscale.x(peak_tick) - 25, panel.y_right(attack_fee[peak_tick]) - 18, f"Attack fees across {len(nonzero_attack_ticks)} blocks: {total_attack_fee:.4f} ETH", 12, 700, "middle", "attack_tx"))
    out.append(text(xscale.x(7) + 10, panel.y_left(left_max * 0.18), "normal fee drops to 0 during attack_on", 12, 700, "start", "attack_normal"))
    out.append(text(110, 625, f"Baseline normal total: {sum(baseline_normal_fee.values()):.4f} ETH", 13, 700))
    out.append(text(400, 625, f"Attack-run normal total: {sum(attack_normal_fee.values()):.4f} ETH", 13, 700))
    out.append(text(720, 625, f"Adversarial total: {sum(attack_fee.values()):.4f} ETH", 13, 700))
    out.append(text(110, 655, "This is the compact paper-style fee figure; use it together with the finality figure for the dropped/queued conclusion.", 12, 400, "start", "muted"))
    out.append("</svg>\n")
    (out_dir / "图4_双轴每区块交易费用.svg").write_text("\n".join(out), encoding="utf-8")
    write_csv(
        out_dir / "图4_双轴每区块交易费用.csv",
        [
            {
                "globalTick": tick,
                "phase": phase_by_tick(attack_ts).get(tick, ""),
                "baselineNormalFeeCentiEth": f"{baseline_normal_fee.get(tick, 0.0) * 100:.12f}",
                "attackRunNormalFeeCentiEth": f"{attack_normal_fee.get(tick, 0.0) * 100:.12f}",
                "attackTxFeeEth": f"{attack_fee.get(tick, 0.0):.12f}",
            }
            for tick in ticks
        ],
    )


def write_notes(pair_dir: Path, out_dir: Path) -> None:
    finality = read_json(pair_dir / "attack" / "normal_finality_summary.json")
    notes = [
        "# Besu default-pool v3 classic figures",
        "",
        "Figure 1: inclusion and txpool pressure.",
        "Figure 2: finality audit for the 6,400 previously unresolved normal transactions.",
        "Figure 3: receipt-backed fee per block with split panels.",
        "Figure 4: receipt-backed paper-style dual-axis fee per block.",
        "",
        f"Normal finality states: {finality.get('states', {})}",
        f"Normal finality phases: {finality.get('submittedPhaseStates', {})}",
        "",
        "All included counts and fee totals in these figures are derived from tx_final_status.csv.",
    ]
    (out_dir / "图说明.md").write_text("\n".join(notes) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair-dir", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    out_dir = args.out_dir or args.pair_dir / "经典三图"
    out_dir.mkdir(parents=True, exist_ok=True)
    draw_figure1(args.pair_dir, out_dir)
    draw_figure2(args.pair_dir, out_dir)
    draw_figure3(args.pair_dir, out_dir)
    draw_figure4(args.pair_dir, out_dir)
    write_notes(args.pair_dir, out_dir)
    for path in sorted(out_dir.iterdir()):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
