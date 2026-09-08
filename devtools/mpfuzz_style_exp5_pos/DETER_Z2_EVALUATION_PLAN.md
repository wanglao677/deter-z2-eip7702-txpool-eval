# Deter-Z2 Evaluation Plan

## 1. Goal

This document describes the next-step evaluation plan for the EIP-7702
txpool-eviction attack that we call **Deter-Z2**.

The current artifact already reproduces the attack on Besu and Erigon. The
next goal is to turn the reproduction into a paper-style local evaluation,
following the methodology used by DETER and MPFUZZ:

- compare a regular setup with an attacked setup;
- measure attack effectiveness, not only one successful run;
- report the impact on benign transaction inclusion;
- quantify attacker cost and compare it with a baseline spam attack;
- repeat experiments to measure stability;
- study how the result changes under different parameters.

## 2. Methodology Borrowed From Prior Work

### 2.1 DETER-style local txpool and miner evaluation

DETER evaluates whether crafted transactions can deny a victim node's txpool
service and reduce the number of normal transactions that the miner can include.
The local evaluation idea is:

- set up a victim node;
- send normal transactions to the victim;
- send attack transactions to the victim;
- observe txpool state and block inclusion;
- compare the result with a no-attack baseline;
- repeat the experiment and report success rate and cost.

For Deter-Z2, we adapt this idea as follows:

- the victim node is Besu or Erigon running with its default txpool settings;
- the normal workload fills the victim txpool with benign transactions;
- the attack workload sends high-priced EIP-7702 delegated-account transactions;
- the main observed damage is that pending normal transactions are evicted and
  do not receive receipts during the observation window.

### 2.2 MPFUZZ-style regular vs attacked setup

MPFUZZ's local evaluation compares two setups:

- **Regular setup:** the workload node sends only benign transactions.
- **Attacked setup:** the workload node sends benign transactions and the
  attack node sends crafted adversarial transactions.

For Deter-Z2, we use the same comparison:

- Baseline run: submit normal transactions only.
- Attack run: submit the same normal transactions first, then submit Deter-Z2
  attack transactions.

The comparison should show whether the attack changes:

- how many normal transactions remain in txpool;
- how many normal transactions are included in blocks;
- how much gas/fee the attacker pays;
- whether the attack is cheaper than a naive high-fee spam attack.

### 2.3 Time-phased workload evaluation

The one-shot scale experiment is useful for proving txpool displacement, but a
paper-style evaluation should also measure the attack as a time series. The
time-phased experiment keeps the normal workload schedule aligned between the
baseline and attack runs, and introduces the attack workload only during the
attack-on phase.

Recommended phase structure:

- **Warm-up:** normal transactions are submitted while the txpool is not full.
- **Saturation:** normal transactions continue until the txpool reaches a high
  pressure state.
- **Control / attack-on:** the baseline keeps sending normal transactions only;
  the attack run sends normal transactions and Deter-Z2 attack transactions.
- **Recovery:** the attack workload stops while normal transactions continue,
  measuring whether normal inclusion and txpool admission recover.
- **Drain (optional):** all workload submission stops, measuring how long the
  client takes to clear the txpool.

This structure avoids comparing different normal workloads. The baseline is the
counterfactual run without the attack node, not a different workload. A drain
period can still be measured, but it should be treated as a separate recovery
metric rather than the only baseline behavior.

The phased script writes:

```text
timeseries.csv
phase_markers.csv
metrics.csv
summary.json
commands.md
```

`timeseries.csv` is the input for a figure similar to the prior-work locking
attack plots. Each row records the current phase, observed block height,
transactions submitted in that tick, cumulative submissions, per-block
inclusions, and txpool totals. Optional txpool hash classification can be
enabled for small runs with `--txpool-classification content`.
`phase_markers.csv` records phase start/end blocks for plot annotations.
`commands.md` records the exact reproduction command.

## 3. Attack Summary

Deter-Z2 is an EIP-7702 variant of the latent-overdraft style txpool attack.

The attack uses many EIP-7702 delegated EOAs. Each attack sender is delegated
to a drain implementation contract. The attacker then sends many high-priced
transactions from each delegated sender.

At txpool admission time, these transactions can look acceptable and competitive
because they have higher gas prices than the normal workload. They can therefore
evict lower-priced normal transactions.

