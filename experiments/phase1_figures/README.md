# Deter-Z2 Phase 1 paper figures (fixed fee, seed01)

Five publication figures reconstructed from the completed Besu 26.6.1 and Erigon 3.5.0 private PoS runs. This directory contains only plotting code and derived artifacts; it does not modify the evaluation workload, clients, or source data.

## Inputs and reproduction

Place the two original archives on the machine and pass their paths explicitly. The archives are not committed to Git because they contain experiment data and are large.

| Input archive | SHA-256 |
| --- | --- |
| `besu26-phase1-seed01-review.tar (2).gz` | `041536090b79cb89a9a248302782162d80458ac609b35e6c949fa5dcbe3271dd` |
| `erigon35-phase1-seed01-review.tar.gz` | `4db1807f6945bfd92813394dc4d632ef40aa8422d031d653e3aa18a55b55d550` |

Requirements: Python 3, NumPy, and Matplotlib. From the repository root:

```bash
python experiments/phase1_figures/plot.py \
  --besu-archive '/path/to/besu26-phase1-seed01-review.tar (2).gz' \
  --erigon-archive '/path/to/erigon35-phase1-seed01-review.tar.gz'
```

The default output is `experiments/phase1_figures/outputs/fixed_seed01/`. Use `--out-dir` to choose another directory. The script validates per-transaction fees against `gasUsed * effectiveGasPrice`, recomputes window totals from receipts, checks the Besu trace audit, verifies all 7,040 G1/G2 capacity drops, and checks the expected first/tail split. It writes `data_audit.json` and `derived_block_series.csv` alongside the figures.

## Figure map

| Figure | Main result | Primary data inside archives |
| --- | --- | --- |
| 1. Layered eviction | In Besu G1, 7,040 benign transactions progress through the layered pool and are confirmed dropped before the first attack receipt in the next block. | Besu `G1/layer_history.csv`, `G1/normal_capacity_drops.csv`, `G1/tx_outcomes.csv`, `G1/attack_receipts.csv`, `G1/window1_marker.json`, `G1/layer_audit.json`, `G2/normal_capacity_drops.csv`, `G2/layer_audit.json`, `G1/group_summary.json` |
| 2. Benign inclusion | Besu G0 includes 5,820 benign transactions in ten blocks; Erigon G0 includes 7,040. Both clients' G1/G2 include zero. | Besu `G*/tx_outcomes.csv`, `G*/group_summary.json`; Erigon `G*/fee_receipts.csv`, `G*/erigon_summary.json` |
| 3. Per-block paid fees | The normal baseline and G1/G2 attacker fee series show actual on-chain expenditure by block. | Same receipt exports as Fig. 2, plus Besu `G1,G2/attack_receipts.csv` |
| 4. Drain cost | Besu G1/G2 attack fees: 16.8365 / 2,381.4485 ETH (99.29% lower with drain); Erigon: 16.8365 / 953.0000 ETH (98.23% lower). | Besu `G1,G2/attack_receipts.csv`; Erigon `G1,G2/fee_receipts.csv` |
| 5. First versus tail inclusion | Each G1 includes 125 first transactions and zero tails; G2 includes 5,696 Besu tails or 9,875 Erigon tails. | Same attack receipt exports as Figs. 3–4 |

`G*` means each of G0, G1, and G2. `config.json` from both archives is also checked for the window, transaction counts, and payload configuration. Figure 1 reconstructs the time-resolved states from ordered Besu layer events. Transient exits between layer events are not treated as a fourth pool layer. Figures 2–3 use a separate ten-block relative axis for each run, anchored at its injection block. There is no interpolation of unobserved blocks or seconds. The event line at relative block 0 marks attack broadcast before the first observed block; block 1 is the first attack receipt. Figure 3 fees are sums of actual receipts for each block, not a ten-block total divided equally or a gas-limit estimate.

## Interpretation limits

- Besu's 7,040 benign drops are confirmed capacity evictions by its observer. Erigon has 7,040 benign outcomes marked `UNKNOWN` in both attack groups at the ten-block endpoint, so zero Erigon inclusion alone does not prove a capacity eviction mechanism.
- Besu uses 8 KB normal calldata and a 150,000 normal gas limit; Erigon uses zero extra normal calldata and a 21,000 normal gas limit. The panels are not a throughput or security ranking between clients.
- Costs are actual gas fees paid for included **attack workload** transactions on private devnets, excluding setup and funding. They are not mainnet cost estimates. G1/G2 cost reductions are within-client comparisons.
- The experiment is a one-shot attack. There is no observed post-attack recovery series or fresh benign workload after the attack.
- Figure 1 uses the saved `layer_history.csv` event export, cross-checked with `layer_audit.json` and `normal_capacity_drops.csv`; the raw `layertrace.jsonl` is not required by the plotting script.

## Files

Each `figure_0*.{svg,pdf,png}` contains one figure. SVG/PDF are vector exports; PNGs are saved at 300 DPI. `deter_z2_evaluation_figures_review.pdf` combines one figure per page. The historical `phase1_seed01_evaluation_figures.pdf` is preserved separately.