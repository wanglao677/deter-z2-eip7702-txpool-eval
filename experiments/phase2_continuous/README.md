# Deter-Z2 Phase 2: continuous normal workload and recovery observation

This directory is independent of Phase 1. It uses Besu 26.6.1 or Erigon 3.5.0
with the same Phase 1 pool policies, EIP-7702 delegates, and **125 x 128 = 16,000**
attack transactions. The first attack transaction has **zero padding**. The
attack helper's signing and send order are unchanged; its records now include
per-transaction attempt and completion timestamps. All RPC submission is
restricted to loopback Kurtosis devnets.

## Design and capacity budget

Each group runs on a fresh, independent PoS devnet. The normal input plan is
generated **once** and its SHA-256 is checked against every group. Arrival
times follow a seeded exponential interarrival process, which gives Poisson
counts per slot and variable within-slot timing. Transactions are assigned to
55 normal senders in global round-robin order; the Phase 1 top-level
`normalSendOrder=account` is retained only for Phase 1 preflight and is not
used by this Phase 2 sender. Each sender's nonce starts at
zero and increases consecutively. The same deterministic Phase 1 fee generator
supplies fixed legacy gas prices or ±10% uniform randomized prices. The signed
gas limit and attack fee do not change between modes.

| Client | Target normal arrivals/300 s slot | Planned seed01 normal count, 50 slots | Max planned normal gas/slot, seed01 | Attack |
|---|---:|---:|---:|---:|
| Besu | 160 | 8,189 | 29.4 M gas limit | 16,000 |
| Erigon | 600 | 29,791 | 14.091 M gas limit | 16,000 |

For the previously observed ~60 M block gas limit, these maxima are below
the configured 60% admission budget (36 M). **The workload rechecks this bound
against the actual genesis block gas limit before setup transactions.** It also
checks each normal sender's conservative cumulative gas-price * gas-limit
funding bound and verifies the actual funded balance after setup. Gas limits
are capacity bounds; actual gas used and paid fees come from receipts.

The observation spans 10 pre-attack slots, 20 attack-observation slots, and
20 post-injection-observation slots. The attack starts once at the slot-10
boundary and is never automatically repeated. Its actual start and end are
recorded; injection end does not imply recovery. The anchor must be fresh relative to the observed block timestamp: under
60 seconds for the original 300-second slot, or under 15 seconds for the
Erigon 60-second timing pilot. A stale anchor aborts before workload submission. The initial anchor slot can
be empty because transactions start after its block is observed. The plan
continues through all three phases. Missed PoS slots receive explicit rows
with no block number and plotting gaps.

The predefined recovery signal is **three consecutive valid post slots** in
which G1 normal included count **and** normal paid fee are each at least 90%
of the corresponding G0 slot. If not met, the result is `NOT_OBSERVED`.
This is an operational signal, not a claim that the txpool is fully cleared.

## Ubuntu setup

Work in the Ubuntu checkout that already has the local key CSV, Python venv,
Docker/Kurtosis and the verified Besu observer image. The local funder is
created by `init-funder`; do not type or transmit a private key. Preserve
local changes before switching branches.

```bash
cd ~/ETH/besu26-layertrace-eval
git fetch origin phase2-continuous-evaluation
git switch --track -c phase2-continuous-evaluation origin/phase2-continuous-evaluation
git log -1 --oneline
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
python -m unittest discover -s experiments/phase2_continuous/tests -v
```

If the local branch already exists, use
`git switch phase2-continuous-evaluation` and
`git pull --ff-only origin phase2-continuous-evaluation` instead.
The local CSV at
`~/ETH/pos-exp/go-ethereum-pos/devtools/mpfuzz_style_exp5_pos/key_prive_exp5.csv`
must exist; it stays off GitHub.

For Besu, if the existing `besu:26.6.1-layertrace` image or its verification
record is absent in this checkout, run the established image checks:

```bash
python experiments/besu26_layertrace/build_image.py
python experiments/besu26_layertrace/verify.py
```

The pinned image must report Besu 26.6.1, and `prepare` checks its exact image
ID and CLI options. Erigon uses `erigontech/erigon:v3.5.0`, Lighthouse uses the
pinned digest, and ethereum-package uses commit
`c4a8e15d40f0965f3728f776d72f1954440ad96b`.

