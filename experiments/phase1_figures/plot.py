"""Paper figures for fixed-fee Phase 1 Deter-Z2 evaluation.

Read the two transferred review archives directly. No raw input is modified.
The output is a five-page PDF plus per-figure vector and 300-DPI raster files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import tarfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

GROUPS = ("G0", "G1", "G2")
CLIENTS = ("Besu", "Erigon")
BLUE = "#2468A0"
DRAIN = "#B54846"
NO_DRAIN = "#D78A24"
TEAL = "#228A79"
PURPLE = "#8D73A9"
INK = "#25313A"
MID = "#58636C"
LIGHT = "#D8DDDF"
FONT = "DejaVu Sans"
OUT_DEFAULT = Path(__file__).resolve().parent / "outputs" / "fixed_seed01"


@dataclass(frozen=True)
class Receipt:
    kind: str
    role: str
    block: int
    fee_wei: int
    gas_used: int
    effective_price_wei: int
    tx_hash: str


def read_json(archive: tarfile.TarFile, name: str):
    stream = archive.extractfile(name)
    if stream is None:
        raise FileNotFoundError(name)
    return json.load(stream)


def read_rows(archive: tarfile.TarFile, name: str) -> list[dict[str, str]]:
    stream = archive.extractfile(name)
    if stream is None:
        raise FileNotFoundError(name)
    with io.TextIOWrapper(stream, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def erigon_role(label: str) -> str:
    match = re.fullmatch(r"a\d+-x(\d+)", label)
    if not match:
        raise ValueError(f"Unexpected Erigon attack label: {label}")
    return "first" if int(match.group(1)) == 1 else "tail"


def parse_receipts(client: str, rows: list[dict[str, str]]) -> list[Receipt]:
    result = []
    seen = set()
    for row in rows:
        block_text = row.get("receiptBlock" if client == "Besu" else "blockNumber", "")
        if not block_text:
            continue
        tx_hash = row["hash"].lower()
        if tx_hash in seen:
            raise AssertionError(f"Duplicate receipt: {client} {tx_hash}")
        seen.add(tx_hash)
        kind = row["kind"]
        assert kind in ("normal", "attack"), (client, kind)
        role = (row.get("role", "") if client == "Besu" else erigon_role(row["label"]) if kind == "attack" else "")
        if kind == "attack":
            assert role in ("first", "tail"), (client, row["label"], role)
        fee = int(row["feeWei"] if client == "Besu" else row["actualPaidFeeWei"])
        gas_used = int(row["gasUsed"])
        effective = int(row["effectiveGasPriceWei"])
        assert fee == gas_used * effective, (client, tx_hash)
        result.append(Receipt(kind, role, int(block_text), fee, gas_used, effective, tx_hash))
    return result


def load_client(client: str, path: Path) -> dict:
    groups = {}
    with tarfile.open(path, "r:gz") as archive:
        config = read_json(archive, "config.json")
        assert config["secondsPerSlot"] == 300 and config["fullWindowBlocks"] == 10
        assert config["normalSenders"] * config["txsPerSender"] == 7040
        assert config["attackSenders"] * config["txsPerSender"] == 16000
        for group in GROUPS:
            if client == "Besu":
                summary = read_json(archive, f"{group}/group_summary.json")
                normal_rows = read_rows(archive, f"{group}/tx_outcomes.csv")
                attack_rows = read_rows(archive, f"{group}/attack_receipts.csv")
                normal = parse_receipts(client, [r for r in normal_rows if r["kind"] == "normal"])
                attack = parse_receipts(client, attack_rows)
                # The two independent Besu exports must agree on receipt hashes.
                from_outcomes = {r.tx_hash for r in parse_receipts(client, [r for r in normal_rows if r["kind"] == "attack"])}
                assert from_outcomes == {r.tx_hash for r in attack}
                anchor = int(summary["windows"]["10"]["startPoint"]["blockNumber"])
                end = int(summary["windows"]["10"]["targetBlock"])
                expected_normal = summary["windowSummary"]["10"]["normal"]
                expected_attack = summary["windowSummary"]["10"]["attack"]
                assert summary["normalGasPriceMode"] == "fixed"
                audit = read_json(archive, f"{group}/layer_audit.json")
                assert audit["evidenceComplete"] and not audit["issues"], (group, audit)
            else:
                summary = read_json(archive, f"{group}/erigon_summary.json")
                rows = read_rows(archive, f"{group}/fee_receipts.csv")
                all_receipts = parse_receipts(client, rows)
                normal = [r for r in all_receipts if r.kind == "normal"]
                attack = [r for r in all_receipts if r.kind == "attack"]
                anchor = int(summary["phaseMarkers"]["T0"]["block"])
                end = int(summary["phaseMarkers"]["window10"]["block"])
                expected_normal = summary["windows"]["10"]["normal"]
                expected_attack = summary["windows"]["10"]["attack"]
            assert end - anchor == 10, (client, group, anchor, end)
            normal = [r for r in normal if anchor < r.block <= end]
            attack = [r for r in attack if anchor < r.block <= end]
            expected_n = (expected_normal["included"] if client == "Besu"
                          else expected_normal["states"]["INCLUDED"])
            expected_a = (expected_attack["included"] if client == "Besu"
                          else expected_attack["states"]["INCLUDED"])
            expected_nf = int(expected_normal["feeWei"] if client == "Besu"
                              else expected_normal["actualPaidFeeWei"])
            expected_af = int(expected_attack["feeWei"] if client == "Besu"
                              else expected_attack["actualPaidFeeWei"])
            assert len(normal) == expected_n and len(attack) == expected_a
            assert sum(r.fee_wei for r in normal) == expected_nf
            assert sum(r.fee_wei for r in attack) == expected_af
            if group != "G0":
                assert len(normal) == 0
            groups[group] = {
                "summary": summary, "anchor": anchor, "end": end,
                "normal": normal, "attack": attack,
            }
        if client == "Besu":
            groups["mechanism"] = load_besu_mechanism(archive, groups)
        else:
            for group in ("G1", "G2"):
                states = groups[group]["summary"]["windows"]["10"]["normal"]["states"]
                assert states["UNKNOWN"] == 7040 and states["POOL_EVICTED"] == 0
    return {"config": config, "groups": groups, "archiveName": path.name,
            "archiveSha256": sha256(path)}


def apply_layer_event(state: dict, paths: dict, row: dict) -> None:
    event = row["event"]
    tx_hash = row["hash"].lower()
    if event == "enter":
        layer = row["targetLayer"] or row["layer"]
        assert layer in ("prioritized", "ready", "sparse")
        assert tx_hash not in state, (tx_hash, event, state.get(tx_hash))
        state[tx_hash] = layer
        if not paths[tx_hash] or paths[tx_hash][-1] != layer:
            paths[tx_hash].append(layer)
    elif event == "leave":
        old = state.pop(tx_hash, None)
        assert old == row["layer"], (tx_hash, event, old, row["layer"])
    elif event == "drop":
        assert tx_hash not in state
        state[tx_hash] = "dropped"
        paths[tx_hash].append("drop")


def load_besu_mechanism(archive: tarfile.TarFile, groups: dict) -> dict:
    summary = groups["G1"]["summary"]
    normal_hashes = {r["hash"].lower() for r in read_rows(archive, "G1/tx_outcomes.csv")
                     if r["kind"] == "normal"}
    assert len(normal_hashes) == 7040
    raw = read_rows(archive, "G1/layer_history.csv")
    events = [r for r in raw if r["hash"].lower() in normal_hashes
              and r["event"] in ("enter", "leave", "drop")]
    events.sort(key=lambda r: int(r["seq"]))
    assert len({int(r["seq"]) for r in events}) == len(events)
    start_ms = int(summary["attackTiming"]["before"]["unixMillis"])
    end_ms = int(summary["attackTiming"]["afterUnixMillis"])
    assert summary["attackTiming"]["before"]["block"] == 8
    assert summary["attackTiming"]["afterBlock"] == 8
    state, paths = {}, defaultdict(list)
    index = 0
    while index < len(events) and int(events[index]["unixMillis"]) <= start_ms:
        apply_layer_event(state, paths, events[index])
        index += 1
    initial = Counter(state.values())
    assert initial == Counter({"prioritized": 5000, "ready": 2040}), initial
    series = [(0.0, initial["prioritized"], initial["ready"], initial["sparse"], 0)]
    max_gap = 0
    while index < len(events) and int(events[index]["unixMillis"]) <= end_ms:
        timestamp = int(events[index]["unixMillis"])
        while index < len(events) and int(events[index]["unixMillis"]) == timestamp:
            apply_layer_event(state, paths, events[index])
            index += 1
        counts = Counter(state.values())
        gap = 7040 - sum(counts.values())
        assert gap >= 0
        max_gap = max(max_gap, gap)
        series.append(((timestamp - start_ms) / 1000,
                       counts["prioritized"], counts["ready"], counts["sparse"],
                       counts["dropped"]))
    assert Counter(state.values()) == Counter({"dropped": 7040})
    assert max_gap <= 1, max_gap
    path_counts = Counter(tuple(paths[h]) for h in normal_hashes)
    expected_paths = {
        ("prioritized", "ready", "sparse", "drop"): 6701,
        ("ready", "sparse", "drop"): 339,
    }
    assert dict(path_counts) == expected_paths, path_counts
    drop_rows = read_rows(archive, "G1/normal_capacity_drops.csv")
    assert len(drop_rows) == 7040
    assert {r["hash"].lower() for r in drop_rows} == normal_hashes
    assert all(r["phase"] == "attack_broadcast" for r in drop_rows)
    drop_ms = sorted(int(r["unixMillis"]) for r in drop_rows)
    assert start_ms <= drop_ms[0] <= drop_ms[-1] <= end_ms
    first_attack_block = min(r.block for r in groups["G1"]["attack"])
    assert first_attack_block == int(summary["firstAttackIncludedBlock"]) == 9
    next_block_ms = int(read_json(archive, "G1/window1_marker.json")["endPoint"]["timestamp"]) * 1000
    assert drop_ms[-1] < next_block_ms
    # G2 must independently show the same final capacity-drop count.
    g2_drop = read_rows(archive, "G2/normal_capacity_drops.csv")
    assert len({r["hash"].lower() for r in g2_drop}) == 7040
    assert summary["normalCapacityEvictedConfirmed"] == 7040
    assert groups["G2"]["summary"]["normalCapacityEvictedConfirmed"] == 7040
    return {
        "series": series, "dropSeconds": [(t - start_ms) / 1000 for t in drop_ms],
        "duration": (end_ms - start_ms) / 1000, "initial": dict(initial),
        "pathCounts": {"prioritized_ready_sparse_drop": 6701,
                       "ready_sparse_drop": 339},
        "maxBetweenEvents": max_gap, "firstAttackIncludedBlock": first_attack_block,
        "dropBeforeFirstAttackInclusion": True,
    }


def by_block(data: dict, group: str, kind: str, attribute: str) -> list[int]:
    receipts = data["groups"][group][kind]
    anchor = data["groups"][group]["anchor"]
    result = [0] * 10
    for receipt in receipts:
        result[receipt.block - anchor - 1] += getattr(receipt, attribute)
    return result


def set_style():
    plt.rcParams.update({
        "font.family": FONT, "font.size": 8.3,
        "axes.titlesize": 8.7, "axes.labelsize": 8.3,
        "xtick.labelsize": 7.7, "ytick.labelsize": 7.7,
        "legend.fontsize": 7.7, "lines.linewidth": 1.65,
        "axes.linewidth": 0.75, "pdf.fonttype": 42,
        "svg.fonttype": "none", "savefig.dpi": 300,
        "figure.facecolor": "white", "axes.facecolor": "white",
    })


def format_axis(ax, grid_axis="y"):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(direction="out", length=3, width=0.7, colors=INK)
    ax.grid(axis=grid_axis, color=LIGHT, linewidth=0.55)
    ax.set_axisbelow(True)


def figure_one(data: dict):
    mech = data["Besu"]["groups"]["mechanism"]
    arr = np.array(mech["series"], dtype=float)
    x = arr[:, 0]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7.15, 4.55), sharex=True,
                                   gridspec_kw={"height_ratios": [1.75, 1]},
                                   )
    fig.subplots_adjust(left=0.11, right=0.98, top=0.87, bottom=0.21, hspace=0.14)
    for ax in (ax1, ax2):
        format_axis(ax)
        ax.axvline(mech["duration"], color=MID, ls="--", lw=0.9, zorder=1)
        ax.set_xlim(0, mech["duration"] + 0.4)
    ax1.stackplot(x, arr[:, 1], arr[:, 2], arr[:, 3], step="post",
                  colors=[BLUE, TEAL, PURPLE], alpha=0.86,
                  labels=["Prioritized", "Ready", "Sparse"])
    ax1.set_ylim(0, 7400)
    ax1.set_ylabel("Benign tx in pool layers")
    ax1.legend(ncol=3, loc="upper right", frameon=False, columnspacing=1.1)
    ax1.text(0.02, 0.94, "At injection: 5,000 prioritized; 2,040 ready",
             transform=ax1.transAxes, va="top", color=INK,
             bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.88, "pad": 2})
    xs = [0.0]
    ys = [0]
    counts = Counter(mech["dropSeconds"])
    cumulative = 0
    for t, number in sorted(counts.items()):
        cumulative += number
        xs.append(t)
        ys.append(cumulative)
    xs.append(mech["duration"])
    ys.append(cumulative)
    assert ys[-1] == 7040
    ax2.step(xs, ys, where="post", color=DRAIN, lw=1.9)
    ax2.axvline(mech["dropSeconds"][0], color=MID, ls=":", lw=0.9)
    ax2.set_ylim(0, 7600)
    ax2.set_ylabel("Confirmed capacity evictions")
    ax2.set_xlabel("Seconds since attack broadcast began")
    ax2.annotate(f"First drop: {mech['dropSeconds'][0]:.2f} s",
                 xy=(mech["dropSeconds"][0], 0), xytext=(0.32, 0.45),
                 textcoords="axes fraction", ha="left", va="center",
                 color=INK, arrowprops={"arrowstyle": "-", "lw": 0.7, "color": MID})
    ax2.text(mech["duration"] - 0.12, 7100, "7,040",
             ha="right", va="bottom", color=DRAIN, fontweight="bold")
    ax2.text(mech["duration"] - 0.02, 650, "Broadcast end",
             ha="right", va="bottom", color=MID, rotation=90)
    fig.suptitle("Besu: layered eviction during a single attack broadcast",
                 x=0.02, ha="left", fontsize=10.3, fontweight="bold")
    fig.text(0.02, 0.025,
             "G1 drain, seed01. All 7,040 drops precede the first attack receipt in the next block; "
             "G2 also has 7,040 confirmed drops.\nLayer movement is not final eviction; "
             "the observer has at most one tx between layer events at any instant.",
             fontsize=7.3, color=MID)
    return fig


def figure_two(data: dict):
    fig, axes = plt.subplots(1, 2, figsize=(7.15, 3.55), sharey=True)
    fig.subplots_adjust(left=0.10, right=0.98, top=0.82, bottom=0.35, wspace=0.15)
    for ax, client in zip(axes, CLIENTS):
        counts = [sum(r.block - data[client]["groups"]["G0"]["anchor"] == b
                      for r in data[client]["groups"]["G0"]["normal"]) for b in range(1, 11)]
        baseline = np.r_[0, np.cumsum(counts)]
        attacked_g1 = np.r_[0, np.cumsum([sum(r.block - data[client]["groups"]["G1"]["anchor"] == b
                                      for r in data[client]["groups"]["G1"]["normal"]) for b in range(1, 11)])]
        attacked_g2 = np.r_[0, np.cumsum([sum(r.block - data[client]["groups"]["G2"]["anchor"] == b
                                      for r in data[client]["groups"]["G2"]["normal"]) for b in range(1, 11)])]
        assert np.array_equal(attacked_g1, attacked_g2) and attacked_g1[-1] == 0
        format_axis(ax)
        blocks = np.arange(0, 11)
        ax.plot(blocks, baseline, color=BLUE, marker="o", ms=3.1, label="G0 baseline")
        ax.plot(blocks, attacked_g1, color=DRAIN, ls="--", marker="s", ms=3.0,
                label="G1 drain = G2 no-drain")
        ax.set_xlim(-0.2, 10.35)
        ax.set_ylim(-260, 7550)
        ax.set_xticks([0, 2, 4, 6, 8, 10])
        ax.set_xlabel("Blocks after workload injection")
        ax.set_title(f"({'a' if client == 'Besu' else 'b'}) {client} "
                     f"{'26.6.1' if client == 'Besu' else '3.5.0'}")
        ax.annotate(f"{int(baseline[-1]):,}", xy=(10, baseline[-1]),
                    xytext=(-5, 7), textcoords="offset points",
                    ha="right", color=BLUE, fontweight="bold")
        ax.text(9.7, 460, "G1 = G2 = 0", ha="right", color=DRAIN)
    axes[0].set_ylabel("Cumulative benign tx included")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, bbox_to_anchor=(0.5, 0.15),
               frameon=False)
    fig.suptitle("Impact on benign transaction inclusion", x=0.02, ha="left",
                 fontsize=10.3, fontweight="bold")
    fig.text(0.02, 0.025,
             "Each panel aligns its own block 8 to x=0. Calldata and gas limits differ between clients.\n"
             "Erigon benign state is UNKNOWN: zero inclusion does not establish capacity eviction.",
             fontsize=7.2, color=MID)
    return fig


def figure_three(data: dict):
    fig, axes = plt.subplots(2, 1, figsize=(7.15, 4.5), sharex=True, layout="constrained")
    peak = max(by_block(data[c], g, kind, "fee_wei")[b] / 1e18
               for c in CLIENTS for g, kind in (("G0", "normal"), ("G1", "attack"), ("G2", "attack"))
               for b in range(10))
    for ax, client in zip(axes, CLIENTS):
        format_axis(ax)
        x = np.arange(1, 11)
        y0 = np.array(by_block(data[client], "G0", "normal", "fee_wei")) / 1e18
        y1 = np.array(by_block(data[client], "G1", "attack", "fee_wei")) / 1e18
        y2 = np.array(by_block(data[client], "G2", "attack", "fee_wei")) / 1e18
        ax.plot(x, y0, color=BLUE, marker="o", ms=3.2, label="G0 benign fee")
        ax.plot(x, y1, color=DRAIN, ls="--", marker="^", ms=3.5, label="G1 drain attack fee")
        ax.plot(x, y2, color=NO_DRAIN, ls="-.", marker="s", ms=3.1,
                label="G2 no-drain attack fee")
        for xpos, style, color in ((0, ":", MID), (1, "--", TEAL), (10, ":", MID)):
            ax.axvline(xpos, color=color, ls=style, lw=0.8, alpha=0.9, zorder=0)
        ax.set_xlim(-0.45, 10.42)
        ax.set_ylim(-5, peak * 1.13)
        ax.set_yticks([0, 50, 100, 150, 200, 250])
        ax.set_ylabel("Paid gas fee (ETH/block)")
        ax.set_title(f"({'a' if client == 'Besu' else 'b'}) {client}", loc="left")
        ax.text(0.98, 0.79, "Benign fee in G1/G2: 0 in every block",
                transform=ax.transAxes, ha="right", color=MID,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85, "pad": 1})
    axes[-1].set_xticks([0, 1, 2, 4, 6, 8, 10])
    axes[-1].set_xlabel("Blocks after attack injection")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, ncol=3, loc="lower center", bbox_to_anchor=(0.5, 0.15),
               frameon=False)
    fig.suptitle("Actual paid gas fees over the observation window",
                 x=0.02, ha="left", fontsize=10.3, fontweight="bold")
    fig.text(0.02, -0.08,
             "Event marks: x=0 attack broadcast (block 8); x=1 first attack receipt; "
             "x=10 window end. Fees are receipt gasUsed x effectiveGasPrice.",
             fontsize=7.2, color=MID)
    return fig


def figure_four(data: dict):
    costs = {(c, g): sum(r.fee_wei for r in data[c]["groups"][g]["attack"]) / 1e18
             for c in CLIENTS for g in ("G1", "G2")}
    fig, ax = plt.subplots(figsize=(7.15, 2.75), layout="constrained")
    format_axis(ax, grid_axis="x")
    labels = [("Besu", "G1"), ("Besu", "G2"), ("Erigon", "G1"), ("Erigon", "G2")]
    positions = [3.05, 2.35, 1.15, 0.45]
    for (client, group), y in zip(labels, positions):
        cost = costs[(client, group)]
        ax.barh(y, cost - 1, left=1, height=0.43,
                color=DRAIN if group == "G1" else NO_DRAIN,
                edgecolor=INK, linewidth=0.35)
        ax.text(cost * 1.08, y, f"{cost:,.4f} ETH", va="center", fontsize=8.0, color=INK)
    ax.set_xscale("log")
    ax.set_xlim(1, 7000)
    ax.set_xticks([1, 10, 100, 1000])
    ax.set_xticklabels(["1", "10", "100", "1,000"])
    ax.set_yticks(positions)
    ax.set_yticklabels(["Besu  G1 drain", "Besu  G2 no-drain",
                        "Erigon  G1 drain", "Erigon  G2 no-drain"])
    ax.set_ylim(0, 3.55)
    ax.set_xlabel("Attack gas paid (ETH; logarithmic scale)")
    ax.axhline(1.75, color=LIGHT, lw=0.7)
    for client, y in (("Besu", 2.7), ("Erigon", 0.8)):
        reduction = 100 * (1 - costs[(client, "G1")] / costs[(client, "G2")])
        ax.text(0.98, y / 3.55, f"{reduction:.2f}% lower with drain",
                transform=ax.transAxes, ha="right", va="center", fontsize=7.7,
                color=DRAIN, fontweight="bold",
                bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5})
    fig.suptitle("Balance draining reduces observed attack gas expenditure",
                 x=0.02, ha="left", fontsize=10.3, fontweight="bold")
    fig.text(0.02, -0.07,
             "Fixed fee, seed01; ten blocks; attack workload receipts only; setup excluded. "
             "Private PoS devnet, not a mainnet cost estimate.",
             fontsize=7.1, color=MID)
    return fig


def figure_five(data: dict):
    categories = [(c, g) for c in CLIENTS for g in ("G1", "G2")]
    positions = np.array([0.0, 1.0, 2.7, 3.7])
    first = []
    tail = []
    for client, group in categories:
        receipts = data[client]["groups"][group]["attack"]
        first.append(sum(r.role == "first" for r in receipts))
        tail.append(sum(r.role == "tail" for r in receipts))
    assert first == [125, 125, 125, 125]
    assert tail == [0, 5696, 0, 9875]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7.15, 4.55), sharex=True,
                                   gridspec_kw={"height_ratios": [2.0, 0.8]},
                                   )
    fig.subplots_adjust(left=0.11, right=0.98, top=0.87, bottom=0.21, hspace=0.14)
    for ax in (ax1, ax2):
        format_axis(ax)
        ax.set_xlim(-0.62, 4.35)
        ax.axvline(1.85, color=LIGHT, lw=0.7)
    ax1.bar(positions, first, width=0.58, color=DRAIN, label="First tx included")
    ax1.bar(positions, tail, bottom=first, width=0.58, color=NO_DRAIN,
            label="Tail tx included")
    ax1.set_ylim(0, 11000)
    ax1.set_ylabel("Included attack tx (count)")
    ax1.set_title("Full ten-block window", loc="left")
    for x, f, t in zip(positions, first, tail):
        total = f + t
        ax1.text(x, max(total + 270, 440), f"{total:,}", ha="center", va="bottom",
                 color=INK, fontweight="bold")
        if t:
            ax1.text(x, f + t / 2, f"{t:,} tail", ha="center", va="center",
                     color=INK, fontweight="bold")
    ax1.legend(loc="upper left", ncol=2, frameon=False)
    ax2.bar(positions, first, width=0.58, color=DRAIN)
    ax2.set_ylim(0, 190)
    ax2.set_ylabel("First tx (0-190 zoom)")
    ax2.set_title("First-transaction detail", loc="left")
    for x in positions:
        ax2.text(x, 130, "125", ha="center", va="bottom", color=DRAIN)
    ax2.set_xticks(positions)
    ax2.set_xticklabels(["Besu\nG1 drain", "Besu\nG2 no-drain",
                         "Erigon\nG1 drain", "Erigon\nG2 no-drain"])
    fig.suptitle("Drain prevents tail transactions from being included",
                 x=0.02, ha="left", fontsize=10.3, fontweight="bold")
    fig.text(0.02, -0.065,
             "The lower panel magnifies the same first-tx segment; it is not an additional count. "
             "Only transactions with receipts in blocks 1-10 are shown.",
             fontsize=7.1, color=MID)
    return fig


def audit_data(data: dict) -> dict:
    result = {"cohort": "fixed-fee seed01", "windowBlocks": 10,
              "feeBasis": "receipt gasUsed * effectiveGasPrice",
              "figures": {
                  "1": ["Besu G1/layer_history.csv", "Besu G1/normal_capacity_drops.csv",
                        "Besu G1/tx_outcomes.csv", "Besu G1/window1_marker.json",
                        "Besu G1,G2/layer_audit.json"],
                  "2": ["Besu G0,G1,G2/tx_outcomes.csv",
                        "Erigon G0,G1,G2/fee_receipts.csv"],
                  "3": ["Besu G0,G1,G2/tx_outcomes.csv",
                        "Besu G1,G2/attack_receipts.csv",
                        "Erigon G0,G1,G2/fee_receipts.csv"],
                  "4": ["Besu G1,G2/attack_receipts.csv",
                        "Erigon G1,G2/fee_receipts.csv"],
                  "5": ["Besu G1,G2/attack_receipts.csv",
                        "Erigon G1,G2/fee_receipts.csv"],
              }, "clients": {}}
    for client in CLIENTS:
        client_data = data[client]
        item = {"archive": client_data["archiveName"],
                "archiveSha256": client_data["archiveSha256"],
                "normalCalldataPaddingBytes": client_data["config"]["normalPaddingBytes"],
                "normalGasLimit": client_data["config"]["normalGas"],
                "groups": {}}
        for group in GROUPS:
            g = client_data["groups"][group]
            item["groups"][group] = {
                "anchorBlock": g["anchor"],
                "benignIncluded": len(g["normal"]),
                "attackIncluded": len(g["attack"]),
                "benignPaidFeeWei": str(sum(r.fee_wei for r in g["normal"])),
                "attackPaidFeeWei": str(sum(r.fee_wei for r in g["attack"])),
                "attackFirstIncluded": sum(r.role == "first" for r in g["attack"]),
                "attackTailIncluded": sum(r.role == "tail" for r in g["attack"]),
            }
            if client == "Besu":
                item["groups"][group]["benignCapacityEvictedConfirmed"] = g["summary"]["normalCapacityEvictedConfirmed"]
            else:
                item["groups"][group]["benignUnknownAtBlock10"] = g["summary"]["windows"]["10"]["normal"]["states"]["UNKNOWN"]
        if client == "Besu":
            mech = client_data["groups"]["mechanism"]
            item["mechanism"] = {
                "initialLayers": mech["initial"],
                "paths": mech["pathCounts"],
                "firstDropSeconds": mech["dropSeconds"][0],
                "lastDropSeconds": mech["dropSeconds"][-1],
                "broadcastDurationSeconds": mech["duration"],
                "maxTransientBetweenObserverEvents": mech["maxBetweenEvents"],
                "dropsBeforeFirstAttackInclusion": mech["dropBeforeFirstAttackInclusion"],
            }
        g1 = sum(r.fee_wei for r in client_data["groups"]["G1"]["attack"])
        g2 = sum(r.fee_wei for r in client_data["groups"]["G2"]["attack"])
        item["withinClientDrainFeeReductionPercent"] = 100 * (1 - g1 / g2)
        result["clients"][client] = item
    assert result["clients"]["Besu"]["groups"]["G0"]["benignIncluded"] == 5820
    assert result["clients"]["Erigon"]["groups"]["G0"]["benignIncluded"] == 7040
    return result


def write_derived_series(data: dict, out: Path) -> None:
    with (out / "derived_block_series.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["client", "group", "relative_block", "benign_included",
                         "attack_included", "benign_paid_fee_wei", "attack_paid_fee_wei"])
        for client in CLIENTS:
            for group in GROUPS:
                client_data = data[client]
                n = by_block(client_data, group, "normal", "fee_wei")
                a = by_block(client_data, group, "attack", "fee_wei")
                anchor = client_data["groups"][group]["anchor"]
                for i in range(1, 11):
                    normal_count = sum(r.block - anchor == i for r in client_data["groups"][group]["normal"])
                    attack_count = sum(r.block - anchor == i for r in client_data["groups"][group]["attack"])
                    writer.writerow([client, group, i, normal_count, attack_count, n[i - 1], a[i - 1]])


def save_figure(fig, filename: str, out: Path, review: PdfPages) -> None:
    for ext in ("svg", "pdf", "png"):
        fig.savefig(out / f"{filename}.{ext}", dpi=300 if ext == "png" else None,
                    bbox_inches="tight", pad_inches=0.12, facecolor="white")
    svg_path = out / f"{filename}.svg"
    svg_text = svg_path.read_text(encoding="utf-8")
    svg_path.write_text("\n".join(line.rstrip() for line in svg_text.splitlines()) + "\n",
                        encoding="utf-8")
    review.savefig(fig, bbox_inches="tight", pad_inches=0.12, facecolor="white")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--besu-archive", type=Path, required=True)
    parser.add_argument("--erigon-archive", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    set_style()
    data = {"Besu": load_client("Besu", args.besu_archive),
            "Erigon": load_client("Erigon", args.erigon_archive)}
    audit = audit_data(data)
    (args.out_dir / "data_audit.json").write_text(json.dumps(audit, indent=2) + "\n",
                                                   encoding="utf-8")
    write_derived_series(data, args.out_dir)
    names = [
        "figure_01_layered_eviction",
        "figure_02_benign_inclusion",
        "figure_03_fee_dynamics",
        "figure_04_drain_cost",
        "figure_05_attack_inclusion_breakdown",
    ]
    with PdfPages(args.out_dir / "deter_z2_evaluation_figures_review.pdf",
                  metadata={"Title": "Deter-Z2 Phase 1 Evaluation Figures",
                            "Subject": "Fixed fee seed01; private PoS devnets"}) as review:
        for name, build in zip(names, (figure_one, figure_two, figure_three,
                                        figure_four, figure_five)):
            save_figure(build(data), name, args.out_dir, review)
    print(json.dumps({"output": str(args.out_dir),
                      "files": names,
                      "costReductionPercent": {
                          c: audit["clients"][c]["withinClientDrainFeeReductionPercent"]
                          for c in CLIENTS}}, indent=2))


if __name__ == "__main__":
    main()
