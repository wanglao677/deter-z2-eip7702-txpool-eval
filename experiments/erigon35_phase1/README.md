# Erigon 3.5.0 Deter-Z2 Phase 1 Evaluation

Branch: **erigon35-phase1-evaluation**. Implementation commit: **283ecf0bf2015b51c476583d3dc740da2c735a0c**. The README is a subsequent documentation commit on that branch; use git rev-parse HEAD to record the exact checkout used on Ubuntu.

This adapter keeps the existing Besu Phase 1 runner and the historical Erigon PoC. It runs three independent Erigon 3.5.0 + Lighthouse + Kurtosis private PoS networks. No Erigon source patch or pool-capacity override is used. See [COMPATIBILITY.md](COMPATIBILITY.md) for the audited version defaults and evidence limitations.

## Locked design

| Group | Workload | Delegate |
| --- | --- | --- |
| G0 | 55 normal senders × 128 transactions, no attack workload | Alice7702Drain only during matched setup |
| G1 | Same normal; 125 delegated attack senders × 128 transactions | Alice7702Drain for the whole attack sequence |
| G2 | Same normal and attack counts, nonce order, prices, gas limits, funding and padding | Alice7702NoDrain for the whole attack sequence |

The normal transaction is signed as a legacy type-0 transaction with gas limit 21,000 and 0 extra calldata bytes. The attack is also signed as legacy type 0, gas limit 500,000, 0 extra padding in both first and tail, with the drainAll(address) selector. The first and tail gas price is 7 × 571428571428 Wei; the fixed normal price is 3 × that unit. All groups use the same 300-second slot, account-order normal broadcast, round-robin attack broadcast and 10-block common window from normal broadcast completion. A 12-block follow-up is also recorded. The old Erigon PoC used the same 300-second slot; this is not silently changed to the later 30-second experiments.

The randomized mode changes only normal gas prices: independent deterministic uniform integer Wei in ±10% of the fixed normal price, with a fixed normalFeeRandomSeed. The same ordered fee plan is checked against actual helper records in G0, G1 and G2. The --seed CLI option controls account derivation; it does not replace normalFeeRandomSeed. Never compare the Erigon zero-padding cost directly with Besu's 8 KB workload as if the transaction sizes were matched.

## Ubuntu checkout and prerequisites

Use the existing Ubuntu repository and local experiment key CSV. The CSV is a local test-key file, excluded from Git. Do not put real-network funded keys in it.

~~~bash
cd ~/ETH/besu26-layertrace-eval
git status --short
git fetch origin erigon35-phase1-evaluation
git switch --track -c erigon35-phase1-evaluation origin/erigon35-phase1-evaluation
git rev-parse HEAD

source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
python -c 'import web3, solcx; print("Python dependencies OK")'
python -c 'from solcx import install_solc; install_solc("0.8.20")'

go version
kurtosis version
docker version
docker pull erigontech/erigon:v3.5.0
docker image inspect erigontech/erigon:v3.5.0 --format '{{.Id}} {{json .RepoDigests}}'
docker pull sigp/lighthouse@sha256:44aa773dcf274122c32cc41c4dae7124ed3cab5c267ee82174c6dfd9c688da96
kurtosis engine start
kurtosis enclave ls
ls -l ~/ETH/pos-exp/go-ethereum-pos/devtools/mpfuzz_style_exp5_pos/key_prive_exp5.csv
~~~

If the branch already exists locally, use git switch erigon35-phase1-evaluation followed by git pull --ff-only origin erigon35-phase1-evaluation. The full geth module currently declares Go 1.24.0; use a suitable Go toolchain for helper compilation. The --go CLI option accepts an explicit Go binary. Prepare checks the Erigon image version and the generated --rpc.txfeecap=0 and --txpool.trace.senders flags against the actual image before starting any network. It also compiles both Solidity delegates and the helper.

The existing Erigon PoC YAML used erigontech/erigon:latest. This evaluation uses the versioned erigontech/erigon:v3.5.0 image. Prepare records its actual image ID and repository digest in the runtime manifest. The Lighthouse image and ethereum-package commit are pinned in the config.

## Fixed fee: recommended complete matched run

Run init-funder once per checkout. It creates a local, ignored devnet-only key in experiments/erigon35_phase1/.build. No key needs to be pasted into the terminal or chat. Plan and prepare do not start a network.

~~~bash
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
CFG=experiments/erigon35_phase1/config_fixed.json
python experiments/erigon35_phase1/run.py init-funder --config "$CFG"
python experiments/erigon35_phase1/run.py plan --config "$CFG" --seed seed01
python experiments/erigon35_phase1/run.py prepare --config "$CFG" --seed seed01 --go "$(command -v go)"
tmux new -s erigon-phase1
~~~

Inside tmux:

~~~bash
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
CFG=experiments/erigon35_phase1/config_fixed.json
python -u experiments/erigon35_phase1/run.py evaluation \
  --config "$CFG" --group all --seed seed01 --go "$(command -v go)"
~~~

The all command executes G0, G1, G2 in separate enclaves, saving a single comparison root. Detach from tmux with Ctrl-b then d; later use tmux attach -t erigon-phase1. A stopped group enclave does not delete its evidence. On a workload or network failure the runner preserves the enclave and output directory for diagnosis.

For a targeted single-group run, the implemented commands are:

