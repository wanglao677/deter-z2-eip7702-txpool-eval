# 复现实验说明

本仓库是从本地 Deter-Z2 / EIP-7702 txpool attack 实验目录整理出来的精简版。它保留了原始实验脚本的目录结构：

```text
devtools/mpfuzz_style_exp5_pos/
```

保留这个路径是有意的，因为 Python 脚本和 Go helper 都假设从兼容的 `go-ethereum` checkout 根目录运行。

## Python 环境

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
python3 -m venv .venv-exp5
source .venv-exp5/bin/activate
pip install -r /home/wh/ETH/deter-z2-eip7702-txpool-eval/requirements.txt
python3 -c "from solcx import install_solc; install_solc('0.8.20')"
```

## 环境变量

运行本地 devnet 实验前，需要在 shell 里设置 funded account 私钥。收集主网 fee 或 7702 block scan 时，还需要设置主网 RPC 地址。

```bash
export EXP5_FUNDER_PRIVATE_KEY=...
export ALCHEMY_RPC_URL=...
```

不要把 `.env`、私钥、账户文件、keystore、助记词或 API token 提交到 GitHub。

## 启动 Kurtosis 网络

本仓库的 `kurtosis/` 目录保存了 Besu 和 Erigon 的 ethereum-package 配置。实际运行时，在本地 Kurtosis 工作目录中使用对应配置文件。

Besu 示例：

```bash
cd /home/wh/ETH/kurtosis-pos-exp
kurtosis run --enclave besu-lighthouse-7702-phased-formal \
  github.com/ethpandaops/ethereum-package \
  --args-file ./network_params_besu_lighthouse_7702_default_txpool_slot30.yaml \
  --image-download missing
```

Erigon 示例：

```bash
cd /home/wh/ETH/kurtosis-pos-exp
kurtosis run --enclave erigon-lighthouse-7702-phased-formal \
  github.com/ethpandaops/ethereum-package \
  --args-file ./network_params_erigon_lighthouse_7702_default_txpool_slot30.yaml \
  --image-download missing
```

如果同名 enclave 已存在但处于 stopped 状态，先用 Kurtosis 的 enclave 管理命令清理或换一个新的 enclave 名称。

## 查看 phased 实验参数

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
source .venv-exp5/bin/activate
python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_7702_phased_pos.py --help
```

该脚本支持：

```text
--client besu
--client erigon
```

## 当前下一组关键实验

下一组关键实验是 Besu fee-aligned first-0 phased attack。它应当和已经整理进仓库的 Besu fee-aligned baseline 对齐 normal fee 参数。

目标参数：

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
