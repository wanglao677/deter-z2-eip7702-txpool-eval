# MPFUZZ Figure 5 semantic audit - Deter-Z2 Phase 1 fixed fee seed01

The [MPFUZZ paper](https://www.usenix.org/system/files/usenixsecurity24-wang-yibo.pdf) plots benign and adversarial fees of included transactions and a separate conventional-spam cost estimate. Its Appendix D.1 defines the latter as the highest included GasPrice in a regular block times that block's gas limit.

| MPFUZZ curve | Deter-Z2 mapping | Archive source | Status |
| --- | --- | --- | --- |
| Blue: Normal tx fee | G1 benign paid fee in each block | Besu `G1/tx_outcomes.csv`; Erigon `G1/fee_receipts.csv` | Exact from receipt `gasUsed x effectiveGasPrice`; zero in all ten blocks |
| Green: Normal tx fee (Baseline) | G0 benign paid fee in each block | Besu `G0/tx_outcomes.csv`; Erigon `G0/fee_receipts.csv` | Exact from receipt fields |
| Red: Adversarial tx fee | G1 attack paid fee in each block | Besu `G1/attack_receipts.csv`; Erigon `G1/fee_receipts.csv` | Exact from receipt fields |
| Black: Adversarial tx fee (Baseline) | Conventional spam estimate, **not G2** | Would need complete G0 blocks with included transaction GasPrices and each block header `gasLimit` | Unavailable; deliberately omitted |

The first three curves are not stored as ready-made per-block totals. They are derived exactly by grouping the archived included receipts by block and summing `gasUsed x effectiveGasPrice`. The fourth curve cannot be derived faithfully.

The G0 workload receipts do contain configured/signed legacy GasPrice, but they are not full block transaction lists. The provided archives contain no block header gas limits. Erigon G0 has no included workload transactions in relative blocks 4-10, so even its workload-only maximum GasPrice is undefined there. Transaction gas limits (`normalGas` / `attackGas`) are not block gas limits. G2 No-drain remains in the separate cost and first/tail ablation figures.
To complete the black series, export each G0 block with full transactions and its header `gasLimit` (for example via `eth_getBlockByNumber` with full transactions). A block with no included transactions has no highest GasPrice; it must remain undefined rather than be silently set to zero.

## Observed timing and limits

- Both group pairs inject within block 8. The first G1 attack receipt is in block 9; the ten-block window ends at block 18. Plot x=0 marks injection *before* the first observed block, while x=1-10 are actual block receipts.
- Attack broadcast completion within block 8 is recorded, but the end of attack impact is not established. No locking, attack-stop, or recovery phase is plotted.
- There is no normal-only pre-attack block series in these paired observations. Erigon has an additional state probe at block 20, but no renewed benign workload that would support a recovery fee curve.
- Consequently, these figures are a post-injection semantic approximation, not a full MPFUZZ attack-lifecycle recreation.
- One shared ETH/block axis is used: observed nonzero series span roughly 16.84-102.85 ETH/block, so a broken axis or dual axis would add distortion without resolving a scale problem.
- Besu uses 8 KB benign calldata and a 150,000 normal transaction gas limit; Erigon uses no extra benign calldata and a 21,000 gas limit. The separate panels are not a client performance ranking.
- Fees are private-devnet paid gas fees, excluding setup; they are not mainnet cost estimates. G0 and G1 are separate devnets aligned only by relative block index.

## Input checks and sums

### Besu

- Archive SHA-256: `041536090b79cb89a9a248302782162d80458ac609b35e6c949fa5dcbe3271dd`
- G0 benign total: 1026.847543 ETH; G1 benign total: 0.000000 ETH; G1 attack total: 16.836500 ETH.
- G0 included workload maximum signed GasPrice by relative block (Wei): `[1714285714284, 1714285714284, 1714285714284, 1714285714284, 1714285714284, 1714285714284, 1714285714284, 1714285714284, 1714285714284, 1714285714284]`. This is a diagnostic, **not** the conventional-spam cost curve.

### Erigon

- Archive SHA-256: `4db1807f6945bfd92813394dc4d632ef40aa8422d031d653e3aa18a55b55d550`
- G0 benign total: 253.440000 ETH; G1 benign total: 0.000000 ETH; G1 attack total: 16.836500 ETH.
- G0 included workload maximum signed GasPrice by relative block (Wei): `[1714285714284, 1714285714284, 1714285714284, None, None, None, None, None, None, None]`. This is a diagnostic, **not** the conventional-spam cost curve.

## Reproduction

Run `plot_mpfuzz5.py` with `--besu-archive` and `--erigon-archive`. Its JSON audit and per-block Wei CSV make every plotted point traceable. The earlier five-figure suite is preserved in `outputs/fixed_seed01/`.
