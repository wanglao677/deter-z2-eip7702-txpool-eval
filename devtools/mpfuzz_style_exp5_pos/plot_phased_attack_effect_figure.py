#!/usr/bin/env python3
"""Render a focused SVG for a paired phased baseline/attack experiment."""

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


COLORS = {
    "baseline": "#2563eb",
    "attack_normal": "#dc2626",
    "attack_tx": "#ea580c",
    "baseline_pool": "#0f766e",
    "attack_pool": "#7c3aed",
    "attack_queue": "#a855f7",
    "axis": "#27272a",
    "grid": "#d4d4d8",
    "text": "#18181b",
    "muted": "#52525b",
    "phase": "#f8fafc",
    "attack_phase": "#fff1f2",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot phased Deter-Z2 attack effect with txpool gas pressure.")
    parser.add_argument("--baseline-dir", required=True, help="Directory containing baseline timeseries.csv.")
    parser.add_argument("--attack-dir", required=True, help="Directory containing attack timeseries.csv.")
    parser.add_argument("--out", required=True, help="Output SVG path.")
    parser.add_argument("--derived-csv-out", default=None, help="Optional derived CSV output path.")
    parser.add_argument("--rpc", default=None, help="Optional RPC URL used to correct per-block inclusion from chain tx hashes.")
    parser.add_argument("--title", default="First-0-Payload Phased Attack on Besu")
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


def as_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


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


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["result"]


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
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    if abs(value) >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


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


class TickScale:
    def __init__(self, left: float, width: float, ticks: list[int]):
        self.left = left
        self.width = width
        self.ticks = ticks
        self.min_tick = min(ticks)
        self.max_tick = max(ticks)
        self.count = max(1, self.max_tick - self.min_tick + 1)
        self.step = width / self.count

    def x(self, tick: int | float) -> float:
        return self.left + ((tick - self.min_tick) + 0.5) * self.step

    def band_start(self, tick: int | float) -> float:
        return self.left + (tick - self.min_tick) * self.step


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


def value_points(values_by_tick: dict[int, float], ticks: list[int], xscale: TickScale, panel: Panel) -> list[tuple[float, float]]:
    return [(xscale.x(tick), panel.y_value(values_by_tick.get(tick, 0.0))) for tick in ticks]


def polyline(points: list[tuple[float, float]], color: str, width: float = 2.8, dash: str = "") -> str:
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linejoin="round" stroke-linecap="round"{dash_attr}/>'
    )


def area_path(values_by_tick: dict[int, float], ticks: list[int], xscale: TickScale, panel: Panel, color: str, opacity: float) -> str:
    points = value_points(values_by_tick, ticks, xscale, panel)
    if not points:
        return ""
    baseline_y = panel.y + panel.height
    path = [f"M {points[0][0]:.2f} {baseline_y:.2f}", *(f"L {x:.2f} {y:.2f}" for x, y in points)]
    path.append(f"L {points[-1][0]:.2f} {baseline_y:.2f} Z")
    return f'<path d="{" ".join(path)}" fill="{color}" opacity="{opacity:.2f}"/>'


def circles(points: list[tuple[float, float]], color: str, radius: float = 3.1) -> str:
    return "\n".join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius}" fill="{color}"/>' for x, y in points)


def bars(values_by_tick: dict[int, float], ticks: list[int], xscale: TickScale, panel: Panel, color: str) -> list[str]:
    out: list[str] = []
    bar_width = xscale.step * 0.42
    for tick in ticks:
        value = values_by_tick.get(tick, 0.0)
        if value <= 0:
            continue
        x = xscale.x(tick) - bar_width / 2
        y = panel.y_value(value)
        out.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{panel.y + panel.height - y:.2f}" '
            f'fill="{color}" opacity="0.76"/>'
        )
    return out


