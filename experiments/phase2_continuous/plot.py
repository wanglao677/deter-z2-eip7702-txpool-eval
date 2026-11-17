"""Plot true Phase 2 fees and a labeled G0-derived conventional spam estimate."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def rows(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def numeric(row, field):
    return float(row[field]) if row[field] != "" else math.nan


def curves(root: Path):
    files = {g: root / g / "per_block_metrics.csv" for g in ("G0", "G1")}
    if any(not path.exists() for path in files.values()):
        raise RuntimeError("Figure requires real G0 and G1 per_block_metrics.csv files")
    data = {g: rows(path) for g, path in files.items()}
    if len(data["G0"]) != len(data["G1"]) or not data["G0"]:
        raise RuntimeError("Paired groups have unequal or empty observation windows")
    result = []
    for baseline, attack in zip(data["G0"], data["G1"]):
        if baseline["relativeSlotIndex"] != attack["relativeSlotIndex"]:
            raise RuntimeError("Relative slot alignment differs")
        result.append(dict(relativeSlotIndex=int(baseline["relativeSlotIndex"]),
            g0NormalFeeEth=numeric(baseline, "normalPaidFeeEth") if baseline["blockPresent"] == "True" else math.nan,
            g1NormalFeeEth=numeric(attack, "normalPaidFeeEth") if attack["blockPresent"] == "True" else math.nan,
            g1AttackFeeEth=numeric(attack, "attackPaidFeeEth") if attack["blockPresent"] == "True" else math.nan,
            estimatedConventionalSpamCostEth=numeric(baseline, "estimatedConventionalSpamCostEth")
                if baseline["spamEstimateStatus"] == "ESTIMATED_FROM_G0_BLOCK" else math.nan,
            g0BlockPresent=baseline["blockPresent"], g1BlockPresent=attack["blockPresent"],
            g0BlockNumber=baseline["blockNumber"], g1BlockNumber=attack["blockNumber"]))
    return result


def plot(root: Path, output: Path, broken_left=False):
    root = root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = json.loads((root / "config.json").read_text())
    if not config.get("syntheticFixture", False):
        from run import comparison
        comparison(root)  # Audit raw evidence and matched schedules before rendering a real figure.
    summary_path = root / "phase2_evaluation_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    markers = json.loads((root / "G1" / "phase_markers.json").read_text())
    data = curves(root)
    fixture = bool(config.get("syntheticFixture", False))
    fields = list(data[0])
    with (output / "figure5_curves.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)
    x = [r["relativeSlotIndex"] for r in data]
    blue = [r["g1NormalFeeEth"] for r in data]
    green = [r["g0NormalFeeEth"] for r in data]
    red = [r["g1AttackFeeEth"] for r in data]
    black = [r["estimatedConventionalSpamCostEth"] for r in data]
    if broken_left:
        fig, (top, main) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                        gridspec_kw=dict(height_ratios=[1, 2], hspace=.05))
        finite = sorted(v for series in (blue, green, red) for v in series if math.isfinite(v) and v > 0)
        if len(finite) < 2 or finite[-1] <= finite[0] * 3:
            raise RuntimeError("Broken left axis requested without a meaningful fee range")
        cutoff = finite[len(finite) // 3]
        if finite[-1] <= cutoff * 1.55:
            raise RuntimeError("Broken left axis cannot separate these measured fee bands")
        main.set_ylim(0, cutoff * 1.25)
        top.set_ylim(cutoff * 1.55, max(finite) * 1.1)
        top.spines["bottom"].set_visible(False)
        main.spines["top"].set_visible(False)
        top.tick_params(labelbottom=False)
        for ax in (top, main):
            ax.plot(x, blue, "o-", color="#1F67AE", ms=3, lw=1.3)
            ax.plot(x, green, "+-", color="#269640", ms=5, lw=1.3)
            ax.plot(x, red, "^-", color="#C93836", ms=3, lw=1.3)
        top.text(.01, .95, "Split left axis", transform=top.transAxes, va="top", fontsize=8)
    else:
        fig, main = plt.subplots(figsize=(11, 5))
        main.plot(x, blue, "o-", color="#1F67AE", ms=3, lw=1.3, label="Normal fee — G1 Drain")
        main.plot(x, green, "+-", color="#269640", ms=5, lw=1.3, label="Normal fee — G0 Baseline")
        main.plot(x, red, "^-", color="#C93836", ms=3, lw=1.3, label="Attack fee — G1 Drain")
    right = main.twinx()
    right.plot(x, black, "x-", color="#20272E", ms=4, lw=1.2,
               label="Estimated conventional spam cost (G0)")
    seconds = config["secondsPerSlot"]
    anchor = markers["anchorUnixMillis"]
    injection_start = (markers["injectionWindowStartUnixMillis"] - anchor) / (1000 * seconds)
    injection_end = (markers["injectionEndUnixMillis"] - anchor) / (1000 * seconds)
    for axis in ((top, main) if broken_left else (main,)):
        axis.axvline(injection_start, color="#8C4D12", ls="--", lw=1, label="Attack injection")
        if markers["attackWasBroadcast"]:
            axis.axvline(injection_end, color="#A67C52", ls=":", lw=1, label="Injection end")
        axis.axvline(len(data), color="#69727A", ls="-.", lw=1, label="Observation end")
        axis.grid(alpha=.25)
    recovery = summary.get("recovery", {})
    if recovery.get("status") == "OBSERVED":
        main.axvline(recovery["relativeSlotIndex"], color="#1C8277", ls="--",
                     lw=1, label="Observed recovery")
    main.set_xlim(-.5, len(data) + .5)
    main.set_xlabel("Relative PoS slot (independent devnets aligned to anchor)")
    main.set_ylabel("Measured transaction fees per block (ETH)")
    right.set_ylabel("Estimated conventional spam cost per G0 block (ETH)")
    fig.suptitle(f"Deter-Z2 Phase 2 — {config['name']} ({config['normalGasPriceMode']})" +
                 (" — SYNTHETIC FIXTURE" if fixture else ""))
    left_handles, left_labels = main.get_legend_handles_labels()
    right_handles, right_labels = right.get_legend_handles_labels()
    if broken_left:
        from matplotlib.lines import Line2D
        left_handles = [Line2D([], [], color=c, marker=m, ls="-") for c, m in
                        (("#1F67AE", "o"), ("#269640", "+"), ("#C93836", "^"))] + left_handles
        left_labels = ["Normal fee — G1 Drain", "Normal fee — G0 Baseline",
                       "Attack fee — G1 Drain"] + left_labels
    fig.legend(left_handles + right_handles, left_labels + right_labels,
               loc="upper center", bbox_to_anchor=(.5, .98), ncol=2, fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, .88))
    fig.savefig(output / "figure5_phase2.png", dpi=240)
    fig.savefig(output / "figure5_phase2.pdf")
    plt.close(fig)
    ablation = root / "G2" / "per_block_metrics.csv"
    if ablation.exists():
        g2 = rows(ablation)
        if len(g2) != len(data):
            raise RuntimeError("G2 observation length differs")
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(x, red, "^-", color="#C93836", label="G1 Drain actual attack fee")
        ax.plot(x, [numeric(row, "attackPaidFeeEth") if row["blockPresent"] == "True"
                    else math.nan for row in g2], "s-", color="#D78A24",
                label="G2 No-drain actual attack fee")
        ax.set(xlabel="Relative PoS slot", ylabel="Actual attacker gas fee (ETH)",
               title="Drain / no-drain economic ablation")
        ax.legend()
        ax.grid(alpha=.25)
        fig.tight_layout()
        fig.savefig(output / "drain_ablation.png", dpi=240)
        fig.savefig(output / "drain_ablation.pdf")
        plt.close(fig)
    manifest = dict(sourceRun=str(root), configSha256=sha(root / "config.json"),
                    sources={g: sha(root / g / "per_block_metrics.csv") for g in
                             ("G0", "G1", "G2") if (root / g / "per_block_metrics.csv").exists()},
                    curveColumns=dict(blue="G1.normalPaidFeeEth", green="G0.normalPaidFeeEth",
                        red="G1.attackPaidFeeEth",
                        black="G0.estimatedConventionalSpamCostEth (estimated, not measured)"),
                    missedSlotTreatment="gaps, never interpolated",
                    conventionalSpamAvailableSlots=sum(math.isfinite(v) for v in black),
                    syntheticFixture=fixture)
    (output / "plot_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--broken-left", action="store_true")
    args = parser.parse_args()
    print(json.dumps(plot(args.run, args.out, args.broken_left), indent=2))


if __name__ == "__main__":
    main()
