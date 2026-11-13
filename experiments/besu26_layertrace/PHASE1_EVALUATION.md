# Phase 1：Besu 26.6.1 Deter-Z2 三组评估

本文件对应分支 `besu26-phase1-evaluation`。正式运行前用 `git rev-parse HEAD`
核对交付消息中的 commit SHA。本地开发机只做编译和轻量测试；正式实验在独立
Ubuntu 机器的私有 Kurtosis devnet 执行。

## 固定实验设计

配置文件为 `experiments/besu26_layertrace/config_phase1.json`。
旧的 `config.json` 保留首笔 8KB + tail 8KB 的历史机制配置，结果不可混用。

| 参数 | G0 正常基线 | G1 drain | G2 no-drain |
| --- | ---: | ---: | ---: |
| Besu / Txpool | 26.6.1 / 原版默认 LAYERED | 同左 | 同左 |
| 共识 / slot | Lighthouse / 300 秒 | 同左 | 同左 |
| 正常交易 | 55 senders × 128 = 7,040 | 同左 | 同左 |
| 攻击交易 | 0 | 125 senders × 128 = 16,000 | 同左 |
| 攻击首笔额外 padding | 无攻击 | 0 bytes | 0 bytes |
| 攻击 tail 额外 padding | 无攻击 | 8,192 bytes | 8,192 bytes |
| 委托代码 | drain 合约，但不广播攻击负载 | `Alice7702Drain` | `Alice7702NoDrain` |
| 每个攻击 sender 初始注资 | 300 ETH | 300 ETH | 300 ETH |
| 广播顺序 | normal 按账户 | normal 后 attack；attack round-robin | 同 G1 |

三组各自在 genesis 给同一个新建的本地 devnet 出资地址预置 100,000 ETH；
其私钥仅在 Ubuntu 本机的被 Git 忽略文件中，网络配置和结果只记录地址。
与旧的 original8k 运行相比，出资账户和 genesis 余额不同，不能把旧轮当作
这三组的严格基线。每个正常 sender 注资 50 ETH。normal gas limit 150,000，
attack gas limit 500,000；
价格单位为 571,428,571,428 wei，normal ×3，attack ×7；setup 价格为 1 gwei。
G1/G2 的第一笔和 tail 都广播相同的 `drainAll(address)` 调用数据，且 value=0。
G2 的委托代码不会转账，包含第一笔和全部 127 笔 tail。两组委托代码的执行 gas
可能不同，结果中分别报告。这里的“0 bytes”仅指 ABI 调用之外的额外 padding；
方法选择器和 32 字节地址参数仍存在。

准备阶段 G0/G1/G2 都部署合约、给正常和攻击 sender 注资并授权，然后等池清空。
每组创建全新的单节点 Besu/Lighthouse 网络；每组使用相同 seed 和配置。
观察注入结束后第 1 和第 10 个区块。运行脚本不更改 Besu 的容量、比较器、驱逐或余额规则。
`--rpc-tx-feecap` 是单笔 RPC 接纳上限 5 ETH，不是交易池容量设置。

## Ubuntu 准备

需要 Docker（当前用户可运行 `docker ps`）、Kurtosis、Git、Go、Python 3、
现有完整 geth 工作区及其虚拟环境，以及本地 devnet key CSV。CSV 只供应 a/x 账户；
Phase 1 出资账户由 `init-funder` 在 Ubuntu 本机随机生成，不需要旧的 funded0 私钥。
安装说明和现有 PoC 背景见 [README.md](README.md)。远程 Ubuntu 的默认路径为
`$HOME/ETH/pos-exp/go-ethereum-pos`；路径不同请用 `--geth-repo` 和 `--key-csv`。

```bash
cd ~/ETH/besu26-layertrace-eval
# 旧 clone 使用 --single-branch 时，也能从 FETCH_HEAD 创建本地分支。
git fetch origin besu26-phase1-evaluation
git switch besu26-phase1-evaluation 2>/dev/null || git switch -c besu26-phase1-evaluation FETCH_HEAD
git merge --ff-only FETCH_HEAD
git rev-parse HEAD

source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
python -m pip install web3 py-solc-x
python -c "import solcx; solcx.install_solc('0.8.20')"

CFG=experiments/besu26_layertrace/config_phase1.json
python experiments/besu26_layertrace/run.py init-funder --config "$CFG"
python experiments/besu26_layertrace/run.py plan --config "$CFG"

python experiments/besu26_layertrace/build_image.py
python experiments/besu26_layertrace/verify.py
python -m unittest discover -s experiments/besu26_layertrace/tests -v

python experiments/besu26_layertrace/run.py prepare --config "$CFG"
```