At execution time, however, only the first transaction from each attack sender is
expected to succeed. The first transaction drains the delegated EOA's spendable
balance to a receiver account. Later transactions from the same sender no longer
have enough balance to execute successfully or be included.

This produces an asymmetric pattern:

- many attack transactions create txpool pressure;
- normal transactions are evicted before block inclusion;
- only one attack transaction per attack sender is paid on chain;
- the attack cost is much lower than filling the same block space with fully
  executable high-fee spam transactions.

## 4. Research Questions

### RQ1: Txpool eviction effectiveness

How effectively can Deter-Z2 evict pending normal transactions from default Besu
and Erigon txpools?

Primary metric:

- `evictionRate = normalDropped / normalSubmitted`

Expected from current reproduction:

- Besu: 7,040 / 7,040 normal transactions dropped.
- Erigon: 7,040 / 7,040 normal transactions dropped.

### RQ2: Inclusion impact on normal transactions

After normal transactions are evicted from the txpool, how many of them are
included in blocks during the observation window?

Primary metrics:

- `normalReceipts`
- `normalInclusionRate = normalReceipts / normalSubmitted`

Expected from current reproduction:

- Besu: `normalReceipts = 0`.
- Erigon: `normalReceipts = 0`.

The report should say "not included during the observation window", rather than
"never included", because evicted transactions could theoretically be rebroadcast
by another peer or user.

### RQ3: Attack execution asymmetry

Does each attack sender pay for only one successful on-chain transaction while
many of its other transactions are used for txpool pressure?

Primary metrics:

- `attackSubmitted`
- `attackAccepted`
- `attackSuccessfulReceipts`
- `attackSuccessfulReceiptsPerSender`
- `attackPending`
- `attackQueued`
- `attackDropped`
- `attackErrored`

Expected from current reproduction:

- Besu: 125 attack senders; one successful receipt per sender.
- Erigon: 125 attack senders; one successful receipt per sender.

### RQ4: Attack cost

How much ETH does the attack actually cost on chain, and how does that compare
with a naive high-fee spam baseline?

Primary metrics:

- `totalAttackGasUsed`
- `totalAttackCostWei`
- `avgCostPerSuccessfulAttackSender`
- `avgCostPerEvictedNormalTx`
- `baselineSpamCostWei`
- `costRatio = totalAttackCostWei / baselineSpamCostWei`

For an included transaction:

```text
txCostWei = gasUsed * effectiveGasPrice
```

For the naive spam baseline, use a block-level estimate similar to MPFUZZ:

```text
baselineSpamCostWei = highestNormalEffectiveGasPrice * blockGasLimit
```

If the experiment spans multiple blocks, compute the baseline per block and sum
over the same observation window.

### RQ5: Stability

Is the attack result stable across repeated runs?

Primary metrics:

- mean and standard deviation of `evictionRate`;
- mean and standard deviation of `normalInclusionRate`;
- mean and standard deviation of `totalAttackCostWei`;
- number of successful trials.

Recommended minimum:

- 3 baseline trials per client;
- 3 attack trials per client.

For a stronger paper result:

- 5 or 10 trials per client.

### RQ6: Parameter sensitivity

Which parameters are necessary for the attack to succeed, and how do they change
the cost/effectiveness tradeoff?

Parameters to vary:

- number of normal transactions;
- number of normal senders;
- normal transactions per sender;
- number of attack senders;
- attack transactions per sender;
- attack gas price;
- normal gas price;
- calldata padding size;
- block time / slot time;
- post-attack observation window.

Important current observations:

- Besu required larger calldata padding in our successful default-txpool run.
- Erigon did not require calldata padding in the successful run.
- Small workloads, such as 1,000 normal plus 1,000 attack transactions, did not
  necessarily fill the default txpool enough to trigger full eviction.

### RQ7: Time-phased service degradation and recovery

When normal transactions are submitted continuously, does Deter-Z2 only evict a
static set of already-pending transactions, or does it also degrade txpool
service over time?

Primary metrics:

- normal transactions included per block;
- attack transactions included per block;
- txpool pending and queued counts per block;
- normal transaction displacement during the attack-on phase;
- number of blocks needed for normal inclusion to recover after the attack
  workload stops;
- number of blocks needed for the txpool to drain after all workload stops.

Expected output:

- a `timeseries.csv` file for every phased run;
- a block-by-block figure showing the baseline curve and attacked curve;
- vertical markers for warm-up, saturation, attack start, attack end, recovery,
  and optional drain.

