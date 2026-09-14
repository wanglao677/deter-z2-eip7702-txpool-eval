# MPFUZZ-style experiment 5 on PoS

This is the PoS/Kurtosis version of the old PoW experiment:

```text
Contract-state side effect under price-only txpool admission
```

The script uses the running `geth-lighthouse-devnet` enclave, discovers the
Kurtosis-mapped EL RPC port, funds the old experiment accounts, deploys the
selected experiment contract, and sends the A1-A4 / X1-X4 workload.

Experiment cases:

```text
sender-balance-exhaustion
                      intended main case:
                      X1-X4 call ValueForwarder.forwardTo(a) with value=10 ETH.
                      The contract internally forwards msg.value to a. X starts
                      with 17 ETH, so X1 consumes the sender's spendable balance
                      through value + gas cost.

contract-state-drain  original case:
                      X1-X4 call OneShotDrain.drain(); X1 sets drained=true,
                      and X2-X4 revert because contract state changed.

transfer-all          transfer-contract case:
                      X1-X4 call TransferVault.transferTo(a, 17 ETH).
                      This is now a legacy/control case: it transfers pre-funded
                      contract balance, not x's own msg.value.
```

Contract deployment supports two paths:

```text
web3  iBatch-style Web3.py deployment:
      Solidity source -> solcx compile_source() -> contract.constructor(...).transact()

raw   fallback signed raw creation transaction with built-in equivalent bytecode
```

The default `--deploy-mode auto` tries `web3` first and falls back to `raw` only
when Web3/Solidity compiler dependencies are missing. To force the iBatch-style
path for mentor-facing runs, use:

```bash
python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_pos.py --deploy-mode web3
```

Install the Web3/iBatch-style dependencies with:

```bash
python3 -m pip install web3 py-solc-x
python3 -c "from solcx import install_solc; install_solc('0.8.20')"
```

Unlike the PoW version, this script does not call `miner_start` or
`miner_stop`. Blocks are produced by Lighthouse validators, so the script sends
transactions quickly after a fresh block and then waits for receipts.

Expected result for the intended sender-balance case:

```text
after A1-A4:
  A1-A4 pending

after X1-X4:
  A1-A4 dropped/not pending
  X1 pending
  X2-X4 dropped/not pending under sender balance/upfront-cost constraints
  (if a txpool build admits all X calls, the script reports that separately)

after PoS block production:
  X1 status = 0x1
  X2-X4 have no successful receipt
  x balance is below the next X transaction upfront cost
```

Run from WSL:

```bash
cd /home/wh/ETH/pos-exp/go-ethereum-pos
python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_pos.py
```

Run the intended sender-balance case with Web3/iBatch-style deployment:

```bash
python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_pos.py \
  --case sender-balance-exhaustion \
  --deploy-mode web3
```

Run the same intended case through raw fallback bytecode:

```bash
python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_pos.py \
  --case sender-balance-exhaustion \
  --deploy-mode raw
```

Run the legacy pre-funded contract-balance case:

```bash
python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_pos.py \
  --case transfer-all \
  --deploy-mode web3
```

Run the EIP-7702 Alice-overdraft case proposed by the advisor:

```bash
python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_7702_pos.py
```

By default, official Geth limits EIP-7702 delegated accounts to one in-flight
transaction. To run the relaxed-control version that disables this limit for the
experiment, rebuild `saferad-geth-pos:dev` after the txpool changes and launch
Kurtosis with:

```bash
cd /home/wh/ETH/kurtosis-pos-exp
kurtosis run --enclave geth-lighthouse-devnet \
  github.com/ethpandaops/ethereum-package \
  --args-file ./network_params_7702_relaxed.yaml \
  --image-download missing
```

The relaxed-control config adds:

```text
--txpool.allowdelegatedinflight
```

This case deploys `Alice7702Drain`, sends an EIP-7702 set-code transaction that
delegates Alice (`a`) to that implementation, then sends X1-X4 from Alice to
Alice with `tx.value=0`. X1 executes `drainAll(x)` in Alice's account context,
transferring Alice's remaining spendable balance to `x`. With the default
parameters, Alice starts at 10 ETH and each X transaction has a 2 ETH gas cap,
so X1 should leave Alice with only the gas refund, below the next X transaction
upfront cost.

To avoid confusion around the direct `from=Alice, to=Alice` self-call trigger,
the 7702 script also supports an `alice-helper` variant:

```bash
python3 devtools/mpfuzz_style_exp5_pos/mp_exp5_7702_pos.py \
  --case alice-helper
```

This variant still delegates Alice to `Alice7702Drain`, but also deploys
`Alice7702DrainTrigger`. X1-X4 are sent as top-level transactions from Alice to
the helper contract, and the helper calls Alice's delegated `drainAll(x)`.
Thus the top-level transaction target is a third-party contract, while the ETH
movement still comes from Alice's 7702 account context and goes to `x`.
upfront cost.

Expected EIP-7702 result:

```text
after A1-A4:
  A1-A4 pending

after X1-X4:
  ideally X1-X4 pending and A1-A4 evicted by price-only admission
  if Geth's EIP-7702 delegated-sender txpool limits reject later X txs, the
  script reports CHECK instead of PASS

after PoS block production:
  X1 status = 0x1
  X2-X4 have no receipt
  Alice balance is below the next X transaction upfront cost
```

The running Geth must be started with a four-slot price-only pool, for example:

```text
--txpool.nolocals
--txpool.pricelimit=1
--txpool.pricebump=1
--txpool.globalslots=4
--txpool.globalqueue=0
--txpool.accountslots=4
--txpool.accountqueue=0
--txpool.admissionpolicy=price-only
```
