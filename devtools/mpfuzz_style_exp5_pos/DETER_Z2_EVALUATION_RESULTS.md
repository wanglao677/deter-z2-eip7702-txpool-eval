# Local Evaluation of Deter-Z2 Against Besu and Erigon

## Abstract

This report presents a local evaluation of Deter-Z2, an EIP-7702-based
transaction-pool eviction attack against Ethereum execution clients. We evaluate
the attack on Besu and Erigon using controlled baseline and attack experiments.
In each experiment, 55 benign accounts submit 7,040 normal transactions. In the
attack configuration, 125 EIP-7702 delegated accounts subsequently attempt to
submit 16,000 higher-priced attack transactions.

In the first formal trial, both Besu and Erigon admit the complete benign
workload, and all 7,040 normal transactions are eventually included in the
corresponding baseline experiments. Under Deter-Z2, all 7,040 previously
admitted normal transactions disappear from the victim transaction pool, and none
receive a receipt within the measured attack observation window. Meanwhile, only
125 attack transactions successfully execute on chain, exactly one per delegated
attack sender, while 15,660 attack transactions remain in Besu's transaction
pool and 15,875 remain in Erigon's.

These results demonstrate a strong execution asymmetry: a small number of
successful on-chain attack executions supports a substantially larger
adversarial transaction-pool footprint. The measured pool-to-successful-execution
ratios are approximately 125.3x for Besu and 127.0x for Erigon. The results
establish the feasibility of Deter-Z2 against both clients under the tested local
configurations. Repeated trials and parameter-sensitivity experiments are still
required to evaluate stability and identify the minimum successful attack
configurations.

## 1. Evaluation Goals

The evaluation addresses the following research questions:

- **RQ1 - Eviction effectiveness:** Can Deter-Z2 remove benign transactions from
  the default transaction pools of Besu and Erigon?
- **RQ2 - Inclusion impact:** Do the evicted benign transactions remain absent
  from blocks during the observation window?
- **RQ3 - Execution asymmetry:** How many attack transactions actually succeed
  on chain compared with the number used to create transaction-pool pressure?
- **RQ4 - Attack workload execution cost:** What on-chain gas cost is paid by
  the mined attack-workload transactions?
- **RQ5 - Cross-client behavior:** How do Besu and Erigon differ in transaction
  admission, eviction, and execution behavior?

## 2. Experimental Setup

### 2.1 Local networks

The experiments use local proof-of-stake networks created with Kurtosis and
`ethpandaops/ethereum-package` version 6.1.0. Each network contains one execution
client, one Lighthouse consensus client, and one validator. A fresh Kurtosis
enclave is used for each formal run so that old blocks, account nonces, and
transaction-pool contents cannot affect a later experiment.

The evaluated execution clients are:

| Client | Version | Chain ID | Consensus client |
| --- | --- | ---: | --- |
| Besu | `besu/v26.6.1/linux-x86_64/openjdk-java-25` | 13371349 | Lighthouse |
| Erigon | `erigon/3.5.0/linux-amd64/go1.25.11` | 13371350 | Lighthouse |

Both clients are evaluated with their default transaction-pool settings in the
local network configuration. The RPC ports are assigned dynamically by Kurtosis
and therefore differ between runs.

### 2.2 Workloads

The baseline and attack experiments use the same benign workload:

| Parameter | Value |
| --- | ---: |
| Normal senders | 55 |
| Transactions per normal sender | 128 |
| Total normal transactions | 7,040 |
| Normal price multiplier | 3 |

The attacked configuration additionally uses:

| Parameter | Value |
| --- | ---: |
| EIP-7702 attack senders | 125 |
| Transactions attempted per attack sender | 128 |
| Total attack transactions attempted | 16,000 |
| Attack price multiplier | 7 |
| Attack transaction gas limit | 500,000 |

The experiment derives the transaction gas price from a local price unit of
`571428571428` wei. The resulting prices are deliberately synthetic and are
used to control transaction-pool ordering in the local experiment. Consequently,
the ETH-denominated costs reported below are comparative local-workload costs,
not estimates of the cost of attacking Ethereum mainnet.

Besu and Erigon use different transaction payloads because the current
client-specific successful configurations target different limiting resources:

| Client | Normal calldata | Attack padding | Normal gas limit | Primary pressure |
| --- | ---: | ---: | ---: | --- |
| Besu | 8,192 bytes | 8,192 bytes | 150,000 | Transaction data size |
| Erigon | 0 bytes | 0 bytes | 21,000 | Pending subpool count |

