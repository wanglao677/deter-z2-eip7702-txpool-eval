# Besu 首笔 7、后续 10 gwei 配对实验

## 这次验证什么

固定正常交易 3 gwei、攻击首笔 7 gwei、后续交易 10 gwei，验证容量扩大以及交易池实现改变后，是否仍会出现正常交易成交受抑制、攻击首笔未上链、后续交易排队的现象。

现有 10 笔容量实验已经在完整 Besu 二进制程序上运行；特殊之处是交易池配置。默认 Besu 使用分层交易池，不能把顺序交易池放大后的结果直接当成默认配置结果。

首笔被后续交易挤掉是待验证解释，不能仅凭最终缺失状态确定内部淘汰原因。本脚本保存过程快照和账户 nonce，用于逐步验证。

## 已提供的实验配置

| 配置参数 | 交易池 | 容量约束 | 正常账户数 | 每周期正常交易 | 攻击账户数 | 每攻击周期交易 | 阶段周期数 | 出块间隔 |
|---|---|---|---:|---:|---:|---:|---|---|
| cap10 | 顺序交易池 | 10 笔 | 5 | 8 | 5 | 20 | 1、1、2、2、3 | 4 秒 |
| cap100 | 顺序交易池 | 100 笔 | 50 | 80 | 50 | 200 | 1、1、2、2、3 | 4 秒 |
| sequenced4096 | 顺序交易池 | 4096 笔 | 100 | 800 | 512 | 4096 | 2、4、4、4、10 | 30 秒 |
| default | 默认分层交易池 | 保留默认内存限制 | 100 | 800 | 512 | 4096 | 2、4、4、4、10 | 30 秒 |

阶段依次为预热、攻击前负载、攻击／对照窗口、恢复、排空。这里使用“周期”：发送和采样耗时可能跨越区块，实际区块数会单独统计。名为 saturation 的阶段不代表已经测得池满。

两种大容量配置使用相同负载，用于比较交易池配置；100 笔配置用于较短的扩容验证。顺序交易池配置保留此前单账户比例、本地优先级等实验选项；默认配置不覆盖交易池选项。二者差异不止容量。

## 默认配置每个阶段发多少

| 阶段 | 周期数 | 基线正常交易 | 攻击组正常交易 | 攻击组攻击交易 |
|---|---:|---:|---:|---:|
| 预热 | 2 | 1600 | 1600 | 0 |
| 攻击前负载 | 4 | 3200 | 3200 | 0 |
| 攻击／对照窗口 | 4 | 3200 | 3200 | 16384 |
| 恢复 | 4 | 3200 | 3200 | 0 |
| 排空 | 10 | 0 | 0 | 0 |
| 总计 | 24 | 11200 | 11200 | 16384 |

正常交易为 0 字节附加数据、21000 gas 上限、固定 3 gwei。攻击账户分四批启用，每批 128 个，每个账户 32 笔，共 512 笔首笔和 15872 笔后续交易。

首笔附加填充为 0 字节，后续附加填充为 8192 字节，gas 上限均为 500000。0 字节指附加填充，攻击调用仍含执行委托合约所需数据。7702 授权发生在准备阶段，不能把所有后续调用都直接称为类型 0x4 交易。

每周期先发送正常交易，再以轮转顺序发送当期攻击账户的交易。首笔和后续交易处于同一轮发送流程，不等待首笔上链。资金和授权在负载开始前准备；负载阶段使用本地递增 nonce，不自动补发缺失交易。

基线和攻击各使用独立新网络，正常账户、nonce、费用、交易哈希和发送周期会在结束后逐项通过指纹校验。默认先保持固定负载，机制验证之后再增加随机性。

## 运行方式

以下命令全部在“窗口 2：Ubuntu 实验运行窗口”执行。脚本自动管理新网络和发现端口；窗口 1 无需输入指令。原有实验网络和结果保留，脚本不执行删除。

复现 10 笔容量下的“零链上攻击费用”版本：

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
bash devtools/mpfuzz_style_exp5_pos/run_besu_cap10_zero_fee_pair.sh seed01
```

这个 wrapper 固定使用 `cap10` 配置：正常交易 3 gwei，攻击首笔 1 gwei，攻击后续 7 gwei。这里的“0 gas fee”不是指交易字段里的 gas price 等于 0，而是指攻击交易没有上链 receipt，因此攻击负载实际支付的链上费用为 0。成功标准是：基线 normal 全部上链；攻击组 normal 明显减少并出现池中缺失；攻击交易 `attackIncluded=0`，攻击上链费用为 0。

如果要跑 3 个 seed：

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
for seed in seed01 seed02 seed03; do
  bash devtools/mpfuzz_style_exp5_pos/run_besu_cap10_zero_fee_pair.sh "$seed"
done
```

跑完某一对后画小容量机制图：

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
export PAIR_DIR="/home/wh/ETH/Besu配对实验/实际目录名"
.venv-exp5/bin/python devtools/mpfuzz_style_exp5_pos/plot_lowcap10_besu_mechanism.py \
  --baseline-dir "$PAIR_DIR/baseline" \
  --attack-dir "$PAIR_DIR/attack" \
  --out-dir "$PAIR_DIR/图"
