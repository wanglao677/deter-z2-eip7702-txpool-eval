# Deter-Z2 Phase 1: MPFUZZ Figure 5 semantic approximation

This addition preserves the original five-figure suite in `outputs/fixed_seed01/`. It plots one publication figure each for Besu 26.6.1 and Erigon 3.5.0, using the fixed-fee seed01 archives only. No client, workload, or raw experiment data are modified.

## Reproduce

Use Python 3 with NumPy and Matplotlib. From the repository root:

```bash
python experiments/phase1_figures/plot_mpfuzz5.py \
  --besu-archive '/path/to/besu26-phase1-seed01-review.tar (2).gz' \
  --erigon-archive '/path/to/erigon35-phase1-seed01-review.tar.gz'
```

The input SHA-256 hashes are pinned in the script. The default output is `experiments/phase1_figures/outputs/mpfuzz5_fixed_seed01/`; `--out-dir` changes it. Outputs include two one-page figures (each SVG, vector PDF, 300 DPI PNG), a two-page review PDF, a source/semantics audit in Markdown and JSON, and an exact-Wei fee series CSV.

## Semantic correspondence

| MPFUZZ Figure 5 quantity | Deter-Z2 plotted quantity | Status |
| --- | --- | --- |
| Normal tx fee | G1 benign actual paid fees per block | Exact receipt sum (zero here) |
| Normal tx fee (Baseline) | G0 benign actual paid fees per block | Exact receipt sum |
| Adversarial tx fee | G1 attack actual paid fees per block | Exact receipt sum |
| Adversarial tx fee (Baseline) | Conventional spam cost: highest included GasPrice in the regular block times its block gas limit | **Unavailable**, not plotted |

The archived G0 files contain submitted workload receipts, including signed legacy GasPrice. They do not contain complete baseline block transactions or actual block-header gas limits. For Erigon, later baseline blocks have no included workload transaction from which even a workload-only maximum price could be selected. G2 No-drain is an ablation of the drain mechanism and is intentionally excluded from the main Figure 5-style plots. Its results remain in the older cost and first/tail figures.

`MPFUZZ_FIG5_DATA_AUDIT.md` provides the exact per-curve source files, timeline evidence, and limitations. `mpfuzz5_derived_fees_by_block.csv` records the three plotted values in Wei for each of ten relative blocks. The script checks every included receipt against `gasUsed * effectiveGasPrice` through the existing archive loader, recomputes per-block sums, verifies group totals, and checks that the event markers match archived block numbers.

The paired runs are separate private devnets. Relative block zero is the injection block (absolute height 8 in each run); plotted receipt data begin at relative block one. There is no pre-attack normal-only series or measured recovery fee series. The figures do not estimate mainnet costs or rank the clients because their benign calldata and gas limits differ.