### 2.3 EIP-7702 attack construction

For an attack run, the experiment performs these steps:

1. Deploy the `Alice7702Drain` implementation contract.
2. Fund the normal sender accounts.
3. Fund the attack sender accounts.
4. Submit EIP-7702 set-code transactions that delegate each attack EOA to
   `Alice7702Drain`.
5. Wait until all setup transactions are included.
6. Submit the 7,040 normal transactions and record the transaction-pool state.
7. Submit the 16,000 higher-priced attack transactions.
8. Locate the normal and attack transactions in the transaction pool.
9. Wait for the configured observation window and collect transaction receipts.

The drain implementation transfers the delegated sender's available balance
during the first successful execution. Later transactions from the same sender
can still contribute to transaction-pool pressure at admission time, but they do
not have sufficient spendable balance to execute successfully afterward.

### 2.4 Baseline construction

The baseline uses a fresh client with the same normal workload but omits contract
deployment, attack-account funding, EIP-7702 delegation, and attack submission.
The observation window is long enough for all normal transactions to be included:

- Besu baseline: 10 post-submission blocks.
- Erigon baseline: 3 post-submission blocks.

The different windows account for the larger Besu transaction payload. They do
not change the pre-inclusion transaction-pool measurement used to verify that all
7,040 normal transactions were initially accepted.

## 3. Metrics

For each run, the experiment records submission results, transaction-pool
locations, receipts, gas use, and transaction cost. The main metrics are:

```text
evictionRate = normalMissingAtTxpoolCheck / normalSubmitted
normalInclusionRate = normalSuccessfulReceipts / normalSubmitted
attackIncludedReceipts = attackSuccessfulReceipts + attackFailedReceipts
attackWorkloadExecutionCostWei =
    sum(gasUsed * effectiveGasPrice) for all mined attack-workload transactions
costPerSuccessfulSender =
    attackWorkloadExecutionCostWei / attackSuccessfulSenders
costPerEvictedNormalTx =
    attackWorkloadExecutionCostWei / normalMissingAtTxpoolCheck
poolAmplification =
    attack transactions remaining in the pool / attackSuccessfulReceipts
```

`normalMissingAtTxpoolCheck` counts normal transactions that were accepted before
the attack but could no longer be found in either the pending or queued subpool
after the attack. The report calls these transactions *evicted* only in the
following sense: they were accepted before the attack, absent from pending and
queued after the attack, and not included before the post-attack snapshot. This
definition separates eviction from ordinary block inclusion. A zero receipt count
means that the transactions were not included during this experiment's
observation window; it does not imply that a user or peer could never rebroadcast
them later.

## 4. Main Results

| Client | Mode | Normal submitted | Normal included | Normal evicted | Eviction rate | Attack attempted | Attack accepted | Attack included | Attack successful | Attack failed | Pool amplification | Attack workload execution cost |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Besu | Baseline | 7,040 | 7,040 | 0 | N/A | 0 | 0 | 0 | 0 | 0 | N/A | 0.0000 ETH |
| Besu | Attack | 7,040 | 0 | 7,040 | 100% | 16,000 | 16,000 | 125 | 125 | 0 | 125.28x | 52.0000 ETH |
| Erigon | Baseline | 7,040 | 7,040 | 0 | N/A | 0 | 0 | 0 | 0 | 0 | N/A | 0.0000 ETH |
| Erigon | Attack | 7,040 | 0 | 7,040 | 100% | 16,000 | 15,875 | 125 | 125 | 0 | 127.00x | 16.8365 ETH |

The baseline establishes that the normal workload is valid and can be processed
by both clients in the absence of an attack. Therefore, the disappearance and
non-inclusion of the normal transactions in the attacked runs cannot be explained
by malformed normal transactions or insufficient normal-account balances.

Under Deter-Z2, the eviction rate is 100% for both clients. All 7,040 normal
transactions are missing from the transaction pool after attack submission, and
no normal transaction receives a receipt during the observation window. This
answers RQ1 and RQ2 affirmatively for the tested configurations.

The inclusion comparison should be read with the observation-window limitation
discussed in Section 9.2. The current baseline runs use client-appropriate full
windows to show that the benign workload can eventually complete, while the
attack runs check the immediate post-attack window. Future trials should also
record a common short inclusion window for both baseline and attack runs.