```

首先直接测试默认 Besu 配置：

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
bash devtools/mpfuzz_style_exp5_pos/run_besu_pool_pair.sh pair --profile default --seed seed01
```

这一个命令依次完成编译发送工具、启动基线网络、基线负载、基线审计、启动攻击网络、攻击负载、攻击审计、中文对照报告。

准备阶段按不超过 50 笔分批注资和授权，并在正式发送前检查余额、委托代码和空池；准备失败会中止，不会混成“攻击成交为零”的实验结果。

默认配置每次运行的负载窗口理论约 12 分钟，一对约 24 分钟，另加网络启动、准备和审计时间。脚本会持续打印进度。没有完成真实运行之前，不预先宣称复现成功。

要先跑较短的 100 笔扩容验证：

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
bash devtools/mpfuzz_style_exp5_pos/run_besu_pool_pair.sh pair --profile cap100 --seed seed01
```

大容量顺序交易池对照：

```bash
bash devtools/mpfuzz_style_exp5_pos/run_besu_pool_pair.sh pair --profile sequenced4096 --seed seed01
```

仅预览方案、不启动网络或发送交易：

```bash
bash devtools/mpfuzz_style_exp5_pos/run_besu_pool_pair.sh plan --profile default --seed seed01
```

## 输出在哪里

所有新结果集中放在 `/home/wh/ETH/Besu配对实验/`，每一对实验使用带时间戳的独立目录。不会混入旧 scale_runs。

| 文件 | 用途 |
|---|---|
| 实验结果.md | 可以直接阅读的中文对照表、阶段表和数据检查 |
| plan.json、network.yaml | 实验参数和实际使用的网络配置 |
| source_hashes.json | 本次所用脚本和合约的校验摘要 |
| pair_validation.json | 正常交易内容及计划是否一致，审计是否稳定 |
| baseline、attack 子目录 | 基线和攻击的各自记录 |
| tx_final_status.csv | 每笔交易的提交阶段、成交区块、成交阶段、首笔／后续角色、费用、最终状态 |
| block_stats.csv | 每个实际区块的正常／攻击成交数和实际费用，包含零成交区块 |
| phase_stats.csv | 两种口径：此阶段实际成交多少，此阶段提交的交易最终去了哪里 |
| sender_nonce_evidence.csv | 链上 nonce、余额、委托代码、池内是否缺少账户下一笔交易 |
| pool_snapshots.jsonl | 发送前、发送后、观察出块后的池成员快照；记录快照是否跨区块或报错 |
| pool_final_evidence.json | 最终池成员和审计起止区块 |
| receipts_evidence.json | 批量查询所得原始回执，明确保留未上链交易的空回执 |
| chain_identity.json | 实验网络编号与区块锚点，避免用重建后的链误审旧数据 |
| setup_evidence.json | 正常账户和攻击账户的初始余额、委托代码检查 |
| run.log、network.log | 完整运行与网络日志 |

## 怎样判断结果

先看正常交易计划校验是否通过，再比较攻击／对照窗口的正常成交数和手续费。恢复、排空阶段单独统计，不把预热成交算成攻击期间成交。

攻击上链数为 0、正常成交下降且配对和审计有效时，可以报告“本次攻击负载的链上执行费用为 0，同时观察到正常成交受抑制”。若默认配置让正常交易全部及时通过，也应保留这个结果，它说明小容量现象未按相同方式迁移。

“未上链且池中缺失”不是已确定的内部 eviction 原因；首笔和后续交易的过程快照、链上 nonce 和余额用于核对。审计跨新区块会自动重试，仍不稳定时标为状态待复核，不硬算 dropped。

本次普通交易为轻量转账，不等同于此前 8192 字节正常交易的正式大规模实验；手续费曲线和吞吐不要直接与那批结果混合。

默认节点接收本地 RPC 交易，保留默认本地交易策略。这是一项本地开发网络验证，不能直接代表远程节点通过对等网络传播的效果。

## 中断后的处理

程序发生错误会保留目录和网络，不自动开始下一组。`execution.json` 记录网络名、RPC 和当前步骤。先检查该目录内日志，再对仍存活的原网络补做审计。

```bash
bash devtools/mpfuzz_style_exp5_pos/run_besu_pool_pair.sh audit --run-dir "实际实验目录/attack"
```

如果基线已完成审计、攻击目录尚未创建，可以继续同一对实验：

```bash
bash devtools/mpfuzz_style_exp5_pos/run_besu_pool_pair.sh run --pair-dir "实际实验目录" --mode attack
```

如果已有分支目录，脚本会拒绝覆盖；审计后的报告可以用 `report --pair-dir "实际实验目录"` 重新生成。

## 资料依据

[Besu 官方交易池说明](https://docs.besu-eth.org/public-networks/concepts/transactions/pool)说明默认使用分层交易池，容量按内存约束；顺序交易池需要显式选择。具体实验仍以保存的镜像版本、网络配置和实际运行行为为准。
