# 本地验证记录

日期：2026-11-04。环境：UbuntuE / Docker / 官方 Besu 26.6.1 AMD64 发行镜像。

* 观测镜像构建成功，`--version` 返回 `besu/v26.6.1/linux-x86_64/openjdk-java-25`。
* `verify.py` 通过：原版、观测关闭、观测开启的真实 Ready/EndLayer 小型场景结果相同。
  三种模式均保留低价 sender 的 nonce 0，移出其 nonce 1，保留较高价格的新交易。
  开启观测时记录了 candidate nonce 0 / actual nonce 1、EndLayer drop、invalidated 和 reset，
  session_end 无日志丢失和序号缺口。
* 16 项 Python 测试通过，覆盖层间迁移与最终移除的区分、重新接纳、promotion/replacement、
  reset、丢日志、缺 checkpoint、未 flush 尾部、损坏行、重复序号、RPC batch 顺序和错误、
  负载参数及累计 upfront balance 校验；此外验证了出资账户私钥缺失、地址不匹配和有效密钥加载。
* Go helper 编译成功；`run.py prepare` 检查通过，固定版本网络包、Python/solc、本地密钥路径可用。
* 新 CLI 参数经过 Besu `--help` 解析检查；Python 源文件编译检查通过。
* 回归检查确认缺少 `EXP5_FUNDER_PRIVATE_KEY` 时 `prepare` 在创建网络之前失败，
  错误消息不包含密钥。

本次没有启动完整 PoS 网络、没有执行 7,040/16,000 笔攻击实验，也没有完整运行 Besu 上游测试套件。
小型状态对照证明所测路径未改变驱逐结果，不证明所有路径或运行时性能完全等价。
每次重新构建后必须重新执行 `verify.py`；`prepare` 校验验证记录中的镜像 ID。
