# Weekly Progress: Phased Evaluation for Deter-Z2 on Besu

截至 2026-09-03，本周主要工作是把 Deter-Z2 的本地实验从原先的
one-shot txpool filling 版本，扩展为更接近论文 evaluation 形式的
phased evaluation。当前结果还应定位为 preliminary result，而不是最终
paper-ready conclusion。

## 1. 本周工作概述

本周完成了以下几项工作：

1. 设计并实现了 phased evaluation workload。
2. 在实验中加入 mainnet-sampled gas fee 模型。
3. 加入 sender-monotonic normal gas fee 策略，避免同一 normal sender 后续
   nonce 的 gas price 下降。
4. 实现 first-0-payload attack optimization，即每个攻击地址的第一笔攻击交易
   使用 0-byte payload。
5. 在 Besu 上完成了一组 phased baseline 和 first-0-payload phased attack
   初步实验。
6. 生成了三类图：
   - paper-like tx fee per block figure；
   - attack effect / txpool gas pressure figure；
   - normal gas fee distribution figure。

## 2. Motivation: 为什么改成 Phased Evaluation

之前的 one-shot scale experiment 适合证明一个核心现象：正常交易先进入
txpool，随后 Deter-Z2 attack transactions 大量进入 txpool，并把正常交易从
pending/queued pool 中挤出。

但是，论文中的 evaluation 通常不是只观察一次 txpool snapshot，而是观察一段
时间序列。例如参考论文里的 locking attack 图，会展示不同 block height 下的
normal transaction fee、adversarial transaction fee，以及攻击开始和结束时系统
行为的变化。

因此，我们将实验扩展为 phased workload，目标是观察：

- normal traffic 持续进入时，Besu 是否能稳定处理正常交易；
- attack burst 开始时，normal transaction inclusion 是否出现 stall；
- attack 停止后，normal traffic 是否恢复；
- workload 停止后，txpool 是否能够自然清空；
- first-0-payload attack 是否能够降低攻击者首笔上链交易的 calldata 成本。

## 3. Phased Evaluation Design

### 3.1 Baseline Run

Baseline run 不发送攻击交易，只发送 normal transactions。它用于提供一个
counterfactual：如果没有 Deter-Z2 attack，Besu 在同样 normal workload 下应该
如何处理交易。

| Phase | Workload | Purpose |
| --- | --- | --- |
| Warmup | 持续发送 normal transactions | 建立正常流量，观察 txpool 未饱和时的处理能力 |
| Saturation | 继续发送 normal transactions | 让 normal backlog 和 txpool pressure 逐步上升 |
| Control | 继续发送 normal transactions，无 attack | 对齐 attack run 中 attack-on 的时间窗口 |
| Recovery | 继续发送 normal transactions，无 attack | 观察没有攻击时系统是否继续稳定处理 normal traffic |
| Drain | 停止发送 normal transactions | 观察 txpool 是否能够自然清空 |

### 3.2 Attack Run

Attack run 和 baseline 使用同样的 normal workload schedule，但在 attack-on
phase 额外注入 Deter-Z2 attack transactions。

| Phase | Workload | Purpose |
| --- | --- | --- |
| Warmup | 持续发送 normal transactions | 和 baseline 对齐，建立正常流量 |
| Saturation | 继续发送 normal transactions | 形成正常 backlog |
| Attack on | normal transactions 继续发送，同时注入 Deter-Z2 attack burst | 观察 normal inclusion 是否被打断，以及攻击交易是否占据 txpool |
| Recovery | attack 停止，normal transactions 继续发送 | 观察攻击停止后 normal inclusion 是否恢复 |
| Drain | normal 和 attack 都停止 | 观察 txpool pressure 是否能够自然清空 |

本周使用的 phased 配置为：