### RQ8: Besu first-light cost optimization

Can the Besu attack reduce on-chain cost by making the first transaction from
each attack sender carry no payload or only a small payload, while later
transactions from the same sender keep large calldata padding for txpool
pressure?

Primary metrics:

- `normalDroppedAfterAttack`;
- `normalReceipts`;
- `attackSuccessfulReceipts`;
- `attackSuccessfulReceiptsPerSender`;
- `totalAttackGasUsed`;
- `totalAttackCostWei`;
- `avgCostPerEvictedNormalTx`.

This variant tests whether we can separate the two roles of the attack
transactions:

- the first transaction from each attack sender executes on chain and should be
  cheap;
- later transactions mainly create txpool pressure and should ideally not be
  paid on chain.

## 5. Experimental Setup

### 5.1 Common environment

Use Kurtosis with `ethpandaops/ethereum-package` to run local PoS devnets.

Each run should record:

- client name and version;
- consensus client version;
- chain ID;
- RPC URL;
- block/slot time;
- txpool-related client flags;
- genesis and network parameter file;
- exact command used to start the devnet;
- exact command used to run the experiment.

### 5.2 Besu setup

Execution client:

- Besu with default txpool settings.

Consensus layer:

- Lighthouse beacon node and validator.

Successful initial attack configuration:

```text
normal-count = 7040
normal-senders = 55
normal-txs-per-sender = 128
attack-senders = 125
attack-txs-per-sender = 128
normal-calldata-bytes = 8192
attack-calldata-padding-bytes = 8192
normal-gas = 150000
attack-gas = 500000
```

Current successful result:

```text
normalDropped = 7040
normalReceipts = 0
attackSuccessfulReceipts = 125
attackSuccessfulReceiptsPerSender = {1: 125}
```

### 5.3 Erigon setup

Execution client:

- Erigon with default txpool settings.

Consensus layer:

- Lighthouse beacon node and validator.

Relevant default txpool parameters observed in Erigon source/config:

```text
PendingSubPoolLimit = 10000
BaseFeeSubPoolLimit = 30000
QueuedSubPoolLimit = 30000
AccountSlots = 16
MaxNonceGap = 64
```

Successful initial attack configuration:

```text
normal-count = 7040
normal-senders = 55
normal-txs-per-sender = 128
attack-senders = 125
attack-txs-per-sender = 128
normal-calldata-bytes = 0
attack-calldata-padding-bytes = 0
normal-gas = 21000
attack-gas = 500000
```

Current successful result:

```text
normalDropped = 7040
normalReceipts = 0
attackPending = 10000
attackQueued = 5875
attackErrored = 125
attackSuccessfulReceipts = 125
attackSuccessfulReceiptsPerSender = {1: 125}
```

Note: the observed `attackQueued = 5875` is not the maximum queued capacity. It
is the remainder from this specific workload after Erigon admitted 10,000 attack
transactions into the pending subpool and rejected 125 transactions with
`pending sub-pool is full`.

## 6. Workload Design

### 6.1 Normal workload

The normal workload represents benign users whose transactions should be
included if the txpool is not attacked.

Normal transactions should:

- use normal sender accounts that are disjoint from attack senders;
- use lower gas price than attack transactions;
- have valid nonces and enough balance;
- be submitted before the attack workload;
- be checked in txpool before attack submission.

For the main experiment, reuse:

```text
normal-count = 7040
normal-senders = 55
normal-txs-per-sender = 128
normal-price = 3
```

### 6.2 Attack workload

The attack workload represents adversarial EIP-7702 delegated senders.

Attack setup:

- deploy `Alice7702Drain`;
- fund generated attack senders;
- send EIP-7702 set-code transactions so each attack sender delegates to the
  drain implementation;
- submit high-priced attack transactions from the delegated senders.

For the main experiment, reuse:

```text
attack-senders = 125
attack-txs-per-sender = 128
attacker-price = 7
```

The attack transactions should be sent in round-robin order so that many attack
senders contribute to txpool pressure at the same time.

## 7. Experiment Groups

### Group A: Baseline run

Purpose:

- show that normal transactions can be accepted and included without attack.

Procedure:

1. Start a fresh Besu or Erigon devnet.
2. Ensure the txpool is empty.
3. Fund normal sender accounts.
4. Submit normal transactions only.
5. Record txpool state after submission.
6. Wait for the same observation window used by the attack run.
7. Record normal receipts and block inclusion.

