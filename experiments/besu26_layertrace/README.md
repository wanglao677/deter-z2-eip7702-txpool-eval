# Besu 26.6.1：逐笔层迁移观测与原始 8KB 配对实验

这是独立的 `besu26-layertrace` 实验分支。目标是解释正常交易怎样经过
Prioritized → Ready → Sparse → EndLayer，以及最终退出池子的原因。
不改 Besu 的比较器、容量计算、余额检查、nonce 规则或驱逐对象选择。

## 本轮配置

| 项目 | 配置 |
|---|---|
| 执行客户端 | Besu 26.6.1，源码 `983c53fb797b7e2e8cf940dc0361770705675a93` |
| 池策略 | LAYERED，保留该发行版默认策略；Prioritized 5,000 笔，Ready/Sparse 各 50,000,000 bytes |
| 共识/网络 | Lighthouse + ethereum-package 6.1.0 固定 commit，1 个节点，300 秒 slot |
| normal | 55 senders × 128 = 7,040 笔，8,192 bytes 零字节 calldata，gas limit 150,000 |
| attack | 125 delegated senders × 128 = 16,000 笔，gas limit 500,000 |
| attack padding | 首笔和后续均 8,192 bytes；`drainAll(address)` ABI 参数另计 |
| 价格单位 | 571,428,571,428 wei |
| normal gas price | 单位 × 3 = 1,714,285,714,284 wei |
| attack gas price | 首笔/后续均单位 × 7 = 3,999,999,999,996 wei |
| 顺序 | 先发完 normal，再发 attack；normal 按 account，attack round-robin；单路广播 |
| 观察窗口 | 注入结束后第 1 和第 10 个真实区块 |
| setup | 两组都部署、注资、授权；baseline 仅跳过 attack 负载 |

这里沿用的是仓库 `results/cross_client_trial01/besu_attack/summary.json` 的原始机制案例，
不是 V5 的“首笔无 padding / tail p24k / 3、7、10 gwei”配置。
攻击工作负载是已授权账户发出的普通 legacy 交易；EIP-7702 type-4 交易用于前置授权。

明确标准化的差异：normal 每个 sender 注资 50 ETH，attack 300 ETH，以覆盖
128 笔累计 upfront cost；setup 使用 1 gwei。旧结果文件未记录完整注资配置，
因此本轮不声称逐项复刻旧 setup。RPC 单笔费用上限显式设置 20 ETH，
允许上述高价格负载提交；这是 RPC 接纳设置，不是池驱逐规则。
Lighthouse 镜像固定 digest，所有运行保存镜像 ID、实际启动命令、Go/包版本和配置散列。

## 安装与准备（Ubuntu / WSL 中执行）

使用独立目录，保留现有完整 geth 工作区供 helper 编译。
以下假定完整 geth 位于 `$HOME/ETH/pos-exp/go-ethereum-pos`，本地实验密钥文件仍在其
`devtools/mpfuzz_style_exp5_pos/key_prive_exp5.csv`，不会上传密钥。
该 CSV 只包含 `a` 和 `x`；`funded0` 出资账户还需要在运行 `prepare` 和 `pair` 的同一终端中设置
`EXP5_FUNDER_PRIVATE_KEY`。脚本会在启动网络前检查私钥格式与出资地址是否一致，
不会在错误消息中显示私钥。若关闭终端或进入新的 tmux shell，需重新设置该变量。

```bash
cd ~/ETH
git clone --branch besu26-layertrace --single-branch git@github.com:wanglao677/deter-z2-eip7702-txpool-eval.git besu26-layertrace-eval
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate

# 在当前终端安全输入 funded0 私钥，输入时不回显，也不写入 shell 历史。
read -rsp 'funded0 devnet private key: ' EXP5_FUNDER_PRIVATE_KEY; printf '\n'
export EXP5_FUNDER_PRIVATE_KEY

# 仅在该虚拟环境缺少依赖时安装；不要用 sudo pip。
python -m pip install web3 py-solc-x
python -c "import solcx; solcx.install_solc('0.8.20')"

python experiments/besu26_layertrace/build_image.py
python experiments/besu26_layertrace/verify.py
python -m unittest discover -s experiments/besu26_layertrace/tests -v
python experiments/besu26_layertrace/run.py prepare
python experiments/besu26_layertrace/run.py plan
```

`build_image.py` 从固定 commit 下载三个 Java 源文件，检查并应用观测补丁，
用固定 Besu AMD64 发行镜像里的 JDK 25 和 release jars 编译，然后更新 eth jar。
它检查每个文件的观测调用，记录修改前后 SHA-256。未设置
`BESU_LAYER_TRACE_FILE` 时观察器不启用。新镜像只构建在本机，不推送 Docker Hub。

`verify.py` 在内存中运行小型真实 Ready/EndLayer 场景，对比原版、观测关闭、
观测开启的池状态，并检查候选 nonce 与实际移出 nonce、最终 drop、invalidated 日志。
没有启动 PoS 网络或发送实验负载。`prepare` 要求该镜像的验证通过，再编译 helper、
检查 Python/solc/密钥文件，并获取固定 ethereum-package；不会启动实验网络。

路径不同可以显式提供：

```bash
python experiments/besu26_layertrace/run.py prepare \
  --geth-repo "$HOME/ETH/pos-exp/go-ethereum-pos" \
  --key-csv "$HOME/ETH/pos-exp/go-ethereum-pos/devtools/mpfuzz_style_exp5_pos/key_prive_exp5.csv"
```

## 真正启动实验