| Parameter | Value |
| --- | ---: |
| Warmup blocks | 2 |
| Saturation blocks | 4 |
| Attack/control blocks | 2 |
| Recovery blocks | 4 |
| Drain blocks | 10 |
| Normal senders | 100 |
| Normal rate per block | 800 |
| Attack senders | 125 |
| Attack rate per block | 8000 |
| Normal calldata | 8192 bytes |
| Normal gas limit | 150000 |
| Attack calldata padding | 8192 bytes |
| First attack calldata padding | 0 bytes |
| Attack gas limit | 500000 |

## 4. Gas Fee Modeling

为了让 normal transaction gas price 更接近真实链上情况，我们加入了
mainnet-sampled gas fee 模型。

### 4.1 真实 gas fee 数据来源

目前整理了三种获取真实 gas fee 数据的方法：

| Method | Data source | Usage |
| --- | --- | --- |
| `eth_feeHistory` | Ethereum JSON-RPC | 获取最近 N 个 mainnet blocks 的 base fee 和 priority fee percentiles |
| Block receipt sampling | Ethereum blocks + receipts | 逐笔统计真实上链交易的 `effectiveGasPrice` |
| Explorer / gas tracker API | Etherscan、Blocknative、GasNow 类服务 | 获取外部聚合的 gas fee 参考值 |

本周采用的是第一种方法：通过 Alchemy mainnet RPC 调用 `eth_feeHistory`，收集
最近 1024 个 mainnet blocks 的 fee history。

生成的数据文件为：

```text
devtools/mpfuzz_style_exp5_pos/fee_data/mainnet_latest_1024_20260901_231327/fee_history.csv
devtools/mpfuzz_style_exp5_pos/fee_data/mainnet_latest_1024_20260901_231327/fee_summary.json
```

### 4.2 本周实验中的 normal gas fee 配置

当前 Besu phased run 使用如下 normal fee 设置：

| Parameter | Value |
| --- | ---: |
| Fee source field | `effectiveP50Gwei` |
| Fee mode | `sampled` |
| Fee sampling strategy | `sender-monotonic` |
| Multiplier | 40 |
| Jitter | 0.20 |
| Floor | 2 gwei |
| Cap | 10 gwei |

`sender-monotonic` 的含义是：同一个 normal sender 的后续 nonce 交易不会使用
比前一笔更低的 gas price。这个约束是为了避免同一 sender 的 nonce queue 因
gas price 下降导致交易排序或 replacement 行为变得不稳定。

## 5. First-0-Payload Attack Optimization

导师提出的一个优化方向是：让每个攻击地址的第一笔交易不带 payload 或只带轻
payload，从而降低真正上链执行的攻击交易成本。

Deter-Z2 的攻击机制中，每个 delegated attack sender 通常只有第一笔交易会成功
执行；第一笔执行后会 drain 掉该 sender 的可用余额，后续交易虽然可以在 txpool
中形成压力，但不一定继续成功执行。因此，降低每个 sender 的第一笔交易 payload
可以直接降低攻击者实际支付的链上执行成本。

本周实现的版本为：

- 每个 attack sender 的第一笔 attack transaction 使用 `0` bytes calldata
  padding；
- 后续 attack transactions 仍使用较大的 calldata padding，用来制造 txpool
  pressure；
- 实验记录中会保存每笔 attack transaction 的 `calldataPaddingBytes`，用于事后
  验证。

本周 Besu attack run 中验证到：

| Metric | Value |
| --- | ---: |
| Included attack transactions | 125 |
| Unique attack senders among included attack txs | 125 |
| Included attack tx label | all `x1` |
| Included attack tx payload padding | all `0` bytes |
| Total attack on-chain cost | about 16.8365 ETH |
| Cost per included attack sender | about 0.134692 ETH |

这说明 first-0-payload optimization 在本次 Besu phased attack run 中已经生效：
125 笔上链攻击交易正好来自 125 个攻击地址各自的首笔 0-payload transaction。

## 6. Preliminary Besu Results

### 6.1 Baseline Result

Baseline run path:

```text
devtools/mpfuzz_style_exp5_pos/scale_runs/besu_baseline_phased_sender_monotonic_m40_j20_floor2_cap10_trial01_20260902_163449
```

Baseline 结果如下：