Expected result:

- normal transactions are accepted;
- a substantial number of normal transactions are included;
- no attack transactions are present.

### Group B: Attack run

Purpose:

- show that Deter-Z2 evicts normal transactions and prevents their inclusion
  during the observation window.

Procedure:

1. Start a fresh Besu or Erigon devnet.
2. Ensure the txpool is empty.
3. Deploy the EIP-7702 drain implementation.
4. Fund normal sender accounts.
5. Fund attack sender accounts.
6. Delegate attack senders via EIP-7702 set-code transactions.
7. Wait for setup transactions to be included.
8. Submit normal transactions.
9. Record txpool state after normal submission.
10. Submit attack transactions.
11. Record txpool state after attack submission.
12. Wait for the observation window.
13. Record receipts and block inclusion.
14. Compute metrics.

Expected result:

- normal transactions are evicted from txpool;
- normal receipts are zero or near zero during the observation window;
- each attack sender has one successful on-chain transaction;
- attack cost is much lower than a naive spam baseline.

### Group C: Time-phased baseline run

Purpose:

- measure normal txpool service and block inclusion over time without the
  attack workload;
- provide the counterfactual curve for the attacked run;
- avoid confusing natural multi-block inclusion delay with attack-induced
  eviction.

The normal workload schedule in this group must match Group D. The only
difference is that the attack sender set is disabled.

Recommended phase design:

| Phase | Workload | Goal |
| --- | --- | --- |
| Warm-up | normal tx only | Observe normal admission before txpool saturation. |
| Saturation | normal tx only | Bring the txpool to a high-pressure state. |
| Control | normal tx only | Match the attack-on window without adversarial traffic. |
| Recovery-control | normal tx only | Match the attack-off recovery window. |
| Drain, optional | no workload | Measure natural txpool cleanup after all submissions stop. |

During each phase, record one row per block in `timeseries.csv`.

Important rule:

```text
Baseline normal schedule == Attack normal schedule
```

This rule makes the attack workload the only experimental variable.

### Group D: Time-phased attack run

Purpose:

- measure how normal txpool service changes when Deter-Z2 starts during a
  continuous normal workload;
- measure whether normal inclusion recovers after the attack workload stops;
- generate the block-by-block data needed for a figure similar to the
  time-series plots in prior work.

Recommended phase design:

| Phase | Normal workload | Attack workload | Goal |
| --- | --- | --- | --- |
| Warm-up | on | off | Normal txpool admission before saturation. |
| Saturation | on | off | Build normal txpool pressure before attack. |
| Attack-on | on | on | Measure eviction and inclusion degradation. |
| Recovery | on | off | Measure recovery after attack stops. |
| Drain, optional | off | off | Measure how long the txpool takes to clear. |

The attack should start only after the txpool has already reached the intended
pre-attack pressure level. This makes the causal order clear:

```text
normal workload present -> attack workload starts -> normal txpool service changes
```

The attack run should preserve:

- generated normal sender accounts;
- generated attack sender accounts;
- per-phase submission counts;
- per-block txpool status;
- per-block inclusion counts;
- final receipts and cost metrics.

### Group E: Besu first-light attack variant

Purpose:

- reduce Besu attack cost by lowering the calldata size of the first transaction
  from each attack sender;
- preserve txpool pressure by keeping larger calldata padding on later attack
  transactions.

Variants:

| Variant | First attack tx padding | Later attack tx padding | Purpose |
| --- | ---: | ---: | --- |
| Current Besu attack | 8192 bytes | 8192 bytes | Existing successful baseline. |
| First-light | 0 bytes | 8192 bytes | Lowest-cost candidate. |
| First-small | 512 / 1024 / 2048 bytes | 8192 bytes | Fallback if zero-padding first tx is unstable. |

Success criteria:

- normal transactions are still displaced from the txpool;
- normal receipts remain zero or near zero during the observation window;
- each attack sender still has exactly one successful on-chain transaction;
- total attack cost is lower than the current all-8192-byte attack.

## 8. Metrics

Each run should output these metrics:

```text
client
clientVersion
chainId
mode
trial
normalSubmitted
normalAccepted
normalPendingAfterNormal
normalPendingAfterAttack
normalQueuedAfterAttack
normalDroppedAfterAttack
normalReceipts
normalInclusionRate
evictionRate
attackSubmitted
attackAccepted
attackPendingAfterAttack
attackQueuedAfterAttack
attackDroppedAfterAttack
attackErrored
attackSuccessfulReceipts
attackSuccessfulReceiptsPerSender
totalAttackGasUsed
totalAttackCostWei
avgCostPerSuccessfulAttackSender
avgCostPerEvictedNormalTx
baselineSpamCostWei
costRatio
observationBlocks
observationSeconds
```

For phased runs, also output `timeseries.csv` with one row per observed block:

```text
blockNumber
phase
normalSubmittedThisTick
attackSubmittedThisTick
normalSubmittedTotal
attackSubmittedTotal
normalAcceptedTotal
attackAcceptedTotal
normalIncludedInObservedBlocks
attackIncludedInObservedBlocks
normalIncludedTotal
attackIncludedTotal
txpoolPendingAfterSend
txpoolQueuedAfterSend
txpoolPendingAfterBlock
txpoolQueuedAfterBlock
normalPending
normalQueued
normalDropped
attackPending
attackQueued
attackDropped
```

The `normalPending`, `normalQueued`, `normalDropped`, `attackPending`,
`attackQueued`, and `attackDropped` fields require hash-level txpool
classification. This is expensive for large runs, so the script may leave them
blank for formal runs and rely on aggregate txpool totals plus receipt data.
Small smoke tests should enable hash-level classification to validate the
measurement logic.

Derived formulas:

```text
normalInclusionRate = normalReceipts / normalSubmitted
evictionRate = normalDroppedAfterAttack / normalSubmitted
avgCostPerSuccessfulAttackSender = totalAttackCostWei / attackSuccessfulReceipts
avgCostPerEvictedNormalTx = totalAttackCostWei / normalDroppedAfterAttack
costRatio = totalAttackCostWei / baselineSpamCostWei
```

For phased runs, additional derived metrics:

```text
normalIncludedPerBlock = normalIncludedInObservedBlocks
attackIncludedPerBlock = attackIncludedInObservedBlocks
normalBacklog = normalAcceptedTotal - normalIncludedTotal
attackBacklog = attackAcceptedTotal - attackIncludedTotal
recoveryBlocks = first block after attack end where normal inclusion returns to baseline range
drainBlocks = first block after all workload stops where txpool pending and queued are both zero
```

For large phased runs, final receipt collection should default to included
transactions observed in blocks rather than querying every accepted workload
transaction. This keeps large runs from spending excessive time polling
thousands of transactions that are still pending or were displaced. Full
receipt polling can be enabled when needed for small debugging runs.

## 9. Required Artifacts

For each client and each run, preserve:

```text
run.log
summary.json
metrics.csv
timeseries.csv
phase_markers.csv
normal_accounts.jsonl
attack_accounts.jsonl
normal_records.jsonl
attack_records.jsonl
attack_receipts.json
normal_receipts.json
error_summary.txt
commands.md
```

The run directory naming convention should be:

```text
scale_runs/<client>_<mode>_<trial>_<timestamp>/
```

Examples:

```text
scale_runs/besu_attack_t01_20260830_120000/
scale_runs/besu_baseline_t01_20260830_123000/
scale_runs/erigon_attack_t01_20260830_130000/
scale_runs/erigon_baseline_t01_20260830_133000/
```

## 10. Table Templates

### 10.1 Main result table

| Client | Mode | Normal submitted | Normal dropped | Normal receipts | Attack submitted | Attack successful receipts | Cost ETH | Success |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Besu | Baseline | TBD | N/A | TBD | 0 | 0 | 0 | N/A |
| Besu | Attack | 7040 | 7040 | 0 | 16000 | 125 | TBD | Yes |
| Erigon | Baseline | TBD | N/A | TBD | 0 | 0 | 0 | N/A |
| Erigon | Attack | 7040 | 7040 | 0 | 16000 | 125 | TBD | Yes |

### 10.2 Txpool state table

| Client | Phase | Pending | Queued | Normal pending | Normal dropped | Attack pending | Attack queued | Attack errored |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Besu | after normal | 7040 | 0 | 7040 | 0 | 0 | 0 | 0 |
| Besu | after attack | 15660 | 0 | 0 | 7040 | 15660 | 0 | 0 |
| Erigon | after normal | 7040 | 0 | 7040 | 0 | 0 | 0 | 0 |
| Erigon | after attack | 10000 | 5875 | 0 | 7040 | 10000 | 5875 | 125 |