## 5. Client-Specific Results

### 5.1 Besu

Before the attack, Besu contains all 7,040 normal transactions in its pending
pool. After attack submission, the experiment observes:

```text
normal pending:       0
normal queued:        0
normal evicted:   7,040
attack pending:  15,657
attack queued:        3
attack dropped:     340
attack errored:       0
```

Besu accepts all 16,000 attack submissions. The attack workload then displaces
all normal transactions under the default transaction-pool configuration. Of the
accepted attack transactions, 125 receive successful receipts, exactly one for
each of the 125 attack senders. The remaining attack transactions create pool
pressure without successful on-chain execution during the observation window.

The successful Besu configuration uses 8,192 bytes of calldata in both normal
and attack transactions. This suggests that data-size pressure may be relevant
for reaching the default Besu transaction-pool capacity with this workload. A
parameter-sensitivity experiment is still needed before making a stronger claim
about the minimum required calldata size.

### 5.2 Erigon

Before the attack, Erigon also contains all 7,040 normal transactions in its
pending pool. After attack submission, the experiment observes:

```text
normal pending:       0
normal queued:        0
normal evicted:   7,040
attack pending:  10,000
attack queued:    5,875
attack dropped:       0
attack errored:     125
```

Erigon accepts 15,875 of the 16,000 attack transactions. The other 125
submissions return `pending sub-pool is full`. The experiment observes
saturation at 10,000 pending attack transactions. The 5,875
queued transactions are the remainder admitted from this workload, not evidence
that 5,875 is Erigon's maximum queued capacity.

As with Besu, exactly one transaction per attack sender succeeds, for a total of
125 successful attack receipts. All normal transactions are evicted and none are
included during the observation window. In the tested Erigon configuration,
transaction-count pressure is sufficient to fill the pending subpool without
calldata padding. This does not rule out calldata effects under other Erigon
configurations.

## 6. Execution Asymmetry

The key Deter-Z2 result is the difference between transaction-pool occupancy and
on-chain execution:

| Client | Attack attempted | Present after attack | Successful on chain | Success/attempt ratio | Pool amplification |
| --- | ---: | ---: | ---: | ---: | ---: |
| Besu | 16,000 | 15,660 | 125 | 0.78125% | 125.28x |
| Erigon | 16,000 | 15,875 | 125 | 0.78125% | 127.00x |

Although thousands of attack transactions occupy transaction-pool capacity,
only 0.78125% of the attempted transactions succeed on chain. In both clients,
the successful-receipt distribution is `{1: 125}`, meaning that every one of the
125 delegated senders has exactly one successful transaction. This matches the
intended drain behavior and answers RQ3.

The more important asymmetry is the pool-resident footprint per successful
on-chain attack execution. In this trial, every successful attack transaction
supports approximately 125 to 127 attack transactions that remain in the victim
transaction pool. This makes Deter-Z2 different from ordinary executable spam:
the attacker pays sustained execution cost for only one transaction per delegated
sender while many later transactions from the same senders continue to consume
transaction-pool capacity.

## 7. Cost Analysis

The measured cost below is the attack workload execution cost: the gas cost of
mined attack-workload transactions. In this trial, all mined attack-workload
transactions were successful, so `attackIncludedReceipts`,
`attackSuccessfulReceipts`, and the cost-bearing attack receipts are all 125 for
each client.

| Client | Attack included | Attack successful | Attack failed | Attack gas used | Attack workload execution cost | Cost per successful sender | Cost per evicted normal tx |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Besu | 125 | 125 | 0 | 13,000,000 | 52.0000 ETH | 0.4160 ETH | 0.007386 ETH |
| Erigon | 125 | 125 | 0 | 4,209,125 | 16.8365 ETH | 0.134692 ETH | 0.002392 ETH |

Under the client-specific successful attack configurations, Erigon's measured
attack workload execution cost is approximately 67.6% lower than Besu's. This
difference primarily follows from the lower gas used by the successful Erigon
attack transactions and the absence of large calldata in the Erigon workload.
It should not yet be interpreted as a controlled cross-client cost comparison,
because the successful Besu and Erigon workloads intentionally pressure
different transaction-pool resources.