`build_image.py` 构建固定 Besu 26.6.1 观测镜像；
`verify.py` 是小型内存池检查，不代表正式 PoS 评估。
`init-funder` 只生成或复用本机 `.build/phase1_funder.json`，权限为 0600；
它只打印地址，不打印私钥。`.build/` 已被 Git 忽略。不要复制、提交或发送该文件。
删除该文件会生成不同地址，需重新运行 `init-funder` 和 `prepare`；
同一轮 G0/G1/G2 必须使用同一文件。`prepare` 检查实际镜像版本/CLI、
出资文件、依赖并编译 Go helper，不启动网络。
`plan` 输出应包含 `besuVersion: 26.6.1`、`attackFirstPaddingBytes: 0`、
`attackTailPaddingBytes: 8192`、`prefunded_accounts` 的 JSON 字符串中只含新地址与
`100000ETH`，以及 G0/G1/G2。实际节点版本写入每组
`workload_manifest.json` 的 `clientVersion`；必须以 `besu/v26.6.1/` 开头。

## 运行

建议在 tmux 中启动。推荐一次顺序运行三组，自动生成同一个目录下的对照表：

```bash
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
CFG=experiments/besu26_layertrace/config_phase1.json
python -u experiments/besu26_layertrace/run.py evaluation --config "$CFG" --group all --seed seed01
```

也可分别执行各组，每条命令都会创建独立的结果目录和网络；
从别的 shell 执行时使用同一个 `.build/phase1_funder.json`，无需设置密钥环境变量：

```bash
python -u experiments/besu26_layertrace/run.py evaluation --config "$CFG" --group G0 --seed seed01
python -u experiments/besu26_layertrace/run.py evaluation --config "$CFG" --group G1 --seed seed01
python -u experiments/besu26_layertrace/run.py evaluation --config "$CFG" --group G2 --seed seed01
```

替换 `--seed seed02` 可复现另一个确定性账户分配。不同 seed 的结果不要当作同一配对组。
`--group all` 自动汇总三组；分别运行时各目录的表只有本组，不能直接当作三组对照。
完整三组在 300 秒 slot 下可能持续数小时。不要关闭负责运行的 tmux 会话。
运行失败时保留本次 enclave 和已有文件，脚本不会自动重发交易。

默认结果目录：
`experiments/besu26_layertrace/runs/besu26-layertrace-phase1-first0-tail8k_<seed>_<timestamp>/`。
其中有 `G0/`、`G1/`、`G2/`（按实际运行组数）。
逐组重点文件：

| 文件 | 含义 |
| --- | --- |
| `workload_manifest.json` / `network_identity.json` | seed、配置、真实 Besu 版本、独立网络 |
| `normal.jsonl` / `attack.jsonl` | RPC 接受的逐笔 hash、nonce、价格、padding、提交错误 |
| `normal_timing.json` / `attack_timing.json` | 广播开始/结束墙钟时间、耗时、跨越区块 |
| `normal_block_points.json` / `attack_block_points.json` / `window*_marker.json` | 区块号、hash、timestamp、从执行层 genesis timestamp 推导的 slot |
| `layertrace.jsonl` / `layer_audit.json` | 原生逐笔轨迹、checkpoint 完整性 |
| `tx_layers.csv` / `capacity_choices.csv` / `layer_history.csv` | 逐笔最终观测状态、容量选择、全过程入层/离层 |
| `tx_outcomes.csv` / `normal_layer_events.csv` / `normal_layer_phase_summary.csv` / `normal_capacity_drops.csv` | 回执与轨迹关联、normal 各层事件和逐笔 drop 时间 |
| `attack_receipts.csv` / `first_attack_balances.csv` | First/Tail 成交 gas/价格/费用；首笔前后所在区块的余额 |
| `setup_receipts.json` / `receiver_balance.json` | 部署、注资、授权费用；接收地址余额变化 |
| `group_summary.json` / 根目录 `evaluation_summary.csv` | 本组统计、三组对照表 |

费用用每笔已上链交易的 `gasUsed × effectiveGasPrice` 求和，执行失败但上链的交易
照样计费。攻击负载费用与准备阶段费用分开；准备阶段又分为正常注资、
攻击注资、授权和委托合约部署。注资的 ETH 本金不算 gas 费用。
`group_summary.json` 同时报告 First/Tail 平均 gasUsed 与接收地址余额变化。
`first_attack_balances.csv` 的前后余额是首笔所在区块的边界状态；同一 sender
如果在该区块还有其他成交，字段 `sameSenderIncludedInFirstBlock` 会标出，
不能声称它是单笔交易执行瞬间的精确后余额。

`capacity_choice` 与最终 `POOL_DROPPED` 对应，且观测器无丢事件和 checkpoint
已越过观察窗口时，才计入 `normalCapacityEvictedConfirmed`；`normalCapacityEvictedByFullWindow`
仅计算在第 10 个观察区块标记之前发生的最终容量驱逐。
`leave scope=LAYER` 只算移层；`REMOVED_INVALIDATED`、确认上链、
RPC 拒绝、未观察到的状态分别列出。`poolBlockNumber` 只是池处理区块的上下文，
第一笔攻击上链高度来自 canonical receipt。Slot 从执行区块时间推导，未独立调用
beacon REST API 核验，解释时需保留这个限制。