| Metric | Value |
| --- | ---: |
| Normal submitted | 9600 |
| Normal accepted | 9600 |
| Normal included | 9600 |
| Attack submitted | 0 |
| Attack included | 0 |
| Final txpool pending | 0 |
| Final txpool queued | 0 |

Interpretation:

Baseline 表明，在没有 Deter-Z2 attack 的情况下，Besu 可以稳定处理这组
sender-monotonic sampled-fee normal workload。Normal transactions 在发送阶段
持续进入 txpool，drain phase 后 txpool 能够清空。

### 6.2 First-0-Payload Attack Result

Attack run path:

```text
devtools/mpfuzz_style_exp5_pos/scale_runs/besu_attack_phased_sender_monotonic_m40_j20_floor2_cap10_first0_trial01_20260902_165311
```

Attack 结果如下：

| Metric | Value |
| --- | ---: |
| Normal submitted | 9600 |
| Normal accepted | 9600 |
| Attack submitted | 16000 |
| Attack accepted | 16000 |
| Included attack transactions | 125 |
| Included attack sender count | 125 |
| First attack payload padding | 0 bytes |
| Final txpool pending | 9599 |
| Final txpool queued | 1061 |

需要注意的是，本次 phased run 使用了 `receipt-scope none`，因此 `metrics.csv`
中基于 receipt 的 success/inclusion 字段不能直接作为最终成功率解释。我们额外
通过链上 transaction hash 回查确认，本次 run 中 125 笔 attack included
transactions 都是 first-0-payload transactions。

本次 attack run 的关键现象是：

1. 在 attack-on 的第一个 observed block，normal inclusion 出现 stall。
2. 同一个 block 中有 125 笔 attack transactions 上链。
3. 这 125 笔 attack transactions 分别来自 125 个 attack senders 的首笔
   transaction，且 payload padding 为 0 bytes。
4. Attack burst 之后，txpool gas pressure 明显升高。
5. Drain phase 结束后，attack run 的 txpool 仍然保留大量 pending/queued
   transactions，而 baseline txpool 已经清空。

更谨慎地说，当前结果证明的是：

> First-0-payload Deter-Z2 can trigger one successful on-chain transaction per
> delegated attack sender, cause a short normal-inclusion stall at attack start,
> and leave persistent txpool pressure on Besu under the current sampled-fee
> phased setting.

当前结果还不应表述为：

> Normal transactions are permanently excluded.

因为在当前参数下，attack run 中 normal transactions 后续仍然可以恢复 inclusion。

## 7. Figures

本周生成了三类图，可以放在文档或邮件附件中。

### 7.1 Attack Effect / Txpool Gas Pressure

```text
devtools/mpfuzz_style_exp5_pos/scale_runs/besu_first0_sender_monotonic_m40_j20_floor2_cap10_attack_effect.svg
```

这张图展示：

- baseline 和 attack run 的 normal inclusion 对比；
- attack-on 阶段 125 笔 attack tx included；
- baseline txpool 正常 drain；
- attack run 的 txpool gas pressure 在 attack burst 后保持高位。

### 7.2 Paper-like Tx Fee per Block

```text
devtools/mpfuzz_style_exp5_pos/scale_runs/besu_first0_sender_monotonic_m40_j20_floor2_cap10_paper_like_tx_fee.svg
```

这张图模仿参考论文的 fee-per-block 表达方式：

- 左侧 y-axis 表示 normal tx fee per block，单位为 `10^-2 ETH`；
- 右侧 y-axis 表示 adversarial tx fee per block，单位为 `ETH`；
- attack-on 第一个 tick 中，normal fee 下降到 0，adversarial fee 出现 spike；
- adversarial spike 约为 16.8365 ETH，对应 125 笔 first-0-payload attack tx。

当前版本还没有论文图中的 `Adversarial tx fee (Baseline)` 黑线。若要完全复现
论文图的四线结构，下一步需要补一个 adversarial baseline/control run，例如
不使用 first-0-payload 优化的 attack-cost baseline。

