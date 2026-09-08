# Experiment 5 PoS Report: Sender-Balance Exhaustion through Contract Value Forwarding

## 1. 实验目标

PoS 版本 Experiment 5 的当前主实验目标是验证一种由实验参数构造出来的 sender-balance exhaustion 场景：

```text
x 账户初始只有 17 ETH。
X1 调用一个普通 payable 合约函数，并在交易中携带 10 ETH。
合约函数内部把 X1 带入的 msg.value 转给 a。
X1 同时需要承担 gas 成本。
因此 X1 执行后，x 的余额不足以继续支持后续 X2-X4。
```

这里的“耗尽”不是合约强制从 x 钱包里掏钱。合约不能主动转走外部账户的 ETH。真正发生的是：实验把 X1 设计成携带 `value=10 ETH`，合约只把这笔 `msg.value` 转发出去；再加上交易 gas 成本，使 x 的可用余额不足。

## 2. 实验环境

| 项目 | 内容 |
| --- | --- |
| 新 PoS Geth 仓库 | `/home/wh/ETH/pos-exp/go-ethereum-pos` |
| Kurtosis 配置目录 | `/home/wh/ETH/kurtosis-pos-exp` |
| 实验目录 | `/home/wh/ETH/pos-exp/go-ethereum-pos/devtools/mpfuzz_style_exp5_pos` |
| EL 客户端 | saferad Geth: `saferad-geth-pos:dev` |
| CL 客户端 | Lighthouse |
| network id | `13371337` |
| slot 时间 | 4 秒 |

Geth txpool 关键配置：

```text
--txpool.nolocals
--txpool.pricelimit=1
--txpool.pricebump=1
--txpool.globalslots=4
--txpool.globalqueue=0
--txpool.accountslots=4
--txpool.accountqueue=0
--txpool.admissionpolicy=price-only
--rpc.txfeecap=0
```

其中 `--rpc.txfeecap=0` 是因为本实验故意构造高 gas-cap 交易。如果不关闭 RPC fee cap，Geth 会在 RPC 层拒绝交易，例如：

```text
tx fee (7.00 ether) exceeds the configured cap (1.00 ether)
```

## 3. 文件组成

| 文件 | 作用 |
| --- | --- |
| `mp_exp5_pos.py` | PoS 实验主脚本，负责发现 RPC、fund 账户、部署合约、发送 A/X 交易、检查 txpool 与 receipt |
| `ValueForwarder.sol` | 当前主实验合约，负责把 `msg.value` 转发给目标地址 |
| `TransferVault.sol` | 旧的合约余额耗尽对照 case，已不再作为主实验 |
| `OneShotDrain.sol` | 旧 one-shot 状态对照 case |
| `key_prive_exp5.csv` | 实验账户 `a` 和 `x` 的地址与私钥 |
| `README.md` | 运行说明 |
| `EXP5_POS_REPORT.md` | 本报告 |

当前脚本支持的 case：

| Case | 定位 | 是否符合当前目标 |
| --- | --- | --- |
| `sender-balance-exhaustion` | 主实验 | 是 |
| `value-forward-exhaustion` | 主实验别名，会归一化为 `sender-balance-exhaustion` | 是 |
| `transfer-all` | 旧对照，合约预存余额被 X1 转空 | 否 |
| `contract-state-drain` | 旧 one-shot 状态对照 | 否 |

## 4. 主合约设计

当前主实验使用 `ValueForwarder.sol`：

```solidity
pragma solidity ^0.8.20;

contract ValueForwarder {
    event Forwarded(address indexed caller, address indexed to, uint256 amount);

    receive() external payable {}

    function forwardTo(address payable to) external payable {
        require(msg.value > 0, "no value");
        (bool ok,) = to.call{value: msg.value}("");
        require(ok, "forward failed");
        emit Forwarded(msg.sender, to, msg.value);
    }
}
```

这个合约的语义很简单：

```text
调用者在交易里带入多少 msg.value，
合约就把这笔 msg.value 转给参数中的 to 地址。
```

因此 X1 的含义是：