## Plan, prepare, and run

Run Besu **G0 and G1** first; G2 is optional. `plan` is offline and starts
no network. `prepare` builds/checks dependencies and starts no network.
`evaluation` starts and stops only the new enclaves it creates.

```bash
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
python experiments/phase2_continuous/run.py plan --client besu --seed seed01 --fee-mode fixed
python experiments/phase2_continuous/run.py init-funder --client besu
python experiments/phase2_continuous/run.py prepare --client besu --seed seed01 --fee-mode fixed
tmux new -s phase2-besu
python -u experiments/phase2_continuous/run.py evaluation --client besu --group G0,G1 --seed seed01 --fee-mode fixed
```

For Erigon:

```bash
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
docker pull erigontech/erigon:v3.5.0
python experiments/phase2_continuous/run.py plan --client erigon --seed seed01 --fee-mode fixed
python experiments/phase2_continuous/run.py init-funder --client erigon
python experiments/phase2_continuous/run.py prepare --client erigon --seed seed01 --fee-mode fixed
tmux new -s phase2-erigon
python -u experiments/phase2_continuous/run.py evaluation --client erigon --group G0,G1 --seed seed01 --fee-mode fixed
```

With the original 300-second configuration, each group needs at least
50 x 300 s = **4 h 10 min**, plus devnet setup, broadcast and receipt collection. G0/G1 together need well over 8 hours.
Keep the terminal in tmux; use `Ctrl-b d` to detach. A completed group is
stopped but not removed. Failures preserve its enclave and records for
diagnosis, and the runner does not silently resubmit uncertain transactions.

If a group reaches the end of collection but fails while adding setup fees,
keep the saved run directory. After updating the code, validate and finish
the saved summary offline, then append the missing group to that same run:

```bash
RUN=~/ETH/besu26-layertrace-eval/experiments/phase2_continuous/runs/<actual-run-name>
python experiments/phase2_continuous/run.py recover --client besu --group G0 --seed seed01 --fee-mode fixed --out "$RUN"
python experiments/phase2_continuous/run.py audit --client besu --out "$RUN"
python -u experiments/phase2_continuous/run.py evaluation --client besu --group G1 --seed seed01 --fee-mode fixed --out "$RUN"
```

Recovery reads saved blocks, receipts, submission records, setup receipts and
observation markers. It rejects incomplete or inconsistent evidence; it does
not broadcast transactions or restart G0. The failed G0 enclave can be
stopped after recovery because its raw trace was already copied.

For the randomized fee mode, use the same commands with
`--fee-mode randomized-uniform`; the mode applies to the normal transactions
only. The signed normal fee distribution is saved in
`normal_schedule_meta.json`. To append G2 to a completed G0/G1 run, reuse
the **same** client, seed and fee mode:

```bash
RUN=~/ETH/besu26-layertrace-eval/experiments/phase2_continuous/runs/<actual-run-name>
python -u experiments/phase2_continuous/run.py evaluation --client besu --group G2 --seed seed01 --fee-mode fixed --out "$RUN"
```

The runner checks the saved effective config and plan hash before appending.
`--group all` runs all three groups in one command.

## Erigon 60-second timing pilot

The original `config_erigon.json` remains at 300 seconds per slot.
`config_erigon_60s.json` changes only the name and slot duration to 60
seconds. Keep `--config` on every command, including `init-funder`,
`audit`, and any later `evaluation --out` call. G0, G1 and G2 use the
same seeded normal-arrival schedule within this profile. It preserves
600 normal arrivals per slot, 10/20/20 slots, fixed normal fees, the
125 x 128 attack, Erigon 3.5.0 and release-default txpool policy.
Normal submissions therefore average 10 per second. The 50-slot
observation lasts 50 minutes per group, plus setup and collection.

The prior Erigon Phase 1 attack broadcast took 18.24 seconds for 16,000
transactions, which motivates a 60-second initial pilot; it does not
guarantee the Phase 2 sender or node will keep up. Check
`attackBroadcastOverranFirstSlot` in `phase_markers.json` and
`phase2_summary.json`, normal submission lags in
`per_block_metrics.csv`, `normalOutsideObservation`, missed slots
and admission errors before treating the groups as comparable.
The anchor header must be less than 15 seconds old at workload start.
Erigon does not provide Besu's native `layertrace.jsonl`; use its
node log, pool snapshots, receipts, and admission records without
claiming per-layer reconstruction.