def draw_axes(panel: Panel, xscale: TickScale, show_x_labels: bool) -> list[str]:
    out = [
        f'<rect x="{panel.x:.2f}" y="{panel.y:.2f}" width="{panel.width:.2f}" height="{panel.height:.2f}" '
        f'fill="white" stroke="{COLORS["axis"]}" stroke-width="1"/>',
        text(panel.x, panel.y - 14, panel.title, 15, 700),
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = panel.y + panel.height - frac * panel.height
        out.append(
            f'<line x1="{panel.x:.2f}" y1="{y:.2f}" x2="{panel.x + panel.width:.2f}" y2="{y:.2f}" '
            f'stroke="{COLORS["grid"]}" stroke-width="1"/>'
        )
        out.append(text(panel.x - 10, y + 4, format_number(frac * panel.y_max), 12, anchor="end", color="muted"))
    for tick in xscale.ticks:
        x = xscale.x(tick)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y + panel.height:.2f}" x2="{x:.2f}" '
            f'y2="{panel.y + panel.height + 5:.2f}" stroke="{COLORS["axis"]}" stroke-width="1"/>'
        )
        if show_x_labels and (tick == xscale.min_tick or tick == xscale.max_tick or tick % 2 == 0):
            out.append(text(x, panel.y + panel.height + 21, str(tick), 12, anchor="middle", color="muted"))
    out.append(
        f'<text x="{panel.x - 62:.2f}" y="{panel.y + panel.height / 2:.2f}" text-anchor="middle" '
        f'font-size="12" fill="{COLORS["muted"]}" transform="rotate(-90 {panel.x - 62:.2f} {panel.y + panel.height / 2:.2f})">'
        f'{escape(panel.y_label)}</text>'
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
        last = as_int(markers[-1]["endGlobalTick"])
        x = xscale.band_start(last + 1)
        out.append(
            f'<line x1="{x:.2f}" y1="{panel.y:.2f}" x2="{x:.2f}" y2="{panel.y + panel.height:.2f}" '
            f'stroke="#a1a1aa" stroke-width="1" stroke-dasharray="4 4"/>'
        )
    return out


def draw_phase_labels(markers: list[dict[str, str]], xscale: TickScale, y: float) -> list[str]:
    labels = {
        "warmup": "Warmup",
        "saturation": "Saturation",
        "attack_on": "Attack on",
        "control": "Control",
        "recovery": "Recovery",
        "drain": "Drain",
    }
    out: list[str] = []
    for marker in markers:
        start = as_int(marker["startGlobalTick"])
        end = as_int(marker["endGlobalTick"])
        x1 = xscale.band_start(start)
        x2 = xscale.band_start(end + 1)
        out.append(text((x1 + x2) / 2, y, labels.get(marker["phase"], marker["phase"]), 12, 700, anchor="middle", color="muted"))
    return out


def legend_item(x: float, y: float, color: str, label: str, dash: str = "", area: bool = False) -> str:
    if area:
        return (
            f'<rect x="{x:.2f}" y="{y - 7:.2f}" width="26" height="10" fill="{color}" opacity="0.20"/>'
            f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x + 26:.2f}" y2="{y:.2f}" stroke="{color}" stroke-width="3" stroke-linecap="round"/>'
            f'<text x="{x + 34:.2f}" y="{y + 4:.2f}" font-size="12" fill="{COLORS["text"]}">{escape(label)}</text>'
        )
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x:.2f}" y1="{y:.2f}" x2="{x + 26:.2f}" y2="{y:.2f}" stroke="{color}" '
        f'stroke-width="3" stroke-linecap="round"{dash_attr}/>'
        f'<text x="{x + 34:.2f}" y="{y + 4:.2f}" font-size="12" fill="{COLORS["text"]}">{escape(label)}</text>'
    )


def annotate(x: float, y: float, label: str, color: str) -> str:
    return text(x, y, label, 12, 700, color=color)


