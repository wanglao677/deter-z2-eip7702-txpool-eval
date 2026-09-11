# 实验设计

Deter-Z2 是一个基于 EIP-7702 的交易池攻击实验。实验目标是评估攻击交易是否能够在本地执行客户端的 txpool 中挤出正常交易，并观察正常交易的上链情况、攻击交易的执行不对称性以及攻击成本。

## 支持的客户端

当前脚本支持两个 Ethereum 执行客户端：

```text
Besu
Erigon
```

两者都通过 Kurtosis 和 ethereum-package 启动 PoS devnet，并使用 Lighthouse 作为共识客户端。

## 实验对照

实验分为两类运行：

```text
baseline: 只发送正常交易
attack: 发送同样的正常交易，并在 attack-on 阶段加入 Deter-Z2 攻击交易
```

baseline 的作用是证明 normal workload 本身可以被客户端接受并处理。attack run 则观察加入 EIP-7702 攻击交易后，正常交易是否被挤出 txpool，或者无法在观测窗口内上链。

## Phased Workload

phased 实验把 workload 分成多个阶段：

```text
warmup: 预热阶段，开始发送正常交易
saturation: 持续发送正常交易，使 txpool 进入压力状态
control / attack-on: baseline 继续发送正常交易，attack run 同时发送攻击交易
recovery: 攻击停止，正常交易继续发送，观察是否恢复
drain: 停止发送 workload，观察 txpool 是否清空
```

这种设计让 baseline 和 attack run 使用同一组 normal workload，减少“攻击效果”和“workload 差异”混在一起的问题。

## Deter-Z2 攻击构造

攻击使用多个 EIP-7702 delegated EOA。每个攻击 sender 先被 set-code transaction 委托到 drain implementation，然后从该 delegated account 发送多笔高价交易。

核心不对称性是：

```text
txpool admission 阶段：多笔攻击交易看起来价格更高，能够形成 txpool pressure。
execution 阶段：每个攻击 sender 通常只有第一笔交易真正成功执行。
结果：攻击者用较少上链执行成本，支撑更多停留在 txpool 中的攻击交易。
```

first-0-payload optimization 进一步降低第一笔真正上链攻击交易的 calldata 成本；后续攻击交易继续使用大 payload 留在 txpool 中形成压力。

## 主要指标

重点观察：

```text
normalSubmitted
normalAccepted
normalIncluded
normalPendingFinal
normalQueuedFinal
attackSubmitted
attackAccepted
attackIncluded
attackPendingFinal
attackQueuedFinal
txpoolPendingFinal
txpoolQueuedFinal
normalCostEth
attackCostEth
```

图表优先展示：

```text
normal inclusion over phases
txpool gas pressure over phases
transaction fee per block
normal fee distribution
baseline vs attack summary table
```

## 当前实验主线

当前 GitHub 仓库已经包含：

```text
Besu / Erigon cross-client trial01 结果
mainnet 15000 blocks fee history
mainnet 15000 blocks EIP-7702 scan
Besu fee-aligned phased baseline
```

下一步最重要的是补齐：

```text
Besu fee-aligned first-0 phased attack
```

这组 attack 应与现有 fee-aligned baseline 对齐 normal fee 参数，然后收集 receipts，生成 attack-effect 图、tx fee per block 图，并整理 baseline-vs-attack summary table。