### 7.3 Normal Gas Fee Distribution

```text
devtools/mpfuzz_style_exp5_pos/scale_runs/besu_first0_sender_monotonic_m40_j20_floor2_cap10_attack_normal_fee_distribution.svg
```

这张图用于解释 normal gas fee 的随机性和 cap 效果：

- normal gas price 来源于 mainnet fee history；
- 经过 multiplier、jitter、floor、cap 处理；
- sender-monotonic 策略会让后续 tick 中较多交易逐渐接近 cap；
- 当前参数下，后续阶段有较明显 cap saturation，因此 fee 参数仍有继续调优空间。

## 8. Current Interpretation

本周结果可以作为短期阶段性汇报，但还不是最终 evaluation。

当前可以支持的结论：

1. Phased evaluation pipeline 已经跑通。
2. Besu baseline 能稳定处理 sampled-fee normal workload，并在 drain phase 清空
   txpool。
3. First-0-payload optimization 已经在 Besu attack run 中生效。
4. Attack-on 阶段可以观察到 normal inclusion stall。
5. Attack burst 可以显著抬高并维持 txpool gas pressure。
6. 当前 paper-like fee figure 已经可以展示类似参考论文 Figure 5 的 fee-per-block
   现象。

当前还需要谨慎处理的地方：

1. 目前还没有 adversarial fee baseline/control，因此 paper-like figure 中暂时没有
   论文里的黑色 baseline attack-cost 曲线。
2. Txpool gas pressure 目前是基于 txpool count、normal backlog、attack backlog
   和 gas limit 的加权估算，不是 Besu 直接导出的精确 txpool gas 字段。
3. 当前 sampled-fee 参数会让不少 normal transactions 达到 cap，后续需要继续调参
   以获得更自然的 fee distribution。
4. 当前 phased preliminary result 主要集中在 Besu，还需要后续补 Erigon 或其他
   client 对照。

## 9. Next Steps

建议下一步按如下优先级推进：

| Priority | Task | Purpose |
| --- | --- | --- |
| P1 | 整理并提交本周 phased evaluation progress report | 作为短期阶段性成果给导师同步 |
| P2 | 补 adversarial baseline/control run | 支持论文图中的 `Adversarial tx fee (Baseline)` 曲线 |
| P3 | 调整 sampled-fee 参数 | 减少 cap saturation，让 normal fee distribution 更自然 |
| P4 | 改进 txpool gas pressure 统计口径 | 尽量从 client 或 txpool content 中得到更精确的 txpool gas |
| P5 | 在 Erigon 上复现 phased baseline/attack | 做 cross-client comparison |
| P6 | 将稳定结果整理进 Evaluation section | 形成论文 evaluation 初稿 |

## 10. Draft Email

老师您好，

这周我主要把 Deter-Z2 的实验从原来的一次性 txpool filling 版本扩展成了
phased evaluation 版本。目前已经完成 Besu 上的一组 phased baseline 和
first-0-payload attack 初步实验。

新的 phased design 包括 warmup、saturation、attack/control、recovery 和 drain
几个阶段，用来观察正常交易持续进入时，攻击 burst 是否会造成 normal inclusion
stall，以及攻击停止后 txpool 是否能够恢复或清空。

目前初步结果显示：baseline 中 9600 笔 normal transactions 都能被 Besu 接收并
最终上链，txpool 最后清空；attack run 中 16000 笔 attack transactions 被接受，
125 笔 attack transactions 上链，且这 125 笔分别来自 125 个 delegated attack
senders 的首笔 0-payload transaction。攻击开始时 normal inclusion 出现一个
block 的 stall，同时 txpool gas pressure 明显升高，并在 drain 阶段后仍保持较高
水平。

我也整理了几张图，包括 paper-like tx fee per block 图、txpool gas pressure 图
和 normal gas fee distribution 图。当前结果还属于 preliminary result，下一步
计划补 adversarial baseline/control，并进一步调整 sampled gas fee 参数，使最终
evaluation 图更接近论文中的表达方式。

谢谢老师。