```text
X1 from = x
X1 value = 10 ETH
X1 data = forwardTo(a)

执行时：
10 ETH 从 x 进入合约；
合约内部立刻把这 10 ETH 转给 a；
x 还需要支付 gas。
```

## 5. 主 Case: sender-balance-exhaustion

### 5.1 Setup

PoS 脚本会先用 Kurtosis 默认预置账户 `funded0` 给旧实验账户打钱：

| 账户 | setup 后余额目标 | 作用 |
| --- | ---: | --- |
| `a` | 100 ETH | 普通交易发送者、合约转账接收者 |
| `x` | 17 ETH | 攻击交易发送者 |

然后部署 `ValueForwarder` 合约。注意：PoS 主 case 中合约由 `funded0` 部署，不由 `x` 部署。原因是必须保持：

```text
x 初始余额 = 17 ETH
```

如果让 `x` 部署合约，setup 阶段就会提前消耗 x 的余额，破坏实验初始条件。

### 5.2 A 交易

A1-A4 用来先填满全局 pending pool：

| 交易 | from | nonce | gasPrice 逻辑值 | value | gas limit | 目标 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| A1-A4 | `a` | 连续 nonce | 3 | 1 wei | 21000 | 普通 ETH 转账 |

发送 A1-A4 后预期：

```text
pending=4
queued=0
A1-A4 全部 pending
```

### 5.3 X 交易

X1-X4 是主实验交易：

| 交易 | from | nonce | gasPrice 逻辑值 | value | gas limit | 合约调用 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| X1-X4 | `x` | 连续 nonce | 7 | 10 ETH | 100000 | `ValueForwarder.forwardTo(a)` |

脚本为主 case 计算工作负载专用 `price-unit`：

```text
workloadPriceUnitWei = 10000000000000
```

因此 X 交易的实际 gas price 为：

```text
7 * 10000000000000 wei
```

X 交易的 gas cap 为：

```text
7 * 10000000000000 * 100000 = 7 ETH
```

所以每笔 X 交易的最大 upfront cost 为：

```text
10 ETH value + 7 ETH gas cap = 17 ETH
```

这正好匹配 `x` 的初始 17 ETH 余额。

## 6. 预期 txpool 行为

这个主 case 和旧 `transfer-all` 不同。旧 case 的 X1-X4 都是 `value=0`，失败点在合约内部，所以 X1-X4 可以全部 pending，然后 X2-X4 在执行时 revert。

当前主 case 的失败点是 sender balance / upfront cost。这个条件对 txpool 是可见的，因此更合理的预期是：

```text
X1 可能进入 pending 并挤掉一个或多个低价 A slot；
X2-X4 由于 x 的余额约束，可能无法进入或无法保持 pending。
```

脚本接受两种可能观察：

| 观察 | 含义 |
| --- | --- |
| 只有 X1 pending，一个或多个 A dropped | Geth 在 txpool 阶段执行了 sender balance / upfront-cost 约束；未被挤掉的 A 仍可能继续出块 |
| A1-A4 dropped，X1-X4 都 pending | 说明该构建在入池阶段未累计约束全部 X 交易，后续执行阶段会暴露余额耗尽 |

主报告不能再把 X2-X4 写成固定的 `status=0x0 revert`，因为余额不足不是合约内部 revert，而是账户层面的余额约束。

## 7. 预期执行结果

如果 X1 被打包执行，预期为：

```text
X1 status = 0x1
X1 的 10 ETH msg.value 被合约转给 a
合约余额保持为 0
x 支付实际 gas 后，剩余余额低于下一笔 X 的 upfront cost
X2-X4 没有成功 receipt
```

这里需要特别说明：`7 ETH` 是 gas cap，不代表实际一定花满 7 ETH。未使用的 gas 会退还。因此最终 x 余额不一定严格等于 0。实验真正检查的是：

```text
xBalanceWei < nextXUpfrontCostWei
```

也就是 x 不足以继续发送下一笔同样参数的 X 交易。

## 8. 部署方式

PoS 脚本支持两种部署路径。

### 8.1 Web3/iBatch-style

```text
Solidity source
-> solcx.compile_source()
-> w3.eth.contract(...)
-> contract.constructor().transact()
-> receipt.contractAddress
```