~~~bash
python -u experiments/erigon35_phase1/run.py evaluation --config "$CFG" --group G0 --seed seed01 --go "$(command -v go)"
python -u experiments/erigon35_phase1/run.py evaluation --config "$CFG" --group G1 --seed seed01 --go "$(command -v go)"
python -u experiments/erigon35_phase1/run.py evaluation --config "$CFG" --group G2 --seed seed01 --go "$(command -v go)"
~~~

Each individual command creates its own root; the automatic matched comparison is produced by --group all. Do not run multiple commands on one terminal at once.

## Randomized normal fee mode

~~~bash
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
CFG=experiments/erigon35_phase1/config_random_fee.json
python experiments/erigon35_phase1/run.py plan --config "$CFG" --seed seed01
python experiments/erigon35_phase1/run.py prepare --config "$CFG" --seed seed01 --go "$(command -v go)"
python -u experiments/erigon35_phase1/run.py evaluation \
  --config "$CFG" --group all --seed seed01 --go "$(command -v go)"
~~~

For another deterministic fee sequence, copy config_random_fee.json into the ignored experiments/erigon35_phase1/.build directory and change only name and normalFeeRandomSeed in the copy. Use that same copy for all three groups; run --group all. Changing --seed alone changes account addresses but leaves the normal fee sequence unchanged. The original fixed config and historical PoC remain untouched.

## Pool configuration and evidence checks

The network JSON for a run is saved at the run root. Inspect the exact Erigon CLI arguments and image without printing private files:

~~~bash
RUN=experiments/erigon35_phase1/runs/NAME_FROM_CONSOLE
python - "$RUN" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
network = json.loads((root / "network.json").read_text())
node = network["participants"][0]
print("image:", node["el_image"])
print("pool and RPC flags:", [x.split("=")[0] for x in node["el_extra_params"]])
print("slot seconds:", network["network_params"]["seconds_per_slot"])
for group in ("G0", "G1", "G2"):
    p = root / group / "network_identity.json"
    if p.exists():
        print(group, json.loads(p.read_text()))
PY
~~~

The output of network_identity.json contains an RPC port, container ID and command, but no private key. Erigon 3.5.0 source defaults are Pending 10,000, BaseFee 30,000, Queued 30,000, AccountSlots 16, PriceBump 10%, MaxNonceGap 64. The only txpool flag added by this evaluation is txpool.trace.senders. The rpc.txfeecap=0 flag disables the local RPC fee ceiling; it is not a capacity setting.

At T0, T1, T2, T3, T5 (normal-anchor +10 blocks) and T6 (+12 blocks), the runner stores pool JSON and full built-in trace logs. It also stores a +1-block checkpoint. First attack inclusion T4 is derived from canonical receipts. Erigon's JSON-RPC txpool_content/status reveal Pending and Queued, but omit BaseFee. The report marks global BaseFee count UNAVAILABLE and uses positive per-hash trace events only for cohort BaseFee location. A hash missing from RPC is UNKNOWN unless an admitted hash has an explicit capacity-overflow deletion trace. Explicit submission errors such as "pending sub-pool is full" are REJECTED, not POOL_EVICTED. Ambiguous network/RPC transport errors stop the workload rather than being counted as rejection.

If a checkpoint misses its target block or crosses into the next block during collection, its windowComparable field is false. If the first attack inclusion happens during the attack broadcast, preFirstInclusionCapacityEvictionRate is UNAVAILABLE because no measured pre-inclusion snapshot exists. These labels prevent unsupported "perfect eviction" claims.

## Re-audit and output

After a run, use the root printed by the runner:

~~~bash
RUN=experiments/erigon35_phase1/runs/NAME_FROM_CONSOLE
CFG=experiments/erigon35_phase1/config_fixed.json
python experiments/erigon35_phase1/run.py audit --config "$CFG" --out "$RUN"
python -m json.tool "$RUN/erigon_evaluation_summary.json"
python -m json.tool "$RUN/G1/erigon_summary.json"
~~~

For a random-fee run, set CFG to config_random_fee.json. Audit reads saved evidence only and does not restart a network.

Important outputs:

- Root: config.json, network.json, runtime_manifest.json, erigon_evaluation_summary.json.
- Per group: workload_manifest.json, normal_fee_plan.csv/json, normal_timing.json, attack_timing.json when applicable, summary.json, receipts.json, setup_receipts.json, first_attack_balances.json, receiver_balance.json.
- Per group pool evidence: pool_T0/T1.json, pool_T2/T3.json for G1/G2 (pool_G0_end.json for G0), pool_window1/window10/window12.json, erigon_trace_*.log, window*_marker.json.
- Per group metrics: erigon_summary.json, tx_states_window*.csv, fee_receipts.csv. The CSV distinguishes configured gasPrice, effectiveGasPrice, gasUsed, and actualPaidFeeWei.
- Per group private/: local setup account material. Do not upload or share it.

## Lightweight development checks

~~~bash
python -m py_compile experiments/besu26_layertrace/workload.py experiments/erigon35_phase1/*.py
python -m unittest discover -s experiments/erigon35_phase1/tests -v
python -m unittest discover -s experiments/besu26_layertrace/tests -v
~~~

The local tests use synthetic Erigon logs and do not prove real-network attack success. On the development WSL machine the installed Go 1.21 cannot parse the current geth module's Go 1.24 tool block; remote Ubuntu must use a suitable Go toolchain for a fresh helper build. The two Solidity delegates compile with solc 0.8.20 locally.