This is a timing pilot, not a direct 300-second-slot replication: the
same per-slot arrival count is compressed into one fifth the wall time.

```bash
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
CFG=experiments/phase2_continuous/config_erigon_60s.json
python experiments/phase2_continuous/run.py init-funder --client erigon --config "$CFG"
python experiments/phase2_continuous/run.py plan --client erigon --config "$CFG" --seed seed01 --fee-mode fixed
python experiments/phase2_continuous/run.py prepare --client erigon --config "$CFG" --seed seed01 --fee-mode fixed
# Run G0 formally on the Ubuntu host, inside tmux:
python -u experiments/phase2_continuous/run.py evaluation --client erigon --config "$CFG" --group G0 --seed seed01 --fee-mode fixed
# After checking G0 timing, use its printed run directory for the matched groups:
RUN="$(ls -td experiments/phase2_continuous/runs/phase2-erigon-continuous-60s_fixed_seed01_* | head -n 1)"
python -u experiments/phase2_continuous/run.py evaluation --client erigon --config "$CFG" --group G1,G2 --seed seed01 --fee-mode fixed --out "$RUN"
```

## Evidence, audit, and Figure 5-style plot

A run is saved under
`experiments/phase2_continuous/runs/<config>_<fee-mode>_<seed>_<timestamp>/`.
The root contains `config.json`, `network.json`, `runtime_manifest.json`,
`normal_schedule.jsonl`, `normal_schedule_meta.json`, and
`phase2_evaluation_summary.json`. Each group contains:

| File | Meaning |
|---|---|
| `anchor.json`, `phase_markers.json` | Actual anchor slot/block and injection start/end/observation timestamps |
| `normal.jsonl`, `attack.jsonl` | Per transaction sender, nonce, signed price, hash, signing start, network-send attempt/completion times and errors |
| `pool_snapshots.jsonl` | Available pending/queued counts with snapshot time; not claimed to be a perfect block-boundary snapshot |
| `blocks.jsonl`, `block_receipts.jsonl` | Canonical headers, full included transaction lists and original receipts |
| `transaction_outcomes.jsonl` | Broadcast records joined to receipts, including unresolved transactions at the observation cutoff |
| `per_block_metrics.csv` | One row per PoS slot, with separate slot and height, normal/attack count, gas used, actual paid fees, effective price distributions, submission/admission/rejection counts, broadcast lag, pool availability and phase |
| `phase2_summary.json` | Evidence counts and actual ETH fees from receipts |
| `layertrace.jsonl` (Besu), `erigon.log` (Erigon) | Raw node-side evidence |

Setup gas fees are reported separately in `phase2_summary.json` and do not appear in the per-block attack curve. The fee per included transaction is exactly
`receipt.gasUsed * receipt.effectiveGasPrice`. Priority fee and base-fee burn
are separate fields; total fee is **not** validator revenue. The black
Figure 5-style curve is an **estimated conventional spam cost baseline** from
each actual G0 block's maximum included transaction gasPrice multiplied by
that block's gas limit. It is unavailable for empty/missed G0 blocks and is
never G2's actual cost. G2 cost is an independent ablation figure.

To audit saved raw blocks, receipts, admission records, matched plans and
every CSV value without an RPC:

```bash
python experiments/phase2_continuous/run.py audit --client besu --out "$RUN"
```

For an Erigon run, replace `besu` with `erigon`. To plot **after real G0 and
G1 finish**:

```bash
python experiments/phase2_continuous/plot.py --run "$RUN" --out "$RUN/figures"
```

The plot writes PNG, PDF, plotted source CSV, and a SHA-256 source manifest.
If G2 exists, it also writes a separate drain/no-drain cost comparison.
Use `--broken-left` only if the measured fee range actually warrants a split
axis; otherwise use the standard dual-axis plot. No curve is interpolated
across missed slots. Local tests use short, explicitly marked synthetic
fixtures only and are **not** evaluation results.
