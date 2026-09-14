#!/usr/bin/env python3
"""Draw SVG figures for the low-capacity Besu phased experiment."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from html import escape
from pathlib import Path


STATE_COLORS = {
    "INCLUDED_SUCCESS": "#16a34a",
    "INCLUDED_FAILED": "#7f1d1d",
    "PENDING_FINAL": "#2563eb",
    "QUEUED_FINAL": "#f59e0b",
    "DROPPED_OR_EVICTED": "#ef4444",
    "REJECTED_OR_SEND_ERROR": "#6b7280",
    "UNKNOWN_POOL_CONTENT_UNAVAILABLE": "#9ca3af",
}

STATE_LABELS = {
    "INCLUDED_SUCCESS": "included",
    "INCLUDED_FAILED": "failed",
    "PENDING_FINAL": "pending final",
    "QUEUED_FINAL": "queued final",
    "DROPPED_OR_EVICTED": "dropped / evicted",
    "REJECTED_OR_SEND_ERROR": "rejected / send error",
    "UNKNOWN_POOL_CONTENT_UNAVAILABLE": "unknown",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def to_int(value: str | None, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(value, 0)


def short_label(label: str) -> str:
    def numeric_prefix(value: str) -> int:
        digits = []
        for ch in value:
            if not ch.isdigit():
                break
            digits.append(ch)
        return int("".join(digits) or "0")

    if label.startswith("pn"):
        return "N" + str(numeric_prefix(label[2:]))
    if label.startswith("pa"):
        return "A" + str(numeric_prefix(label[2:]))
    return label


class Svg:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.parts: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#ffffff"/>',
            '<style>',
            "text{font-family:Arial,Helvetica,sans-serif;fill:#111827}",
            ".muted{fill:#4b5563}",
            ".small{font-size:12px}",
            ".axis{stroke:#111827;stroke-width:1}",
            ".grid{stroke:#e5e7eb;stroke-width:1}",
            ".dash{stroke:#9ca3af;stroke-width:1;stroke-dasharray:4 4}",
            "</style>",
        ]

    def add(self, value: str) -> None:
        self.parts.append(value)

    def text(
        self,
        x: float,
        y: float,
        value: str,
        size: int = 13,
        weight: int | str = 400,
        anchor: str = "start",
        cls: str = "",
        rotate: float | None = None,
    ) -> None:
        extra = f' class="{cls}"' if cls else ""
        transform = f' transform="rotate({rotate} {x} {y})"' if rotate is not None else ""
        self.add(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}"{extra}{transform}>{escape(value)}</text>'
        )

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: str = "#111827",
        width: float = 1,
        cls: str = "",
        dash: str | None = None,
    ) -> None:
        klass = f' class="{cls}"' if cls else ""
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="{width}"{klass}{dash_attr}/>'
        )

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        fill: str,
        stroke: str = "none",
        opacity: float | None = None,
        rx: float = 0,
    ) -> None:
        op = f' opacity="{opacity}"' if opacity is not None else ""
        self.add(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
            f'fill="{fill}" stroke="{stroke}" rx="{rx:.1f}"{op}/>'
        )

    def circle(self, x: float, y: float, radius: float, fill: str, stroke: str = "none") -> None:
        self.add(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{fill}" stroke="{stroke}"/>'
        )

    def polyline(self, points: list[tuple[float, float]], color: str, width: float = 2.5) -> None:
        if not points:
            return
        value = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        self.add(
            f'<polyline points="{value}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    def polygon(self, points: list[tuple[float, float]], fill: str, opacity: float = 1.0) -> None:
        value = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        self.add(f'<polygon points="{value}" fill="{fill}" opacity="{opacity}"/>')

    def save(self, path: Path) -> None:
        self.parts.append("</svg>")
        path.write_text("\n".join(self.parts) + "\n")


def scale(value: float, d0: float, d1: float, r0: float, r1: float) -> float:
    if d0 == d1:
        return (r0 + r1) / 2
    return r0 + ((value - d0) / (d1 - d0)) * (r1 - r0)


def x_for_tick(tick: float, left: float, width: float, max_tick: int) -> float:
    return scale(tick, 0.5, max_tick + 0.5, left, left + width)


def draw_axes(
    svg: Svg,
    left: float,
    top: float,
    width: float,
    height: float,
    max_tick: int,
    y_max: float,
    y_label: str,
    x_label: str | None = None,
) -> None:
    svg.rect(left, top, width, height, "#ffffff", "#d1d5db")
    for step in range(0, 5):
        value = y_max * step / 4
        y = scale(value, 0, y_max, top + height, top)
        svg.line(left, y, left + width, y, color="#e5e7eb")
        svg.text(left - 12, y + 4, f"{value:g}", size=11, anchor="end", cls="muted")
    for tick in range(1, max_tick + 1):
        x = x_for_tick(tick, left, width, max_tick)
        svg.line(x, top + height, x, top + height + 5, color="#111827")
        svg.text(x, top + height + 20, str(tick), size=11, anchor="middle", cls="muted")
    svg.line(left, top, left, top + height, cls="axis")
    svg.line(left, top + height, left + width, top + height, cls="axis")
    svg.text(left - 54, top + height / 2, y_label, size=12, anchor="middle", cls="muted", rotate=-90)
    if x_label:
        svg.text(left + width / 2, top + height + 44, x_label, size=12, anchor="middle", cls="muted")


def draw_phase_bands(
    svg: Svg,
    phases: list[dict[str, str]],
    left: float,
    top: float,
    width: float,
    height: float,
    max_tick: int,
) -> None:
    fills = {
        "warmup": "#eff6ff",
        "saturation": "#f8fafc",
        "attack_on": "#fee2e2",
        "control": "#f8fafc",
        "recovery": "#f0fdf4",
        "drain": "#f8fafc",
    }
    for phase in phases:
        start = to_int(phase["startGlobalTick"])
        end = to_int(phase["endGlobalTick"])
        name = phase["phase"]
        x0 = x_for_tick(start - 0.5, left, width, max_tick)
        x1 = x_for_tick(end + 0.5, left, width, max_tick)
        svg.rect(x0, top, x1 - x0, height, fills.get(name, "#f9fafb"), opacity=0.45)
        svg.line(x0, top, x0, top + height, color="#9ca3af", dash="4 4")
        svg.text((x0 + x1) / 2, top - 8, name.replace("_", " "), size=11, weight=700, anchor="middle", cls="muted")
    svg.line(left + width, top, left + width, top + height, color="#9ca3af", dash="4 4")


def series_points(
    rows: list[dict[str, str]],
    column: str,
    left: float,
    top: float,
    width: float,
    height: float,
    max_tick: int,
    y_max: float,
) -> list[tuple[float, float]]:
    out = []
    for row in rows:
        tick = to_int(row["globalTick"])
        value = to_int(row.get(column))
        out.append(
            (
                x_for_tick(tick, left, width, max_tick),
                scale(value, 0, y_max, top + height, top),
            )
        )
    return out


def draw_line_series(
    svg: Svg,
    rows: list[dict[str, str]],
    column: str,
    left: float,
    top: float,
    width: float,
    height: float,
    max_tick: int,
    y_max: float,
    color: str,
    marker: str = "circle",
) -> None:
    pts = series_points(rows, column, left, top, width, height, max_tick, y_max)
    svg.polyline(pts, color)
    for x, y in pts:
        if marker == "square":
            svg.rect(x - 4, y - 4, 8, 8, color)
        else:
            svg.circle(x, y, 4, color)


def draw_legend(svg: Svg, items: list[tuple[str, str]], x: float, y: float) -> None:
    for idx, (label, color) in enumerate(items):
        yy = y + idx * 22
        svg.line(x, yy - 4, x + 22, yy - 4, color=color, width=3)
        svg.circle(x + 11, yy - 4, 4, color)
        svg.text(x + 32, yy, label, size=12)


def draw_process_figure(
    baseline_rows: list[dict[str, str]],
    attack_rows: list[dict[str, str]],
    attack_phases: list[dict[str, str]],
    out_path: Path,
) -> None:
    max_tick = max(to_int(r["globalTick"]) for r in attack_rows + baseline_rows)
    svg = Svg(1120, 760)
    left = 92
    width = 930
    top1 = 128
    h1 = 220
    top2 = 452
    h2 = 205

    svg.text(76, 48, "Low-capacity phased attack on Besu", size=27, weight=800)
    svg.text(
        76,
        76,
        "Besu txpool max-size=10. Baseline is clean; attack run injects EIP-7702 transactions during attack_on.",
        size=13,
        cls="muted",
    )

    draw_phase_bands(svg, attack_phases, left, top1, width, h1, max_tick)
    draw_axes(svg, left, top1, width, h1, max_tick, 10, "Included txs per block")
    draw_line_series(svg, baseline_rows, "normalIncludedInObservedBlocks", left, top1, width, h1, max_tick, 10, "#2563eb")
    draw_line_series(svg, attack_rows, "normalIncludedInObservedBlocks", left, top1, width, h1, max_tick, 10, "#dc2626")
    draw_line_series(svg, attack_rows, "attackIncludedInObservedBlocks", left, top1, width, h1, max_tick, 10, "#f97316", marker="square")

    draw_legend(
        svg,
        [
            ("normal included, baseline", "#2563eb"),
            ("normal included, attack run", "#dc2626"),
            ("attack included, attack run", "#f97316"),
        ],
        720,
        106,
    )
    svg.text(340, 185, "normal inclusion stalls during attack_on", size=13, weight=700, anchor="middle", cls="muted")
    svg.text(405, 243, "5 attack txs are included", size=13, weight=700, anchor="middle", cls="muted")

    def with_resident(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        updated = []
        for row in rows:
            copy = dict(row)
            total = to_int(row.get("txpoolPendingAfterBlock")) + to_int(row.get("txpoolQueuedAfterBlock"))
            copy["txpoolResidentAfterBlock"] = str(total)
            updated.append(copy)
        return updated

    baseline_resident = with_resident(baseline_rows)
    attack_resident = with_resident(attack_rows)
    draw_phase_bands(svg, attack_phases, left, top2, width, h2, max_tick)
    draw_axes(svg, left, top2, width, h2, max_tick, 10, "Txs left in txpool", "Aligned phase tick")
    draw_line_series(svg, baseline_resident, "txpoolResidentAfterBlock", left, top2, width, h2, max_tick, 10, "#0f766e")
    draw_line_series(svg, attack_resident, "txpoolResidentAfterBlock", left, top2, width, h2, max_tick, 10, "#7c3aed")
    svg.line(left, scale(10, 0, 10, top2 + h2, top2), left + width, scale(10, 0, 10, top2 + h2, top2), color="#111827", width=1.2, dash="5 5")
    svg.text(left + width - 8, top2 + 14, "configured max-size = 10 txs", size=11, anchor="end", cls="muted")
    draw_legend(svg, [("txpool after block, baseline", "#0f766e"), ("txpool after block, attack run", "#7c3aed")], 720, 430)
    svg.text(415, 506, "attack pressure remains resident near the pool limit", size=13, weight=700, anchor="middle", cls="muted")

    svg.text(76, 716, "Data: timeseries.csv from lowcap10 baseline and attack runs.", size=11, cls="muted")
    svg.save(out_path)


def count_states(rows: list[dict[str, str]], group: str | None = None, phase: str | None = None) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        if group is not None and row.get("group") != group:
            continue
        if phase is not None and row.get("submittedPhase") != phase:
            continue
        counter[row["finalState"]] += 1
    return counter


def draw_stacked_bar(
    svg: Svg,
    x: float,
    y: float,
    width: float,
    height: float,
    counts: Counter,
    total_for_scale: int,
) -> None:
    current = x
    total = sum(counts.values())
    order = ["INCLUDED_SUCCESS", "PENDING_FINAL", "QUEUED_FINAL", "DROPPED_OR_EVICTED", "INCLUDED_FAILED", "REJECTED_OR_SEND_ERROR", "UNKNOWN_POOL_CONTENT_UNAVAILABLE"]
    for state in order:
        count = counts.get(state, 0)
        if not count:
            continue
        segment = width * count / total_for_scale
        svg.rect(current, y, segment, height, STATE_COLORS[state], rx=4)
        if segment > 34:
            svg.text(current + segment / 2, y + height / 2 + 5, str(count), size=12, weight=700, anchor="middle")
        else:
            svg.text(current + segment + 5, y + height / 2 + 5, str(count), size=11, weight=700, anchor="start", cls="muted")
        current += segment
    if total < total_for_scale:
        svg.rect(current, y, width * (total_for_scale - total) / total_for_scale, height, "#f3f4f6", "#e5e7eb", rx=4)


def draw_final_status_figure(
    baseline_status: list[dict[str, str]],
    attack_status: list[dict[str, str]],
    baseline_summary: dict,
    attack_summary: dict,
    out_path: Path,
) -> None:
    rows = [
        ("Baseline normal", count_states(baseline_status, "normal"), 48),
        ("Attack-run normal", count_states(attack_status, "normal"), 48),
        ("Attack-run EIP-7702", count_states(attack_status, "attack"), 40),
    ]
    max_total = max(total for _, _, total in rows)
    svg = Svg(1080, 560)
    svg.text(70, 48, "Final transaction states under txpool max-size=10", size=26, weight=800)
    svg.text(
        70,
        76,
        "Audit state is measured after receipt lookup plus final txpool_content. Missing from both chain and pool is reported as dropped / evicted.",
        size=13,
        cls="muted",
    )

    legend_x = 70
    legend_y = 112
    for idx, state in enumerate(["INCLUDED_SUCCESS", "QUEUED_FINAL", "DROPPED_OR_EVICTED"]):
        x = legend_x + idx * 190
        svg.rect(x, legend_y - 13, 14, 14, STATE_COLORS[state], rx=2)
        svg.text(x + 22, legend_y, STATE_LABELS[state], size=12)

    x = 285
    y0 = 172
    bar_w = 670
    bar_h = 42
    for idx, (label, counts, total) in enumerate(rows):
        y = y0 + idx * 90
        svg.text(70, y + 27, label, size=15, weight=700)
        svg.text(230, y + 27, f"n={total}", size=12, anchor="end", cls="muted")
        draw_stacked_bar(svg, x, y, bar_w, bar_h, counts, max_total)
        summary = ", ".join(f"{STATE_LABELS.get(k, k)}={v}" for k, v in counts.items())
        svg.text(x, y + 64, summary, size=12, cls="muted")

    base_normal = baseline_summary["byGroup"]["normal"]["states"].get("INCLUDED_SUCCESS", 0)
    attack_normal = attack_summary["byGroup"]["normal"]["states"].get("INCLUDED_SUCCESS", 0)
    attack_dropped = attack_summary["byGroup"]["normal"]["states"].get("DROPPED_OR_EVICTED", 0)
    svg.rect(70, 462, 884, 48, "#f9fafb", "#e5e7eb", rx=6)
    svg.text(
        92,
        492,
        f"Key result: baseline normal {base_normal}/48 included; under attack normal {attack_normal}/48 included and {attack_dropped}/48 are absent from both chain and txpool at audit time.",
        size=14,
        weight=700,
    )
    svg.save(out_path)


def draw_phase_status_figure(status_rows: list[dict[str, str]], out_path: Path) -> None:
    desired = [
        ("normal", "warmup"),
        ("normal", "saturation"),
        ("normal", "attack_on"),
        ("normal", "recovery"),
        ("attack", "attack_on"),
    ]
    rows: list[tuple[str, Counter, int]] = []
    for group, phase in desired:
        counts = count_states(status_rows, group, phase)
        total = sum(counts.values())
        if total:
            rows.append((f"{group} / {phase.replace('_', ' ')}", counts, total))

    max_total = max(total for _, _, total in rows)
    svg = Svg(1120, 650)
    svg.text(70, 48, "Final states by submission phase", size=26, weight=800)
    svg.text(70, 76, "Low-capacity attack run only. This shows when the transactions that disappear were submitted.", size=13, cls="muted")

    for idx, state in enumerate(["INCLUDED_SUCCESS", "QUEUED_FINAL", "DROPPED_OR_EVICTED"]):
        x = 70 + idx * 190
        svg.rect(x, 111, 14, 14, STATE_COLORS[state], rx=2)
        svg.text(x + 22, 124, STATE_LABELS[state], size=12)

    chart_left = 110
    chart_top = 170
    chart_w = 900
    chart_h = 360
    svg.rect(chart_left, chart_top, chart_w, chart_h, "#ffffff", "#d1d5db")
    for step in range(0, 5):
        value = max_total * step / 4
        y = scale(value, 0, max_total, chart_top + chart_h, chart_top)
        svg.line(chart_left, y, chart_left + chart_w, y, color="#e5e7eb")
        svg.text(chart_left - 12, y + 4, f"{value:g}", size=11, anchor="end", cls="muted")
    svg.text(42, chart_top + chart_h / 2, "Transactions", size=12, anchor="middle", cls="muted", rotate=-90)

    slot = chart_w / len(rows)
    bar_w = min(96, slot * 0.58)
    state_order = ["INCLUDED_SUCCESS", "QUEUED_FINAL", "DROPPED_OR_EVICTED", "PENDING_FINAL", "INCLUDED_FAILED", "REJECTED_OR_SEND_ERROR", "UNKNOWN_POOL_CONTENT_UNAVAILABLE"]
    for idx, (label, counts, total) in enumerate(rows):
        center = chart_left + slot * idx + slot / 2
        y_cursor = chart_top + chart_h
        for state in state_order:
            count = counts.get(state, 0)
            if not count:
                continue
            seg_h = chart_h * count / max_total
            y_cursor -= seg_h
            svg.rect(center - bar_w / 2, y_cursor, bar_w, seg_h, STATE_COLORS[state], rx=2)
            if seg_h >= 18:
                svg.text(center, y_cursor + seg_h / 2 + 5, str(count), size=12, weight=700, anchor="middle")
        svg.text(center, chart_top + chart_h + 24, label, size=11, anchor="middle", cls="muted")
        svg.text(center, chart_top + chart_h + 42, f"n={total}", size=10, anchor="middle", cls="muted")

    svg.text(
        70,
        606,
        "Interpretation: normal transactions submitted in attack_on and recovery account for the missing normal txs; attack txs mostly become dropped / evicted or remain queued.",
        size=13,
        cls="muted",
    )
    svg.save(out_path)


def phase_for_tick(phases: list[dict[str, str]], tick: int) -> str:
    for phase in phases:
        start = to_int(phase["startGlobalTick"])
        end = to_int(phase["endGlobalTick"])
        if start <= tick <= end:
            return phase["phase"]
    return ""


def block_tick_map(rows: list[dict[str, str]]) -> dict[int, int]:
    return {to_int(row["blockNumber"]): to_int(row["globalTick"]) for row in rows}


def aggregate_fee_by_tick(
    status_rows: list[dict[str, str]],
    tick_by_block: dict[int, int],
) -> dict[str, dict[int, float]]:
    fees: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for row in status_rows:
        if row.get("receiptFound") != "1":
            continue
        block = to_int(row.get("receiptBlockNumber"))
        if block not in tick_by_block:
            continue
        gas_used = to_int(row.get("gasUsed"))
        effective_price = to_int(row.get("effectiveGasPrice"))
        group = row.get("group") or "unknown"
        tick = tick_by_block[block]
        fees[group][tick] += gas_used * effective_price / 10**15
    return fees


def draw_tx_fee_per_block_figure(
    baseline_rows: list[dict[str, str]],
    attack_rows: list[dict[str, str]],
    attack_phases: list[dict[str, str]],
    baseline_status: list[dict[str, str]],
    attack_status: list[dict[str, str]],
    out_path: Path,
) -> None:
    max_tick = max(to_int(r["globalTick"]) for r in baseline_rows + attack_rows)
    baseline_fees = aggregate_fee_by_tick(baseline_status, block_tick_map(baseline_rows))
    attack_fees = aggregate_fee_by_tick(attack_status, block_tick_map(attack_rows))

    baseline_normal = baseline_fees.get("normal", {})
    attack_normal = attack_fees.get("normal", {})
    attack_adversarial = attack_fees.get("attack", {})
    y_max = max(
        [1.0]
        + list(baseline_normal.values())
        + list(attack_normal.values())
        + list(attack_adversarial.values())
    )
    y_max = max(1.0, round(y_max * 1.25 + 0.05, 2))

    svg = Svg(1120, 620)
    left = 92
    top = 132
    width = 930
    height = 350

    svg.text(76, 48, "Low-capacity tx fee per block on Besu", size=27, weight=800)
    svg.text(
        76,
        76,
        "Receipt-backed fees aggregated by included block, phase-aligned across baseline and attack runs.",
        size=13,
        cls="muted",
    )
    svg.text(76, 96, "Unit: mETH per block. 1 mETH = 0.001 ETH.", size=12, cls="muted")

    draw_phase_bands(svg, attack_phases, left, top, width, height, max_tick)
    draw_axes(svg, left, top, width, height, max_tick, y_max, "Tx fee per block (mETH)", "Aligned phase tick")

    def draw_fee_series(values: dict[int, float], color: str, marker: str = "circle") -> None:
        pts = []
        for tick in range(1, max_tick + 1):
            value = values.get(tick, 0.0)
            pts.append((x_for_tick(tick, left, width, max_tick), scale(value, 0, y_max, top + height, top)))
        svg.polyline(pts, color)
        for x, y in pts:
            if marker == "square":
                svg.rect(x - 4, y - 4, 8, 8, color)
            else:
                svg.circle(x, y, 4, color)

    draw_fee_series(baseline_normal, "#2563eb")
    draw_fee_series(attack_normal, "#dc2626")
    draw_fee_series(attack_adversarial, "#f97316", marker="square")

    draw_legend(
        svg,
        [
            ("normal tx fee, baseline", "#2563eb"),
            ("normal tx fee, attack run", "#dc2626"),
            ("EIP-7702 tx fee, attack run", "#f97316"),
        ],
        715,
        118,
    )

    attack_start = x_for_tick(3 - 0.5, left, width, max_tick)
    attack_end = x_for_tick(4 + 0.5, left, width, max_tick)
    svg.rect(attack_start, top, attack_end - attack_start, height, "#fee2e2", opacity=0.18)
    svg.text((attack_start + attack_end) / 2, top + 44, "attack_on: normal fee drops to 0", size=13, weight=700, anchor="middle", cls="muted")

    base_total = sum(baseline_normal.values())
    attack_normal_total = sum(attack_normal.values())
    attack_total = sum(attack_adversarial.values())
    svg.rect(76, 530, 930, 48, "#f9fafb", "#e5e7eb", rx=6)
    svg.text(
        96,
        560,
        f"Totals: baseline normal {base_total:.3f} mETH; attack-run normal {attack_normal_total:.3f} mETH; EIP-7702 attack {attack_total:.3f} mETH.",
        size=14,
        weight=700,
    )
    svg.save(out_path)


def draw_nonce_grid(status_rows: list[dict[str, str]], out_path: Path) -> None:
    grouped: dict[str, dict[int, str]] = defaultdict(dict)
    groups: dict[str, str] = {}
    max_nonce = 0
    for row in status_rows:
        label = short_label(row["label"])
        nonce = to_int(row["nonce"])
        grouped[label][nonce] = row["finalState"]
        groups[label] = row["group"]
        max_nonce = max(max_nonce, nonce)

    normal_labels = sorted([k for k, v in groups.items() if v == "normal"], key=lambda s: int(s[1:]))
    attack_labels = sorted([k for k, v in groups.items() if v == "attack"], key=lambda s: int(s[1:]))
    labels = normal_labels + attack_labels

    cell = 34
    left = 118
    top = 145
    width = left + (max_nonce + 1) * cell + 90
    height = top + len(labels) * cell + 110
    svg = Svg(max(760, width), max(470, height))
    svg.text(70, 48, "Per-sender nonce final state grid", size=26, weight=800)
    svg.text(70, 76, "Low-capacity attack run. Each cell is one submitted tx keyed by sender label and nonce.", size=13, cls="muted")

    legend_x = 70
    legend_y = 112
    for idx, state in enumerate(["INCLUDED_SUCCESS", "QUEUED_FINAL", "DROPPED_OR_EVICTED"]):
        x = legend_x + idx * 190
        svg.rect(x, legend_y - 13, 14, 14, STATE_COLORS[state], rx=2)
        svg.text(x + 22, legend_y, STATE_LABELS[state], size=12)

    for nonce in range(0, max_nonce + 1):
        x = left + nonce * cell + cell / 2
        svg.text(x, top - 16, str(nonce), size=11, anchor="middle", cls="muted")
    svg.text(left + (max_nonce + 1) * cell / 2, top - 42, "nonce", size=12, anchor="middle", cls="muted")

    for row_idx, label in enumerate(labels):
        y = top + row_idx * cell
        if row_idx == len(normal_labels):
            svg.line(70, y - 7, left + (max_nonce + 1) * cell, y - 7, color="#9ca3af", dash="4 4")
        svg.text(70, y + 22, label, size=12, weight=700)
        for nonce in range(0, max_nonce + 1):
            x = left + nonce * cell
            state = grouped[label].get(nonce)
            if state:
                svg.rect(x + 3, y + 3, cell - 6, cell - 6, STATE_COLORS[state], "#ffffff", rx=4)
            else:
                svg.rect(x + 3, y + 3, cell - 6, cell - 6, "#f3f4f6", "#ffffff", rx=4)

    svg.text(70, height - 36, "Green means included. Red means accepted but absent from both chain and final txpool_content. Orange means still queued.", size=12, cls="muted")
    svg.save(out_path)


def write_notes(out_dir: Path, baseline_summary: dict, attack_summary: dict) -> None:
    normal_base = baseline_summary["byGroup"]["normal"]["states"]
    normal_attack = attack_summary["byGroup"]["normal"]["states"]
    attack_attack = attack_summary["byGroup"]["attack"]["states"]
    notes = [
        "# Low-capacity Besu figures",
        "",
        "Source runs:",
        f"- Baseline: {baseline_summary['runDir']}",
        f"- Attack: {attack_summary['runDir']}",
        "",
        "Key audit counts:",
        f"- Baseline normal states: {dict(normal_base)}",
        f"- Attack-run normal states: {dict(normal_attack)}",
        f"- Attack-run EIP-7702 states: {dict(attack_attack)}",
        "",
        "Figure files:",
        "- fig1_lowcap10_inclusion_txpool.svg",
        "- fig2_lowcap10_final_status.svg",
        "- fig3_lowcap10_status_by_phase.svg",
        "- fig4_lowcap10_sender_nonce_grid.svg",
        "- fig5_lowcap10_tx_fee_per_block.svg",
        "",
        "Note: DROPPED_OR_EVICTED means accepted, no receipt, and absent from final txpool_content at audit time.",
    ]
    (out_dir / "figure_notes.md").write_text("\n".join(notes) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--attack-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    baseline_rows = read_csv(args.baseline_dir / "timeseries.csv")
    attack_rows = read_csv(args.attack_dir / "timeseries.csv")
    attack_phases = read_csv(args.attack_dir / "phase_markers.csv")
    baseline_status = read_csv(args.baseline_dir / "tx_final_status.csv")
    attack_status = read_csv(args.attack_dir / "tx_final_status.csv")
    baseline_summary = read_json(args.baseline_dir / "tx_final_status_summary.json")
    attack_summary = read_json(args.attack_dir / "tx_final_status_summary.json")

    draw_process_figure(
        baseline_rows,
        attack_rows,
        attack_phases,
        args.out_dir / "fig1_lowcap10_inclusion_txpool.svg",
    )
    draw_final_status_figure(
        baseline_status,
        attack_status,
        baseline_summary,
        attack_summary,
        args.out_dir / "fig2_lowcap10_final_status.svg",
    )
    draw_phase_status_figure(
        attack_status,
        args.out_dir / "fig3_lowcap10_status_by_phase.svg",
    )
    draw_tx_fee_per_block_figure(
        baseline_rows,
        attack_rows,
        attack_phases,
        baseline_status,
        attack_status,
        args.out_dir / "fig5_lowcap10_tx_fee_per_block.svg",
    )
    draw_nonce_grid(
        attack_status,
        args.out_dir / "fig4_lowcap10_sender_nonce_grid.svg",
    )
    write_notes(args.out_dir, baseline_summary, attack_summary)

    for path in sorted(args.out_dir.iterdir()):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