## 审计及成功条件

`--group all` 成功结束后应看到三个 `group_summary.json`，
根目录 `evaluation_summary.csv` 三行，`layerEvidenceComplete=true`，
`layerIssues=[]`，真实 clientVersion 为 Besu 26.6.1，并核对每组
normalSubmitted=7040、G1/G2 attackSubmitted=16000。实际驱逐数、首笔及
tail 上链数和费用必须读真实输出，不能由配置推断。

必要时从仍在运行的本轮 enclave 重新收取轨迹（`ENCLAVE` 从该组
`network_identity.json` 读取）：

```bash
python experiments/besu26_layertrace/run.py collect --enclave "$ENCLAVE" --out "$RUN/G1"
python experiments/besu26_layertrace/report.py --root "$RUN" --refresh
cat "$RUN/evaluation_summary.csv"
```

`collect` 只影响指定 enclave/目录。若网络已停止，应直接对已保存的
`layertrace.jsonl` 用 `audit.py` 重审，再用 `report.py --refresh` 汇总；
重审时要把 `--out` 指向原组目录以更新 `layer_audit.json` 和 CSV。
`report.py` 会把过期 checkpoint 的无回执状态列为未知，而不强判为驱逐。

本分支只准备 Phase 1 代码。单元测试、编译、`verify.py` 都不是 G0/G1/G2
正式运行结果。观测器有潜在 CPU/磁盘影响；本轮不测 12 秒 slot、P2P 传播或新版 Besu。

## 可选的 Normal 随机费用模式

固定费用的 config_phase1.json 保持原样。独立的
config_phase1_random_fee.json 只改变 normal 的报价方式和实验目录名称；
G0、G1、G2、Besu 版本、slot、发送顺序、normal gas limit 150,000、
attack gas price 及 first/tail padding 均沿用固定费用配置。

normal 是 Go helper 签名的 legacy type-0 交易，每笔包含 gasPrice；
其中心价格为 1,714,285,714,284 Wei（约 1,714.29 Gwei）。
随机模式用固定种子 besu26-phase1-normal-fee-v1 对每笔独立抽样，
在中心价格 ±10% 的整数 Wei 范围内均匀分布。
生成器是有版本标识的 SHA-256 拒绝采样；同一配置产生完全相同的
逐笔价格序列。命令的 --seed seed01/seed02 仍控制账户分配；
normal 费用随机种子单独固定，因此跨 G0/G1/G2 以及不同账户 seed
都可复用同一报价序列。比较不同账户 seed 的实验时，应分别成组评价。

Ubuntu 运行随机费用三组时，沿用上面的镜像、依赖、本地 funder 和
prepare 步骤，只将 CFG 设置为新文件，使用全新的结果目录：

    cd ~/ETH/besu26-layertrace-eval
    source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
    CFG=experiments/besu26_layertrace/config_phase1_random_fee.json
    python experiments/besu26_layertrace/run.py plan --config "$CFG"
    python experiments/besu26_layertrace/run.py prepare --config "$CFG"
    python -u experiments/besu26_layertrace/run.py evaluation --config "$CFG" --group all --seed seed01

每组的 normal_fee_plan.csv 包含广播顺序、label、sender、nonce 和签名报价；
normal_fee_prices.txt 是传给 Go helper 的逐笔价格列表。
normal_fee_plan.json 包含费用区间、gas limit、模式、随机种子及序列 SHA-256。
广播结束后程序逐笔核对 helper 的 label、sender、nonce 和 gasPriceWei；
根目录汇总也要求已完成组的费用序列摘要完全一致，否则拒绝形成配对结果。

group_summary.json 的 normalFeeStats 以及根目录 evaluation_summary.csv
给出已接受 normal 的 Mean、Median、Min、Max、P10、P90，单位 Gwei；
P10/P90 使用 nearest-rank 定义。group/normal_fee_bins.csv 和根目录
normal_fee_bins.csv 按报价区间给出接受数、已证实的容量驱逐数、
最终驱逐率、第 10 个观察区块标记前的驱逐数及驱逐率。
随机范围分成四个等宽的整数 Wei 区间；固定模式只有一个价格区间。
只有轨迹审计完整且 checkpoint 覆盖观察窗口时，驱逐率才有数值；
证据不完整时留空，不能解释为 0%。

这些统计中的 gasPrice 是交易签名时提出的价格，不代表已经支付的手续费。
实际已支付的费用只从已上链回执的 gasUsed × effectiveGasPrice 得到；
tx_outcomes.csv 分别记录 signedGasPriceWei、effectiveGasPriceWei、gasUsed
和 feeWei，未上链交易的实际费用字段留空。相同 sender 的多笔交易有
nonce 依赖，因此不同价格区间的驱逐率是描述性观察，不单独证明价格
导致驱逐。正式实验只在远程 Ubuntu 执行。
