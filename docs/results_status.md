# 结果状态

本仓库目前保存的是 Deter-Z2 / EIP-7702 txpool attack 本地评估项目的精简整理版。

## 已包含

- Besu / Erigon cross-client trial01 结果。
- 15000 个主网区块的 fee history。
- 15000 个主网区块的 EIP-7702 block scan。
- Besu fee-aligned phased baseline。
- phased evaluation、receipt collection 和 plotting 脚本。

## 当前主要缺口

Besu fee-aligned first-0 attack run 尚未包含。当前 fee-aligned baseline 已经说明：在没有攻击压力的情况下，normal workload 可以被 Besu 正常处理并清空 txpool。

下一步需要跑 matching attack run，使用和 baseline 对齐的 normal fee 配置：

```text
normal fee field: effectiveP50Gwei
normal fee multiplier: 11.275044
normal fee jitter: 0.25
normal fee floor: 2 gwei
normal fee cap: 20 gwei
normal fee sampling: sender-monotonic
attack first payload: 0-byte padding
attack blocks: 4
attack sender activation: new-per-block
```

## 下一步目标

跑完 Besu fee-aligned first-0 phased attack 后，需要：

```text
1. 收集 attack receipts。
2. 生成 attack-effect 图。
3. 生成 tx fee per block 图。
4. 整理 baseline vs attack summary table。
5. 将精选结果加入 results/besu_fee_aligned_first0_attack/。
```
