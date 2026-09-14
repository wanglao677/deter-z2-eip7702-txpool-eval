#!/usr/bin/env python3
"""Draw a normal-transaction finality figure for a Besu paired pool experiment."""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


COLORS = {
    "included": "#16a34a",
    "absent": "#dc2626",
    "queued": "#f59e0b",
    "pending": "#2563eb",
    "unknown": "#9ca3af",
    "axis": "#27272a",
    "grid": "#e5e7eb",
    "text": "#18181b",
    "muted": "#52525b",
    "phase": "#f8fafc",
    "attack": "#fff1f2",
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

PHASE_LABELS = {
    "warmup": "Warmup",
    "saturation": "Saturation",
    "attack_on": "Attack on",
    "recovery": "Recovery",
    "drain": "Drain",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def esc(value: str) -> str:
    return html.escape(str(value), quote=True)


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


def line(x1: float, y1: float, x2: float, y2: float, color: str = "axis", dash: str = "") -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{COLORS[color]}" stroke-width="1"{dash_attr}/>'
    )


def state_order() -> list[str]:
    return [
        "INCLUDED_SUCCESS",
        "ABSENT_NO_RECEIPT_CONFIRMED",
        "STILL_QUEUED_AT_FINAL_AUDIT",
        "STILL_PENDING_AT_FINAL_AUDIT",
        "INCLUDED_FAILED",
        "UNKNOWN_NEEDS_MORE_OBSERVATIONS",
    ]


def draw_stacked_bar(
    label: str,
    counts: Counter[str],
    total: int,
    x: float,
    y: float,
    width: float,
    height: float,
) -> list[str]:
    out = [text(78, y + 27, label, 14, 700), text(260, y + 27, f"n={total}", 12, 400, "end", "muted")]
    cursor = x
    for state in state_order():
        count = counts.get(state, 0)
        if count <= 0:
            continue
        seg = width * count / max(1, total)
        out.append(rect(cursor, y, seg, height, STATE_COLORS[state], radius=4))
        if seg >= 54:
            out.append(text(cursor + seg / 2, y + height / 2 + 5, str(count), 12, 700, "middle"))
        else:
            out.append(text(cursor + seg + 5, y + height / 2 + 5, str(count), 11, 700, "start", "muted"))
        cursor += seg
    if cursor < x + width:
        out.append(rect(cursor, y, x + width - cursor, height, "#f3f4f6", "#e5e7eb", radius=4))
    return out


def draw_phase_bars(phase_counts: dict[str, Counter[str]], x: float, y: float, width: float, height: float) -> list[str]:
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
            out.append(rect(center - slot / 2 + 4, y, slot - 8, height, COLORS["attack"], opacity=0.72))
        cursor = y + height
        counts = phase_counts[phase]
        for state in state_order():
            count = counts.get(state, 0)
            if count <= 0:
                continue
            seg_h = height * count / max_total
            cursor -= seg_h
            out.append(rect(center - bar_width / 2, cursor, bar_width, seg_h, STATE_COLORS[state], "#ffffff", radius=2))
            if seg_h >= 18:
                out.append(text(center, cursor + seg_h / 2 + 5, str(count), 12, 700, "middle"))
        out.append(text(center, y + height + 25, PHASE_LABELS[phase], 12, 700, "middle", "muted"))
    return out


def normal_counts(pair_dir: Path) -> tuple[Counter[str], Counter[str], dict[str, Counter[str]], dict[str, Any]]:
    baseline_summary = read_json(pair_dir / "baseline" / "tx_final_status_summary.json")
    attack_status = read_csv(pair_dir / "attack" / "tx_final_status.csv")
    finality = read_json(pair_dir / "attack" / "normal_finality_summary.json")

    baseline = Counter(baseline_summary["byGroup"]["normal"]["states"])
    attack_all: Counter[str] = Counter()
    phase_counts: dict[str, Counter[str]] = defaultdict(Counter)

    for row in attack_status:
        if row.get("group") != "normal":
            continue
        phase = row.get("submittedPhase") or "unknown"
        if row.get("receiptFound") == "1":
            attack_all["INCLUDED_SUCCESS"] += 1
            phase_counts[phase]["INCLUDED_SUCCESS"] += 1

    for state, count in finality.get("states", {}).items():
        attack_all[state] += int(count)
    for phase, states in finality.get("submittedPhaseStates", {}).items():
        for state, count in states.items():
            phase_counts[phase][state] += int(count)

    return baseline, attack_all, phase_counts, finality


def render(pair_dir: Path, out_path: Path) -> None:
    baseline, attack_all, phase_counts, finality = normal_counts(pair_dir)
    baseline_total = sum(baseline.values())
    attack_total = sum(attack_all.values())

    width = 1180
    height = 760
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        rect(0, 0, width, height, "#ffffff"),
        text(78, 50, "Default-pool v3 normal transaction finality on Besu", 26, 800),
        text(78, 78, "Unresolved normal transactions are re-audited across fresh blocks using receipt lookup plus txpool_content.", 13, 400, "start", "muted"),
    ]

    legend_y = 120
    legend_items = [
        ("INCLUDED_SUCCESS", 78),
        ("ABSENT_NO_RECEIPT_CONFIRMED", 260),
        ("STILL_QUEUED_AT_FINAL_AUDIT", 520),
        ("STILL_PENDING_AT_FINAL_AUDIT", 690),
    ]
    for state, x in legend_items:
        out.append(rect(x, legend_y - 12, 14, 14, STATE_COLORS[state], radius=2))
        out.append(text(x + 22, legend_y, STATE_LABELS[state], 12))

    out.append(text(78, 170, "Overall normal-transaction outcome", 15, 700))
    bar_x = 292
    bar_w = 760
    out.extend(draw_stacked_bar("Baseline normal", baseline, baseline_total, bar_x, 200, bar_w, 42))
    out.extend(draw_stacked_bar("Attack-run normal", attack_all, attack_total, bar_x, 278, bar_w, 42))

    dropped = attack_all.get("ABSENT_NO_RECEIPT_CONFIRMED", 0)
    queued = attack_all.get("STILL_QUEUED_AT_FINAL_AUDIT", 0)
    included = attack_all.get("INCLUDED_SUCCESS", 0)
    out.append(rect(78, 354, 974, 58, "#f9fafb", "#e5e7eb", radius=6))
    out.append(text(100, 388, f"Key result: {included} normal txs included, {dropped} confirmed absent/no receipt, {queued} still queued.", 14, 700))
    out.append(text(100, 408, "The confirmed-absent normal txs were submitted during attack_on; queued txs were submitted during recovery.", 12, 400, "start", "muted"))

    out.extend(draw_phase_bars(phase_counts, 128, 470, 924, 190))
    out.append(text(590, 720, f"Finality audit window: blocks {finality.get('startBlock')} to {finality.get('endBlock')}; confirmation threshold: {finality.get('confirmAbsentObservations')} absent observations.", 12, 400, "middle", "muted"))
    out.append("</svg>\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    render(args.pair_dir, args.out)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