主 case 下部署：

```text
source=ValueForwarder.sol
contractName=ValueForwarder
constructor().transact(value=0)
deployer=funded0
```

### 8.2 Raw fallback

raw fallback 不依赖 Web3.py 和 solc。脚本内置了等价 creation bytecode，并发送 signed raw creation tx。

主 case 下 raw bytecode 的运行时逻辑等价于：

```text
只接受 forwardTo(address)
要求 msg.value > 0
CALL(to, msg.value)
CALL 成功则返回，否则 revert
```

## 9. 运行步骤

窗口 1：启动或重启 Kurtosis PoS devnet。

```bash
cd /home/wh/ETH/kurtosis-pos-exp

kurtosis enclave stop geth-lighthouse-devnet || true
kurtosis enclave rm -f geth-lighthouse-devnet || true

kurtosis run --enclave geth-lighthouse-devnet \
  github.com/ethpandaops/ethereum-package \
  --args-file ./network_params.yaml \
  --image-download missing
```

窗口 2：确认 EL RPC 端口。

```bash
cd /home/wh/ETH/kurtosis-pos-exp
kurtosis enclave inspect geth-lighthouse-devnet
```

找到这一行：

```text
rpc: 8545/tcp -> 127.0.0.1:<rpc_port>
```

窗口 2：运行 raw fallback 主实验。

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos

python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_pos.py \
  --rpc http://127.0.0.1:<rpc_port> \
  --case sender-balance-exhaustion \
  --deploy-mode raw \
  --receipt-timeout 120
```

窗口 2：运行 Web3/iBatch-style 主实验。

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
source /tmp/exp5-web3-venv/bin/activate

python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_pos.py \
  --rpc http://127.0.0.1:<rpc_port> \
  --case sender-balance-exhaustion \
  --deploy-mode web3 \
  --receipt-timeout 120
```

如果没有 Web3 依赖：

```bash
python3 -m venv /tmp/exp5-web3-venv
source /tmp/exp5-web3-venv/bin/activate
python3 -m pip install web3 py-solc-x
python3 -c "from solcx import install_solc; install_solc('0.8.20')"
```

## 10. 当前验证状态

当前主 case 已完成代码实现和静态检查：

```text
mp_exp5_pos.py 支持 sender-balance-exhaustion
ValueForwarder.sol 已加入
raw fallback bytecode 已加入
Web3/iBatch-style 部署路径已支持 ValueForwarder
network_params.yaml 已加入 --rpc.txfeecap=0
```

已进行过一次 PoS raw 调试运行，第一次失败原因是 Geth RPC fee cap：

```text
tx fee (7.00 ether) exceeds the configured cap (1.00 ether)
```

已通过 `--rpc.txfeecap=0` 修复该环境配置。后续需要重新启动 Kurtosis devnet 后补充 raw 和 Web3 两条路径的完整实测日志。

## 11. 与旧 case 的关系

| Case | 合约 | ETH 来源 | 失败点 | 当前定位 |
| --- | --- | --- | --- | --- |
| `sender-balance-exhaustion` | `ValueForwarder` | X 交易自己的 `msg.value` | x 余额 / upfront cost 不足 | 主实验 |
| `transfer-all` | `TransferVault` | 合约预存 17 ETH | 合约余额不足导致 revert | 旧对照 |
| `contract-state-drain` | `OneShotDrain` | 合约预存 17 ETH | 状态变量改变导致 revert | 旧对照 |

因此正式报告主线应使用 `sender-balance-exhaustion`，不能再把 `transfer-all` 当成主实验结论。

## 12. 结论

PoS 版本当前实验 5 的主线已经调整为：

```text
X1 自己携带 ETH 调用合约；
合约内部只转发 X1 带入的 msg.value；
余额耗尽来自实验参数 value + gas cap 的设计；
不是合约强制耗尽 x 的账户余额。
```

该设计符合“合约内部有一笔转账，但钱来自 X1 交易本身”的实验要求。后续报告结果部分应以重新实测的 `sender-balance-exhaustion` raw 和 Web3/iBatch-style 日志为准。