### 10.3 Stability table

| Client | Mode | Trials | Mean eviction rate | Std eviction rate | Mean normal inclusion rate | Mean cost ETH |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Besu | Attack | TBD | TBD | TBD | TBD | TBD |
| Erigon | Attack | TBD | TBD | TBD | TBD | TBD |

### 10.4 Parameter sensitivity table

| Client | Normal txs | Attack senders | Attack txs/sender | Calldata bytes | Eviction rate | Normal receipts | Cost ETH |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Besu | 1000 | 250 | 4 | 0 | TBD | TBD | TBD |
| Besu | 7040 | 125 | 128 | 8192 | 1.0 | 0 | TBD |
| Erigon | 7040 | 125 | 128 | 0 | 1.0 | 0 | TBD |

## 11. Implementation Plan

### Step 1: Add experiment modes

Extend `mp_exp5_7702_scale_pos.py` with:

```text
--mode baseline
--mode attack
```

Baseline mode should skip:

- deploying `Alice7702Drain`;
- funding attack senders;
- EIP-7702 set-code delegation;
- attack transaction submission.

Attack mode should keep the current behavior.

### Step 2: Write more complete metrics

Extend the script to write:

```text
metrics.csv
normal_receipts.json
error_summary.txt
commands.md
```

The existing `summary.json`, `normal_records.jsonl`, `attack_records.jsonl`,
and `attack_receipts.json` should be preserved.

### Step 3: Add repeated trials

Either add `--trial-id` to the script or write a wrapper script that runs:

```text
besu baseline t01/t02/t03
besu attack t01/t02/t03
erigon baseline t01/t02/t03
erigon attack t01/t02/t03
```

Each trial should use a fresh devnet to avoid leftover txpool state.

### Step 4: Add cost calculation

For each successful attack receipt:

```text
costWei = gasUsed * effectiveGasPrice
```

Sum all successful attack transaction costs.

Also compute:

```text
avgCostPerEvictedNormalTx
baselineSpamCostWei
costRatio
```

### Step 5: Add summary aggregation

Add a small aggregator script that reads all run directories and writes:

```text
evaluation_summary.csv
evaluation_summary.md
```

### Step 6: Add time-phased evaluation

Add or maintain a separate phased experiment script:

```text
mp_exp5_7702_phased_pos.py
```

This script should:

- keep the normal workload schedule identical in baseline and attack runs;
- enable attack transactions only during the attack-on phase;
- sample txpool status once per block;
- count normal and attack transactions included in each observed block;
- write `timeseries.csv`, `metrics.csv`, and `summary.json`.

The first validation should be a small smoke test before running any full-size
experiment.

### Step 7: Add Besu first-light attack variant

Extend the attack sender transaction generator so that the first transaction
from each delegated attack sender can use a different calldata padding size
from later transactions.

Recommended parameters:

```text
--attack-first-calldata-padding-bytes 0
--attack-calldata-padding-bytes 8192
```

This variant should be evaluated against the current all-8192-byte Besu attack
to determine whether the same eviction effect can be achieved with lower
on-chain cost.

## 12. Recommended Next Command-Level Work

The next concrete code task should be:

1. modify `mp_exp5_7702_scale_pos.py` to support `--mode baseline`;
2. add metrics output;
3. run one small baseline and one small attack test on Besu;
4. repeat on Erigon;
5. only after that, run full-size multi-trial experiments.

After the one-shot evaluation is stable, run the phased workflow in this order:

1. Besu phased baseline smoke;
2. Besu phased attack smoke;
3. Erigon phased baseline smoke;
4. Erigon phased attack smoke;
5. Besu phased formal trial;
6. Erigon phased formal trial;
7. Besu first-light cost-optimization trial.

This order avoids wasting hours on large experiments before the output format is
ready for analysis.

## References

- Kai Li, Yibo Wang, and Yuzhe Tang. "DETER: Denial of Ethereum Txpool
  sERvices." ACM CCS 2021. https://doi.org/10.1145/3460120.3485369
- Yibo Wang, Yuzhe Tang, Kai Li, Wanning Ding, and Zhihua Yang. "Understanding
  Ethereum Mempool Security under Asymmetric DoS by Symbolized Stateful
  Fuzzing." USENIX Security 2024.
  https://www.usenix.org/conference/usenixsecurity24/presentation/wang-yibo