These values require two qualifications. First, the local gas-price unit is
synthetic, so the ETH amounts should be used for comparison between these runs,
not as a direct mainnet cost estimate. Second, the current metric excludes setup
transactions such as implementation deployment, account funding, and EIP-7702
set-code delegation. A complete end-to-end attacker-cost analysis should report
those setup costs separately and should compare Deter-Z2 with a naive executable
high-fee spam workload over an equivalent observation window.

## 8. Interpretation

The formal trial supports four findings:

1. **Deter-Z2 causes complete local eviction.** Both clients lose all 7,040
   initially accepted normal transactions after the attack workload is admitted.
2. **Eviction affects inclusion.** The baseline includes all normal transactions,
   while the attacked runs include none during the observation window.
3. **The attack has asymmetric execution cost.** Thousands of attack transactions
   create pool pressure, but only one transaction per delegated sender succeeds.
   The measured pool amplification is 125.28x on Besu and 127.00x on Erigon.
4. **The limiting resource is client dependent.** Besu is pressured through large
   transaction payloads, whereas Erigon reaches its pending-subpool transaction
   limit without calldata padding.

The result demonstrates a denial-of-service effect at the local victim node. It
does not by itself prove a network-wide attack: peer-to-peer propagation,
rebroadcast behavior, heterogeneous client configurations, and public-network fee
dynamics are outside the scope of this experiment.

## 9. Threats to Validity

### 9.1 Single formal trial

This report currently contains one baseline and one attack trial for each client.
The runs are complete and internally consistent, but they are not enough to
estimate variance or an empirical success probability. At least three fresh
trials per client and mode should be collected for the paper, with five or ten
trials preferred for stronger stability evidence.

### 9.2 Different observation windows

The Besu baseline waits ten blocks and the Erigon baseline waits three blocks,
while each attack run checks receipts after one block. The pre-attack and
post-attack transaction-pool measurements already demonstrate eviction, but a
paper comparison should additionally report normal inclusion over one common
short window and one client-appropriate full-inclusion window.

### 9.3 Local synthetic fees

The configured gas prices are designed for a deterministic local experiment and
do not represent public-network fee conditions. Mainnet-denominated cost claims
would require replaying the measured gas use against representative historical
base fees and priority fees.

### 9.4 Setup cost excluded

The current attack workload execution cost covers mined attack-workload
transactions only. In this trial, all mined attack-workload transactions were
successful, so this is numerically equal to the cost of successful workload
transactions. Deployment, funding, and set-code transaction costs are not
included. Those costs should be measured separately because some setup actions
may be reusable across attack rounds.

### 9.5 Default local topology

Each experiment uses one execution client and one local consensus client. The
evaluation does not yet measure propagation across multiple execution nodes or
mixed-client networks.

## 10. Reproducibility Artifacts

The combined metrics are stored at:

```text
devtools/mpfuzz_style_exp5_pos/scale_runs/deter_z2_formal_metrics_trial01.csv
```

The paper-friendly summary table is stored at:

```text
devtools/mpfuzz_style_exp5_pos/scale_runs/deter_z2_formal_table_trial01.md
```

The four source run directories are:

```text
scale_runs/besu_baseline_trial01_fullwindow10_20260831_001110/
scale_runs/besu_attack_trial01_20260831_091147/
scale_runs/erigon_baseline_trial01_20260831_100626/
scale_runs/erigon_attack_trial01_20260831_103425/
```

Each run directory contains `metrics.csv` and `summary.json`, together with the
generated account, submission, and receipt artifacts produced by the experiment
script.

## 11. Current Conclusion and Next Steps

The first formal local evaluation reproduces Deter-Z2 on both Besu and Erigon.
For the tested default configurations, the attack evicts 100% of the benign
transactions and suppresses their inclusion during the observation window while
only one attack transaction per delegated sender succeeds on chain.

The next evaluation work should proceed in this order:

1. Add a common short observation-window metric so that baseline and attack
   inclusion can be compared over the same number of blocks.
2. Keep reporting attack included, successful, and failed receipts separately in
   future runs.
3. Repeat the four experiment groups on fresh enclaves for at least three trials.
4. Aggregate mean, standard deviation, and success rate for eviction, inclusion,
   pool amplification, and attack workload execution cost.
5. Measure setup cost and add a naive executable-spam cost baseline.
6. Vary attack senders, transactions per sender, and calldata size to identify
   each client's minimum successful attack configuration.
7. Add a multi-node experiment to study whether peer rebroadcast changes the
   duration of the denial-of-service effect.