先完成上述准备。建议在 tmux 中执行，300 秒 slot 的两组实验会耗时数小时。

```bash
cd ~/ETH/besu26-layertrace-eval
source ~/ETH/pos-exp/go-ethereum-pos/.venv-exp5/bin/activate
read -rsp 'funded0 devnet private key: ' EXP5_FUNDER_PRIVATE_KEY; printf '\n'
export EXP5_FUNDER_PRIVATE_KEY
python -u experiments/besu26_layertrace/run.py pair --seed seed01
```

流程：baseline 独立网络 → matching setup → fresh block → normal → 1/10 block 观察 →
保存证据并停止本轮 baseline；然后创建 attack 独立网络执行相同 setup、normal、attack、观察。
成功后停止这次创建的网络，保留 enclave 和磁盘文件。不会停止其他实验、不会 `rm` enclave。
任何启动、提交、回执校验或观测完整性错误都会停止流程，保留当前网络供排查。
传输错误不自动重发，避免重复提交改变实验。

输出在 `experiments/besu26_layertrace/runs/<配置>_<seed>_<时间>/`。
每组实际 network 名称/RPC 保存在 `network_identity.json`。
发送跨区块不会被隐瞒或自动重试：`normal_timing.json`、`attack_timing.json`
记录耗时、开始/结束区块、跨区块数。1/10 block 是各组注入结束后的观察窗口，
不是完全相同的墙钟时长；解释对照时必须一并看这些时间。

## 证据与判定

| 文件 | 用途 |
|---|---|
| `layertrace.jsonl` | 节点原始事件；不含 RLP、calldata 或私钥 |
| `tx_layers.csv` | 各 workload hash 在最后完整 checkpoint 时的层/移除状态及完整性标记 |
| `layer_history.csv` | 每笔交易的入层、移出、最终 drop 事件与时间 |
| `capacity_choices.csv` | 容量驱逐候选 hash/sender/nonce 与实际移出 hash/nonce |
| `layer_audit.json` | 序号缺口、丢失计数、checkpoint 水位和日志散列 |
| `normal.jsonl` / `attack.jsonl` | 提交清单、hash、nonce、价格、padding、提交错误 |
| `receipts.json` / `summary.json` | 回执和 1/10 block 上链数、成功数、实际费用 |
| `setup_accounts.json` / `senders_window*.json` | 指定链上高度的 sender nonce、余额、委托代码 |
| `metrics.prom` / `node_runtime.json` / `besu.log` | 聚合指标、实际配置与节点日志 |

区分三种证据：

* `leave` 且 scope=`LAYER`：只表示离开某层，下一步可能进入另一层。
* `drop`：实际走到 EndLayer 的 `DROPPED`。结合 `capacity_choice` 与前后事件解释驱逐链。
* `leave` 且 scope=`POOL`：按实际原因记为 confirmed、invalidated、replaced、below_min_score 等，
  不把余额失效或评分移除算成容量驱逐。

`poolBlockNumber` 是池最近处理的区块上下文，不等于交易上链高度；上链以回执为准。
`score` 等字段是事件发生时的值，不是持续采样。
`requestedCount` 保留客户端传入的有符号压力值；容量由 bytes 触发时它可以为负，
不表示实际移出了负数笔交易。

观察器用有界异步队列写盘，记录序号、dropped、checkpoint。审计只回放到最后完整
checkpoint；其后事件计入 `eventsAfterCheckpoint`，不用于当前状态。
出现丢日志、重启、多 session、序号缺口或损坏行时，`evidenceComplete=false`，
CSV 保留最后观察值但不把它当作完整最终状态。未观察到的 hash 为 `UNOBSERVED`，
不能据此认定被驱逐。RPC pending/queued 也不等同于这些物理层。

失败后补收集（把参数换成该轮 `network_identity.json` 的值）：

```bash
python experiments/besu26_layertrace/run.py collect \
  --enclave YOUR_ENCLAVE \
  --out /absolute/path/to/the/run/attack
```

已有文件离线重审：

```bash
python experiments/besu26_layertrace/audit.py \
  --trace /absolute/path/to/attack/layertrace.jsonl \
  --out /absolute/path/to/attack/review \
  --records /absolute/path/to/attack/normal.jsonl /absolute/path/to/attack/attack.jsonl
```

## 验证范围

提交前已完成发行镜像构建、helper 编译、准备检查、审计单元测试，以及真实层对象的
原版/关闭/开启观察器对照。完整 PoS 配对实验留给实际运行，尚无本轮攻击效果结论。
观察代码仍有 CPU、锁与磁盘开销；小型状态对照不证明性能等价。
本轮先验证驱逐路径；如需比较攻击成功率或吞吐，需要另做观测开关的同配置对照。

上游依据均固定到 Besu 26.6.1 源码，而非在线最新文档：
[AbstractTransactionsLayer](https://github.com/besu-eth/besu/blob/983c53fb797b7e2e8cf940dc0361770705675a93/ethereum/eth/src/main/java/org/hyperledger/besu/ethereum/eth/transactions/layered/AbstractTransactionsLayer.java)、
[ReadyTransactions](https://github.com/besu-eth/besu/blob/983c53fb797b7e2e8cf940dc0361770705675a93/ethereum/eth/src/main/java/org/hyperledger/besu/ethereum/eth/transactions/layered/ReadyTransactions.java)、
[SparseTransactions](https://github.com/besu-eth/besu/blob/983c53fb797b7e2e8cf940dc0361770705675a93/ethereum/eth/src/main/java/org/hyperledger/besu/ethereum/eth/transactions/layered/SparseTransactions.java)。
