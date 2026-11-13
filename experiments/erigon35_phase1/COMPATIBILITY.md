# Erigon 3.5.0 Phase 1 compatibility audit

Checked against the Phase 1 code at 71dc97b, the historical Erigon PoC, and Erigon's v3.5.0 tag (7e8c1eff200590e5cc16604318ab82d25c9a30aa).

| Component | Decision |
| --- | --- |
| G0/G1/G2 setup and funding, Solidity drain/no-drain delegates, legacy transaction helper | Reuse. The no-drain delegate implements the same drainAll(address) ABI but never transfers value; it must be used for every G2 attack transaction. |
| Fixed and deterministic ±10% normal fee plan | Reuse; the helper signs legacy type-0 transactions with per-transaction gasPrice and a constant gas limit. Verify the signed sequence in every group. |
| Receipt and actual gas fee calculation | Reuse gasUsed × effectiveGasPrice for included transactions. Upfront gasPrice × gasLimit is not a paid fee. |
| Besu version check, 8 KB normal/tail data, 150k normal gas, layer trace, LAYERED policy, 13371349 chain ID | Erigon-specific configuration and observer required. The Erigon PoC used 0-byte padding, 21k normal gas, and chain ID 13371350. |
| Txpool admission errors | Erigon may return "pending sub-pool is full". Keep the attempt and classify it as REJECTED; do not count it as a post-admission eviction. |
| Pool layers | Erigon Pending/BaseFee/Queued are not equivalent to Besu Prioritized/Ready/Sparse. Erigon 3.5.0 txpool_content and txpool_status omit BaseFee; a missing hash in those RPCs is insufficient to prove eviction. |
| Capacity deletion | Built-in --txpool.trace.senders logs per-hash insertions, subpool moves, and Deleted txn by nonce with a reason. Capacity-overflow deletion after admission, without later reacceptance or receipt, is positive POOL_EVICTED evidence. Missing/incomplete trace yields UNKNOWN. |

Erigon 3.5.0 release defaults: Pending 10,000, BaseFee 30,000, Queued 30,000; AccountSlots=16 (local transaction handling differs), PriceBump=10%, MaxNonceGap=64, QueuedDormancyDuration=3h. No pool limits are overridden. --rpc.txfeecap=0 retains the historical local-RPC fee-cap override and does not change pool capacity. The historical Erigon observation (7,040 normal absent, 125 attack submissions rejected, 125 first attack inclusions) used RPC/receipt evidence and a different window. It is not confirmed capacity-eviction evidence for this evaluation.

Sources: [pool defaults](https://github.com/erigontech/erigon/blob/v3.5.0/txnprovider/txpool/txpoolcfg/txpoolcfg.go), [JSON-RPC pool view](https://github.com/erigontech/erigon/blob/v3.5.0/rpc/jsonrpc/txpool_api.go), [per-hash trace deletion](https://github.com/erigontech/erigon/blob/v3.5.0/txnprovider/txpool/senders.go), [subpool trace](https://github.com/erigontech/erigon/blob/v3.5.0/txnprovider/txpool/sub_pool.go).