def block_to_tick(rows: list[dict[str, str]]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for row in rows:
        tick = as_int(row["globalTick"])
        observed = as_int(row.get("blockNumber"))
        if observed > 0:
            mapping[observed] = tick
        start = as_int(row.get("startBlock"))
        target = as_int(row.get("targetBlock"))
        for block_number in range(start + 1, target + 1):
            mapping[block_number] = tick
    return mapping


def record_hashes(run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    normal = {
        str(row["hash"]).lower(): row
        for row in read_records(run_dir / "normal_records.jsonl")
        if row.get("hash")
    }
    attack = {
        str(row["hash"]).lower(): row
        for row in read_records(run_dir / "attack_records.jsonl")
        if row.get("hash")
    }
    return normal, attack


def receipt_blocks_counts_by_tick(
    run_dir: Path,
    rows: list[dict[str, str]],
) -> tuple[dict[int, float], dict[int, float], dict[str, Any]] | None:
    path = run_dir / "fee_receipt_blocks.csv"
    if not path.exists():
        return None

    ticks = [as_int(row["globalTick"]) for row in rows]
    normal_counts = {tick: 0.0 for tick in ticks}
    attack_counts = {tick: 0.0 for tick in ticks}
    for row in read_csv(path):
        tick = as_int(row.get("globalTick"))
        if tick not in normal_counts:
            continue
        normal_counts[tick] += float(as_int(row.get("normalIncluded")))
        attack_counts[tick] += float(as_int(row.get("attackIncluded")))

    audit = read_json(run_dir / "fee_receipt_audit.json")
    attack_group = audit.get("groups", {}).get("attack", {}) if isinstance(audit, dict) else {}
    padding_counts = {}
    for key, value in attack_group.get("paddingBytesHistogram", {}).items():
        try:
            padding_counts[int(key)] = value
        except (TypeError, ValueError):
            padding_counts[str(key)] = value

    return normal_counts, attack_counts, {
        "source": "fee-receipt-blocks",
        "attackPaddingCounts": padding_counts,
        "attackUniqueSenders": attack_group.get("uniqueSenders", ""),
    }


def chain_counts_by_tick(
    run_dir: Path,
    rows: list[dict[str, str]],
    rpc_url: str | None,
) -> tuple[dict[int, float], dict[int, float], dict[str, Any]]:
    from_receipt_blocks = receipt_blocks_counts_by_tick(run_dir, rows)
    if from_receipt_blocks is not None:
        return from_receipt_blocks

    fallback_normal = {as_int(row["globalTick"]): float(as_int(row["normalIncludedInObservedBlocks"])) for row in rows}
    fallback_attack = {as_int(row["globalTick"]): float(as_int(row["attackIncludedInObservedBlocks"])) for row in rows}
    if not rpc_url:
        return fallback_normal, fallback_attack, {"source": "timeseries"}

    normal_hashes, attack_hashes = record_hashes(run_dir)
    if not normal_hashes and not attack_hashes:
        return fallback_normal, fallback_attack, {"source": "timeseries"}

    mapping = block_to_tick(rows)
    normal_counts: dict[int, int] = defaultdict(int)
    attack_counts: dict[int, int] = defaultdict(int)
    attack_padding_counts: dict[int, int] = defaultdict(int)
    attack_suffix_counts: dict[str, int] = defaultdict(int)
    attack_senders: set[str] = set()
    first_block = min(mapping)
    last_block = max(mapping)
    try:
        for block_number in range(first_block, last_block + 1):
            block = rpc_call(rpc_url, "eth_getBlockByNumber", [hex(block_number), False])
            if not block:
                continue
            tick = mapping.get(block_number)
            if tick is None:
                continue
            for tx_hash in block.get("transactions", []):
                key = str(tx_hash).lower()
                normal_record = normal_hashes.get(key)
                if normal_record is not None:
                    normal_counts[tick] += 1
                attack_record = attack_hashes.get(key)
                if attack_record is not None:
                    attack_counts[tick] += 1
                    attack_padding_counts[as_int(attack_record.get("calldataPaddingBytes"))] += 1
                    label = str(attack_record.get("label", ""))
                    attack_suffix_counts[label.split("-")[-1] if "-" in label else label] += 1
                    sender = str(attack_record.get("sender", "")).lower()
                    if sender:
                        attack_senders.add(sender)
    except (OSError, TimeoutError, urllib.error.URLError, RuntimeError):
        return fallback_normal, fallback_attack, {"source": "timeseries"}

    found = sum(normal_counts.values()) + sum(attack_counts.values())
    if found == 0:
        return fallback_normal, fallback_attack, {"source": "timeseries"}
    ticks = [as_int(row["globalTick"]) for row in rows]
    return (
        {tick: float(normal_counts.get(tick, 0)) for tick in ticks},
        {tick: float(attack_counts.get(tick, 0)) for tick in ticks},
        {
            "source": "chain-rpc",
            "attackPaddingCounts": dict(sorted(attack_padding_counts.items())),
            "attackSuffixCounts": dict(sorted(attack_suffix_counts.items())),
            "attackUniqueSenders": len(attack_senders),
        },
    )


def metric_int(metrics: dict[str, str], key: str, default: int) -> int:
    value = metrics.get(key)
    return default if value in (None, "") else as_int(value)


def pool_gas_values(
    rows: list[dict[str, str]],
    metrics: dict[str, str],
    is_attack_run: bool,
) -> tuple[dict[int, float], dict[int, float]]:
    normal_gas = metric_int(metrics, "normalGas", 150000)
    attack_gas = metric_int(metrics, "attackGas", 500000)
    pending_by_tick: dict[int, float] = {}
    queued_by_tick: dict[int, float] = {}
    for row in rows:
        tick = as_int(row["globalTick"])
        pending = as_int(row["txpoolPendingAfterBlock"])
        queued = as_int(row["txpoolQueuedAfterBlock"])
        if is_attack_run:
            normal_backlog = as_int(row.get("normalBacklog"))
            attack_backlog = as_int(row.get("attackBacklog"))
            total_backlog = normal_backlog + attack_backlog
            if total_backlog > 0:
                avg_gas = ((normal_backlog * normal_gas) + (attack_backlog * attack_gas)) / total_backlog
            else:
                avg_gas = normal_gas
        else:
            avg_gas = normal_gas
        pending_by_tick[tick] = (pending * avg_gas) / 1_000_000
        queued_by_tick[tick] = (queued * avg_gas) / 1_000_000
    return pending_by_tick, queued_by_tick


def write_derived_csv(
    path: Path,
    ticks: list[int],
    phase_by_tick: dict[int, str],
    baseline_normal: dict[int, float],
    attack_normal: dict[int, float],
    attack_tx: dict[int, float],
    baseline_pool: dict[int, float],
    attack_pool: dict[int, float],
    attack_queue: dict[int, float],
    attack_rows_by_tick: dict[int, dict[str, str]],
) -> None:
    fieldnames = [
        "globalTick",
        "phase",
        "baselineNormalIncluded",
        "attackRunNormalIncluded",
        "attackTxIncluded",
        "baselinePendingPoolGasM",
        "attackRunPendingPoolGasM",
        "attackRunQueuedPoolGasM",
        "attackRunNormalBacklog",
        "attackBacklog",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for tick in ticks:
            attack_row = attack_rows_by_tick.get(tick, {})
            writer.writerow(
                {
                    "globalTick": tick,
                    "phase": phase_by_tick.get(tick, ""),
                    "baselineNormalIncluded": int(baseline_normal.get(tick, 0)),
                    "attackRunNormalIncluded": int(attack_normal.get(tick, 0)),
                    "attackTxIncluded": int(attack_tx.get(tick, 0)),
                    "baselinePendingPoolGasM": f"{baseline_pool.get(tick, 0.0):.6f}",
                    "attackRunPendingPoolGasM": f"{attack_pool.get(tick, 0.0):.6f}",
                    "attackRunQueuedPoolGasM": f"{attack_queue.get(tick, 0.0):.6f}",
                    "attackRunNormalBacklog": as_int(attack_row.get("normalBacklog")),
                    "attackBacklog": as_int(attack_row.get("attackBacklog")),
                }
            )


def render(
    baseline_dir: Path,
    attack_dir: Path,
    out_path: Path,
    derived_csv_out: Path,
    rpc_url: str | None,
    title_value: str,
) -> None:
    baseline_rows = read_csv(baseline_dir / "timeseries.csv")
    attack_rows = read_csv(attack_dir / "timeseries.csv")
    markers = read_csv(attack_dir / "phase_markers.csv")
    baseline_metrics = read_metrics(baseline_dir)
    attack_metrics = read_metrics(attack_dir)
    if rpc_url is None:
        rpc_url = attack_metrics.get("rpc") or baseline_metrics.get("rpc")

    baseline_normal, _, baseline_meta = chain_counts_by_tick(baseline_dir, baseline_rows, rpc_url)
    attack_normal, attack_tx, attack_meta = chain_counts_by_tick(attack_dir, attack_rows, rpc_url)

    ticks = sorted({as_int(row["globalTick"]) for row in baseline_rows + attack_rows})
    phase_by_tick = {as_int(row["globalTick"]): row["phase"] for row in attack_rows}
    attack_rows_by_tick = {as_int(row["globalTick"]): row for row in attack_rows}

    baseline_pool, _ = pool_gas_values(baseline_rows, baseline_metrics, is_attack_run=False)
    attack_pool, attack_queue = pool_gas_values(attack_rows, attack_metrics, is_attack_run=True)

    include_max = nice_max(
        max(
            [baseline_normal.get(tick, 0.0) for tick in ticks]
            + [attack_normal.get(tick, 0.0) for tick in ticks]
            + [attack_tx.get(tick, 0.0) for tick in ticks]
            + [0.0]
        )
    )
    pool_max = nice_max(
        max(
            [baseline_pool.get(tick, 0.0) for tick in ticks]
            + [attack_pool.get(tick, 0.0) + attack_queue.get(tick, 0.0) for tick in ticks]
            + [0.0]
        )
    )

    width = 1200
    height = 840
    left = 96
    chart_width = 1016
    xscale = TickScale(left, chart_width, ticks)
    top = Panel(left, 165, chart_width, 225, include_max, "Block inclusion during the phased workload", "Txs included")
    bottom = Panel(left, 505, chart_width, 205, pool_max, "Txpool gas pressure after each observed block", "Gas in txpool (M gas)")

    attack_blocks = metric_int(attack_metrics, "attackBlocks", sum(1 for marker in markers if marker.get("phase") == "attack_on"))
    source_note = "Included counts prefer archived receipt blocks; txpool gas is a weighted estimate from observed txpool counts."
    attack_zero_payload = (
        attack_meta.get("attackPaddingCounts", {}).get(0, 0)
        if isinstance(attack_meta.get("attackPaddingCounts"), dict)
        else 0
    )
    attack_unique_senders = attack_meta.get("attackUniqueSenders", "")

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(left, 48, title_value, 26, 800),
        text(left, 76, f"Baseline sends only normal transactions; the attack run injects a {attack_blocks}-block first-0 Deter-Z2 burst while normal traffic continues.", 13, color="muted"),
        text(left, 98, source_note, 12, color="muted"),
        text(left, 120, f"inclusion source: baseline={baseline_meta['source']}, attack={attack_meta['source']}", 12, color="muted"),
    ]

    out.extend(draw_phase_labels(markers, xscale, 145))
    for panel in (top, bottom):
        out.extend(draw_phase_bands(panel, markers, xscale))
        out.extend(draw_axes(panel, xscale, show_x_labels=(panel is bottom)))

    out.extend(bars(attack_tx, ticks, xscale, top, COLORS["attack_tx"]))
    baseline_points = value_points(baseline_normal, ticks, xscale, top)
    attack_normal_points = value_points(attack_normal, ticks, xscale, top)
    out.append(polyline(baseline_points, COLORS["baseline"], 2.8))
    out.append(circles(baseline_points, COLORS["baseline"], 3.0))
    out.append(polyline(attack_normal_points, COLORS["attack_normal"], 2.8))
    out.append(circles(attack_normal_points, COLORS["attack_normal"], 3.0))
    out.append(legend_item(705, top.y + 25, COLORS["baseline"], "normal included (baseline)"))
    out.append(legend_item(705, top.y + 47, COLORS["attack_normal"], "normal included (attack run)"))
    out.append(legend_item(705, top.y + 69, COLORS["attack_tx"], "attack tx included as bars"))

    attack_total_included = int(sum(attack_tx.values()))
    first_attack_tick = next((tick for tick in ticks if attack_tx.get(tick, 0.0) > 0), None)
    if first_attack_tick is not None and attack_total_included > 0:
        label = (
            f"{attack_total_included} first-0 attack txs included across attack-on"
            if attack_zero_payload == attack_total_included
            else f"{attack_total_included} attack txs included across attack-on"
        )
        out.append(
            annotate(
                xscale.x(first_attack_tick) + 12,
                max(top.y + 42, top.y_value(max(attack_tx.values())) - 12),
                label,
                "attack_tx",
            )
        )
    stall_ticks = [
        tick
        for tick in ticks
        if baseline_normal.get(tick, 0.0) > 0 and attack_normal.get(tick, 0.0) == 0 and phase_by_tick.get(tick) == "attack_on"
    ]
    if stall_ticks:
        out.append(annotate(xscale.x(stall_ticks[0]) + 12, top.y_value(include_max * 0.12), "normal inclusion stalls at attack start", "attack_normal"))

    out.append(area_path(attack_pool, ticks, xscale, bottom, COLORS["attack_pool"], 0.18))
    out.append(area_path(attack_queue, ticks, xscale, bottom, COLORS["attack_queue"], 0.10))
    baseline_pool_points = value_points(baseline_pool, ticks, xscale, bottom)
    attack_pool_points = value_points(attack_pool, ticks, xscale, bottom)
    attack_queue_points = value_points(attack_queue, ticks, xscale, bottom)
    out.append(polyline(baseline_pool_points, COLORS["baseline_pool"], 2.8))
    out.append(circles(baseline_pool_points, COLORS["baseline_pool"], 3.0))
    out.append(polyline(attack_pool_points, COLORS["attack_pool"], 2.9))
    out.append(circles(attack_pool_points, COLORS["attack_pool"], 3.0))
    out.append(polyline(attack_queue_points, COLORS["attack_queue"], 2.2, "6 4"))
    out.append(circles(attack_queue_points, COLORS["attack_queue"], 2.8))
    out.append(legend_item(705, bottom.y + 25, COLORS["baseline_pool"], "baseline pending txpool gas"))
    out.append(legend_item(705, bottom.y + 47, COLORS["attack_pool"], "attack-run pending txpool gas", area=True))
    out.append(legend_item(705, bottom.y + 69, COLORS["attack_queue"], "attack-run queued txpool gas", "6 4", area=True))
    out.append(annotate(xscale.x(8) + 18, bottom.y_value(pool_max * 0.72), "pool gas pressure jumps and remains elevated", "attack_pool"))
    out.append(annotate(xscale.x(14), bottom.y_value(pool_max * 0.07), "baseline drains to zero", "baseline_pool"))

    baseline_total = sum(baseline_normal.values())
    attack_normal_total = sum(attack_normal.values())
    attack_total = sum(attack_tx.values())
    final_pending = as_float(attack_pool.get(max(ticks), 0.0))
    final_queued = as_float(attack_queue.get(max(ticks), 0.0))
    footer_y = 765
    out.append(text(left, footer_y, f"Baseline normal included: {format_number(baseline_total)} txs", 13, 700))
    out.append(text(400, footer_y, f"Attack-run normal included: {format_number(attack_normal_total)} txs", 13, 700))
    out.append(text(730, footer_y, f"Attack included: {format_number(attack_total)} txs", 13, 700))
    if attack_zero_payload:
        out.append(text(left, footer_y + 25, f"First-0 optimization check: {attack_zero_payload} included attack txs used 0-byte payload across {attack_unique_senders} senders.", 12, color="muted"))
    out.append(text(left, footer_y + 47, f"Final attack-run txpool gas estimate: pending {format_number(final_pending)} M gas + queued {format_number(final_queued)} M gas.", 12, color="muted"))
    out.append(text(604, 742, "Aligned phase tick", 13, anchor="middle", color="muted"))
    out.append("</svg>\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")
    write_derived_csv(
        derived_csv_out,
        ticks,
        phase_by_tick,
        baseline_normal,
        attack_normal,
        attack_tx,
        baseline_pool,
        attack_pool,
        attack_queue,
        attack_rows_by_tick,
    )


def main() -> int:
    args = parse_args()
    out_path = Path(args.out)
    derived_csv_out = Path(args.derived_csv_out) if args.derived_csv_out else out_path.with_suffix(".csv")
    render(
        Path(args.baseline_dir),
        Path(args.attack_dir),
        out_path,
        derived_csv_out,
        args.rpc,
        args.title,
    )
    print(out_path)
    print(derived_csv_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
