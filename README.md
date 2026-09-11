# Deter-Z2 EIP-7702 Txpool Evaluation

这是 Deter-Z2 实验项目的 GitHub 精简整理版，重点保存 EIP-7702 txpool attack 的实验脚本、合约、Kurtosis 配置、主网 fee/7702 统计数据，以及 Besu / Erigon 本地评估结果。

本仓库不是完整的 `go-ethereum` fork，也不包含本地私钥、账户 dump、`.env`、虚拟环境、Kurtosis 容器数据或完整原始 `scale_runs/` 目录。

## 仓库结构

- `devtools/mpfuzz_style_exp5_pos/`：实验脚本、7702 攻击脚本、统计脚本、画图脚本和 Solidity 合约。
- `kurtosis/`：Besu / Erigon 的 ethereum-package 配置文件。
- `results/mainnet_fee_15000/`：15000 个主网区块的 gas fee 采样数据。
- `results/mainnet_7702_scan_15000/`：15000 个主网区块的 EIP-7702 交易扫描结果。
- `results/cross_client_trial01/`：早期 Besu / Erigon cross-client trial01 结果。
- `results/besu_fee_aligned_baseline/`：Besu phased fee-aligned baseline 结果。
- `results/figures/`：已整理的图表文件。
- `docs/`：中文实验设计、复现说明和结果状态说明。

## 当前状态

已整理进仓库的主要内容：

- Besu / Erigon cross-client trial01 metrics。
- mainnet-derived fee history，规模为 15000 blocks。
- mainnet EIP-7702 block scan，规模为 15000 blocks。
- Besu fee-aligned phased baseline。
- phased workload、receipt collection、fee plotting、attack-effect plotting 相关脚本。

当前最重要的缺口是 matching attack run：

- Besu fee-aligned first-0 phased attack 尚未整理进仓库。
- 该 attack 应与现有 `results/besu_fee_aligned_baseline/` 使用同一组 normal fee 参数。
- 跑完后需要补 attack receipts、attack-effect 图、tx fee per block 图和 baseline-vs-attack summary table。

## 安全说明

不要提交以下内容：

- `.env`
- 私钥、助记词、keystore
- Alchemy 或其他 RPC API key
- `*accounts.jsonl`
- `key_prive_exp5.csv`
- `.venv-exp5/`
- `__pycache__/`
- Kurtosis 自动生成的大量容器数据
- 未筛选的完整 `scale_runs/`

## 复现说明

见 `docs/reproduction.md`。

## 实验设计

见 `docs/experiment_design.md`。
