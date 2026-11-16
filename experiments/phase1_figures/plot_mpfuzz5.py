"""Truthful MPFUZZ Figure 5-style fee curves for Deter-Z2 Phase 1.

Only G0 and G1 are plotted. The conventional-spam cost baseline is unavailable
from the transferred archives and is intentionally not replaced by G2.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import tarfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

from plot import by_block, load_client, read_json, read_rows

OUT_DEFAULT = Path(__file__).resolve().parent / "outputs" / "mpfuzz5_fixed_seed01"
EXPECTED_SHA256 = {
    "Besu": "041536090b79cb89a9a248302782162d80458ac609b35e6c949fa5dcbe3271dd",
    "Erigon": "4db1807f6945bfd92813394dc4d632ef40aa8422d031d653e3aa18a55b55d550",
}
BLUE = "#1F67AE"
GREEN = "#269640"
RED = "#C93836"
GREY = "#69727A"
GRID = "#E0E4E7"
BLACK = "#20272E"


def nested_block_limit_fields(value, path="") -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            subpath = f"{path}.{key}" if path else key
            if key.lower() in {"blockgaslimit", "block_gas_limit", "gaslimit", "gas_limit"}:
                found.append(subpath)
            found.extend(nested_block_limit_fields(child, subpath))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(nested_block_limit_fields(child, f"{path}[{index}]"))
    return found


def source_audit(client: str, archive_path: Path, data: dict) -> dict:
    """Audit archived G0 prices, block-header absence, and real event markers."""
    group0 = data["groups"]["G0"]
    group1 = data["groups"]["G1"]
    anchor = group0["anchor"]
    assert group1["anchor"] == anchor == 8
    assert group0["end"] == group1["end"] == 18
    with tarfile.open(archive_path, "r:gz") as tar:
        names = set(tar.getnames())
        baseline_file = ("G0/tx_outcomes.csv" if client == "Besu"
                         else "G0/fee_receipts.csv")
        rows = read_rows(tar, baseline_file)
        meta_files = (["config.json", "G0/group_summary.json", "G0/summary.json"]
                      if client == "Besu" else
                      ["config.json", "G0/erigon_summary.json", "G0/summary.json"])
        block_limit_fields = [f"{name}:{field}"
                              for name in meta_files
                              for field in nested_block_limit_fields(read_json(tar, name))]
        complete_block_files = sorted(name for name in names if re.search(
            r"(?:^|/)(?:full_)?(?:execution_)?blocks?(?:_with_transactions|_headers)?\.(?:json|jsonl|csv)$",
            name, re.IGNORECASE))
    # These archives contain workload receipts, not full blocks or header gas limits.
    assert not block_limit_fields, block_limit_fields
    assert not complete_block_files, complete_block_files
    maximum = [None] * 10
    observed = 0
    for row in rows:
        if row["kind"] != "normal":
            continue
        field = "receiptBlock" if client == "Besu" else "blockNumber"
        if not row.get(field):
            continue
        block = int(row[field])
        if not anchor < block <= group0["end"]:
            continue
        if client == "Besu":
            assert row["signedTxType"] == "legacy-0"
            signed = int(row["signedGasPriceWei"])
        else:
            signed = int(row["configuredGasPriceWei"])
        assert signed == int(row["effectiveGasPriceWei"])
        index = block - anchor - 1
        maximum[index] = max(maximum[index] or 0, signed)
        observed += 1
    assert observed == len(group0["normal"])
    assert {price for price in maximum if price is not None} == {1714285714284}
    first_receipt_block = min(receipt.block for receipt in group1["attack"])
    assert first_receipt_block == 9
    if client == "Besu":
        timing = group1["summary"]["attackTiming"]
        assert timing["before"]["block"] == timing["afterBlock"] == anchor
        assert group1["summary"]["firstAttackIncludedBlock"] == first_receipt_block
        broadcast_end_known = True
    else:
        markers = group1["summary"]["phaseMarkers"]
        assert markers["T3"]["block"] == anchor
        assert markers["T4"]["firstAttackIncludedBlock"] == first_receipt_block
        assert markers["window10"]["block"] == group1["end"]
        broadcast_end_known = True
    return {
        "baselineWorkloadReceiptFile": baseline_file,
        "baselineWorkloadMaximumSignedGasPriceWeiByRelativeBlock": maximum,
        "baselineIncludedWorkloadReceipts": observed,
        "completeG0BlockTransactionFiles": complete_block_files,
        "archivedBlockGasLimitFields": block_limit_fields,
        "injectionBlock": anchor,
        "firstAttackReceiptBlock": first_receipt_block,
        "observationEndBlock": group1["end"],
        "attackBroadcastFinishedWithinInjectionBlock": broadcast_end_known,
        "conventionalSpamCostBaselineComputable": False,
        "spamBaselineMissing": [
            "complete transaction lists for G0 blocks 9-18",
            "actual gasLimit field in each G0 block header",
            "a highest included GasPrice for empty G0 blocks",
        ],
    }


def compute_series(client: str, data: dict) -> dict[str, list[int]]:
    series = {
        "attackedNormalFeeWei": by_block(data, "G1", "normal", "fee_wei"),
        "baselineNormalFeeWei": by_block(data, "G0", "normal", "fee_wei"),
        "attackedAdversarialFeeWei": by_block(data, "G1", "attack", "fee_wei"),
    }
    assert all(len(values) == 10 for values in series.values())
    assert all(value == 0 for value in series["attackedNormalFeeWei"])
    assert series["attackedAdversarialFeeWei"][0] > 0
    assert all(value == 0 for value in series["attackedAdversarialFeeWei"][1:])
    expected = {
        "Besu": {"baselineNormalFeeWei": 1026847542856116009600,
                 "attackedAdversarialFeeWei": 16836499999983163500},
        "Erigon": {"baselineNormalFeeWei": 253439999999746560000,
                   "attackedAdversarialFeeWei": 16836499999983163500},
    }
    for name, value in expected[client].items():
        assert sum(series[name]) == value, (client, name)
    for group, kind, name in (("G1", "normal", "attackedNormalFeeWei"),
                              ("G0", "normal", "baselineNormalFeeWei"),
                              ("G1", "attack", "attackedAdversarialFeeWei")):
        summary = data["groups"][group]["summary"]
        if client == "Besu":
            expected_fee = int(summary["windowSummary"]["10"][kind]["feeWei"])
        else:
            expected_fee = int(summary["windows"]["10"][kind]["actualPaidFeeWei"])
        assert sum(series[name]) == expected_fee, (client, group, kind)
    return series


def set_style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 8.2,
        "axes.titlesize": 10.0, "axes.labelsize": 8.4,
        "xtick.labelsize": 7.7, "ytick.labelsize": 7.7,
        "legend.fontsize": 7.4, "axes.linewidth": 0.7,
        "pdf.fonttype": 42, "svg.fonttype": "none",
        "savefig.dpi": 300, "figure.facecolor": "white",
        "axes.facecolor": "white",
    })


def build_figure(client: str, series: dict[str, list[int]]):
    x = np.arange(1, 11)
    blue = np.asarray(series["attackedNormalFeeWei"], dtype=float) / 1e18
    green = np.asarray(series["baselineNormalFeeWei"], dtype=float) / 1e18
    red = np.asarray(series["attackedAdversarialFeeWei"], dtype=float) / 1e18
    fig, ax = plt.subplots(figsize=(7.15, 4.20))
    fig.subplots_adjust(left=0.105, right=0.98, top=0.88, bottom=0.38)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(BLACK)
    ax.spines["bottom"].set_color(BLACK)
    ax.tick_params(direction="out", width=0.7, length=3, colors=BLACK)
    ax.grid(axis="y", linewidth=0.55, color=GRID)
    ax.set_axisbelow(True)
    for event_x, style in ((0, ":"), (1, "--"), (10, ":")):
        ax.axvline(event_x, color=GREY, lw=0.85, ls=style, zorder=0)
    # Draw the outlined red triangles first so blue circles remain visible at zero.
    ax.plot(x, red, color=RED, marker="^", markersize=7.2,
            markerfacecolor="white", markeredgewidth=1.5,
            linestyle="--", linewidth=1.5, zorder=3,
            label="Adversarial tx fee (G1 Drain)")
    ax.plot(x, blue, color=BLUE, marker="o", markersize=3.8,
            linestyle="-", linewidth=1.15, zorder=4,
            label="Normal tx fee (G1 Drain)")
    ax.plot(x, green, color=GREEN, marker="+", markersize=7.5,
            markeredgewidth=1.5, linestyle="-", linewidth=1.4, zorder=5,
            label="Normal tx fee (G0 Baseline)")
    ax.set_xlim(-0.40, 10.45)
    ax.set_ylim(-4, 132)
    ax.set_xticks(np.arange(0, 11, 1))
    ax.set_yticks([0, 25, 50, 75, 100, 125])
    ax.set_xlabel("Relative block height after attack injection")
    ax.set_ylabel("Actual paid fees (ETH / block)")
    ax.set_title(f"Deter-Z2 fee dynamics on {client} "
                 f"{'26.6.1' if client == 'Besu' else '3.5.0'}",
                 loc="left", fontweight="bold")
    ax.annotate("Injection", xy=(0, 115), xytext=(0.22, 124),
                arrowprops={"arrowstyle": "-", "color": GREY, "lw": 0.7},
                fontsize=7.1, color=GREY, ha="left")
    ax.annotate("First attack receipt", xy=(1, 115), xytext=(1.8, 115),
                arrowprops={"arrowstyle": "-", "color": GREY, "lw": 0.7},
                fontsize=7.1, color=GREY, ha="left")
    ax.text(9.95, 124, "Window end", ha="right", fontsize=7.1, color=GREY)
    ax.annotate("16.84 ETH", xy=(1, red[0]), xytext=(1.55, 25),
                arrowprops={"arrowstyle": "-", "color": RED, "lw": 0.7},
                fontsize=7.1, color=RED)
    ax.text(5.5, 7, "G1 benign: 0 in all ten blocks", color=BLUE,
            ha="center", fontsize=7.3)
    handles, labels = ax.get_legend_handles_labels()
    order = [1, 2, 0]  # Blue, green, red: MPFUZZ's semantic ordering.
    fig.legend([handles[i] for i in order], [labels[i] for i in order],
               loc="lower center", ncol=3, bbox_to_anchor=(0.5, 0.18),
               frameon=False, columnspacing=1.0, handlelength=2.2)
    fig.text(0.02, 0.035,
             "Conventional spam-cost baseline (black in MPFUZZ Fig. 5) omitted: "
             "full G0 blocks and header gas limits were not archived.\n"
             "G2 No-drain is an ablation, not a conventional spam baseline. "
             "G0 and G1 are separate devnets aligned by relative block.",
             fontsize=7.0, color=GREY)
    return fig


def save_figure(fig, stem: str, out: Path, review: PdfPages) -> None:
    for ext in ("svg", "pdf", "png"):
        fig.savefig(out / f"{stem}.{ext}",
                    dpi=300 if ext == "png" else None,
                    bbox_inches="tight", pad_inches=0.12,
                    facecolor="white")
    svg = out / f"{stem}.svg"
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text(
        encoding="utf-8").splitlines()) + "\n", encoding="utf-8")
    review.savefig(fig, bbox_inches="tight", pad_inches=0.12,
                   facecolor="white")
    plt.close(fig)


def write_derived_csv(out: Path, data: dict) -> None:
    with (out / "mpfuzz5_derived_fees_by_block.csv").open(
            "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("client", "relative_block", "G1_normal_paid_fee_wei",
                         "G0_normal_paid_fee_wei", "G1_attack_paid_fee_wei"))
        for client in ("Besu", "Erigon"):
            s = data[client]["series"]
            for index in range(10):
                writer.writerow((client, index + 1,
                                 s["attackedNormalFeeWei"][index],
                                 s["baselineNormalFeeWei"][index],
                                 s["attackedAdversarialFeeWei"][index]))


def write_report(out: Path, data: dict) -> None:
    lines = [
        "# MPFUZZ Figure 5 semantic audit - Deter-Z2 Phase 1 fixed fee seed01",
        "",
        "The [MPFUZZ paper](https://www.usenix.org/system/files/usenixsecurity24-wang-yibo.pdf) "
        "plots benign and adversarial fees of included transactions and a separate "
        "conventional-spam cost estimate. Its Appendix D.1 defines the latter as "
        "the highest included GasPrice in a regular block times that block's gas limit.",
        "",
        "| MPFUZZ curve | Deter-Z2 mapping | Archive source | Status |",
        "| --- | --- | --- | --- |",
        "| Blue: Normal tx fee | G1 benign paid fee in each block | Besu `G1/tx_outcomes.csv`; Erigon `G1/fee_receipts.csv` | Exact from receipt `gasUsed x effectiveGasPrice`; zero in all ten blocks |",
        "| Green: Normal tx fee (Baseline) | G0 benign paid fee in each block | Besu `G0/tx_outcomes.csv`; Erigon `G0/fee_receipts.csv` | Exact from receipt fields |",
        "| Red: Adversarial tx fee | G1 attack paid fee in each block | Besu `G1/attack_receipts.csv`; Erigon `G1/fee_receipts.csv` | Exact from receipt fields |",
        "| Black: Adversarial tx fee (Baseline) | Conventional spam estimate, **not G2** | Would need complete G0 blocks with included transaction GasPrices and each block header `gasLimit` | Unavailable; deliberately omitted |",
        "",
        "The first three curves are not stored as ready-made per-block totals. They are "
        "derived exactly by grouping the archived included receipts by block and summing "
        "`gasUsed x effectiveGasPrice`. The fourth curve cannot be derived faithfully.",
        "",
        "The G0 workload receipts do contain configured/signed legacy GasPrice, but they "
        "are not full block transaction lists. The provided archives contain no block header "
        "gas limits. Erigon G0 has no included workload transactions in relative blocks 4-10, "
        "so even its workload-only maximum GasPrice is undefined there. Transaction gas "
        "limits (`normalGas` / `attackGas`) are not block gas limits. G2 No-drain remains in "
        "the separate cost and first/tail ablation figures.",
        "To complete the black series, export each G0 block with full transactions and its "
        "header `gasLimit` (for example via `eth_getBlockByNumber` with full transactions). "
        "A block with no included transactions has no highest GasPrice; it must remain "
        "undefined rather than be silently set to zero.",
        "",
        "## Observed timing and limits",
        "",
        "- Both group pairs inject within block 8. The first G1 attack receipt is in block 9; the ten-block window ends at block 18. Plot x=0 marks injection *before* the first observed block, while x=1-10 are actual block receipts.",
        "- Attack broadcast completion within block 8 is recorded, but the end of attack impact is not established. No locking, attack-stop, or recovery phase is plotted.",
        "- There is no normal-only pre-attack block series in these paired observations. Erigon has an additional state probe at block 20, but no renewed benign workload that would support a recovery fee curve.",
        "- Consequently, these figures are a post-injection semantic approximation, not a full MPFUZZ attack-lifecycle recreation.",
        "- One shared ETH/block axis is used: observed nonzero series span roughly 16.84-102.85 ETH/block, so a broken axis or dual axis would add distortion without resolving a scale problem.",
        "- Besu uses 8 KB benign calldata and a 150,000 normal transaction gas limit; Erigon uses no extra benign calldata and a 21,000 gas limit. The separate panels are not a client performance ranking.",
        "- Fees are private-devnet paid gas fees, excluding setup; they are not mainnet cost estimates. G0 and G1 are separate devnets aligned only by relative block index.",
        "",
        "## Input checks and sums",
        "",
    ]
    for client in ("Besu", "Erigon"):
        item = data[client]
        s = item["series"]
        lines.extend([
            f"### {client}",
            "",
            f"- Archive SHA-256: `{item['archiveSha256']}`",
            f"- G0 benign total: {sum(s['baselineNormalFeeWei']) / 1e18:.6f} ETH; "
            f"G1 benign total: {sum(s['attackedNormalFeeWei']) / 1e18:.6f} ETH; "
            f"G1 attack total: {sum(s['attackedAdversarialFeeWei']) / 1e18:.6f} ETH.",
            f"- G0 included workload maximum signed GasPrice by relative block (Wei): "
            f"`{item['sourceAudit']['baselineWorkloadMaximumSignedGasPriceWeiByRelativeBlock']}`. "
            "This is a diagnostic, **not** the conventional-spam cost curve.",
            "",
        ])
    lines.extend([
        "## Reproduction",
        "",
        "Run `plot_mpfuzz5.py` with `--besu-archive` and `--erigon-archive`. "
        "Its JSON audit and per-block Wei CSV make every plotted point traceable. "
        "The earlier five-figure suite is preserved in `outputs/fixed_seed01/`.",
        "",
    ])
    (out / "MPFUZZ_FIG5_DATA_AUDIT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--besu-archive", type=Path, required=True)
    parser.add_argument("--erigon-archive", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    set_style()
    data = {}
    for client, archive in (("Besu", args.besu_archive),
                            ("Erigon", args.erigon_archive)):
        loaded = load_client(client, archive)
        assert loaded["archiveSha256"] == EXPECTED_SHA256[client]
        data[client] = {
            "archiveSha256": loaded["archiveSha256"],
            "sourceAudit": source_audit(client, archive, loaded),
            "series": compute_series(client, loaded),
        }
    audit = {
        "cohort": "Phase 1 fixed-fee seed01",
        "window": "relative blocks 1-10 after injection in block 8",
        "metricSemantics": {
            "blue": "G1 benign actual paid fees per block, exact receipt sum",
            "green": "G0 benign actual paid fees per block, exact receipt sum",
            "red": "G1 attack actual paid fees per block, exact receipt sum",
            "black": "conventional spam cost baseline unavailable; no curve plotted",
        },
        "data": data,
    }
    (args.out_dir / "mpfuzz5_data_audit.json").write_text(
        json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    write_derived_csv(args.out_dir, data)
    write_report(args.out_dir, data)
    with PdfPages(args.out_dir / "mpfuzz5_deter_z2_review.pdf",
                  metadata={"Title": "Deter-Z2 Phase 1 - MPFUZZ Figure 5 semantic approximation"}) as review:
        for client in ("Besu", "Erigon"):
            stem = f"mpfuzz5_deter_z2_{client.lower()}_fixed_seed01"
            save_figure(build_figure(client, data[client]["series"]),
                        stem, args.out_dir, review)
    print(json.dumps({
        "output": str(args.out_dir),
        "curves": ["G1 normal actual fee", "G0 normal actual fee", "G1 attack actual fee"],
        "spamBaseline": "unavailable: full G0 blocks and block header gasLimit not archived",
        "files": ["mpfuzz5_deter_z2_besu_fixed_seed01",
                  "mpfuzz5_deter_z2_erigon_fixed_seed01",
                  "mpfuzz5_deter_z2_review.pdf"],
    }, indent=2))


if __name__ == "__main__":
    main()