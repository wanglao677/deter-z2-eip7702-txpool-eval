/* SPDX-License-Identifier: Apache-2.0 */
package org.hyperledger.besu.ethereum.eth.transactions.layered;

import java.util.Optional;
import org.hyperledger.besu.crypto.SignatureAlgorithmFactory;
import org.hyperledger.besu.datatypes.Address;
import org.hyperledger.besu.datatypes.Wei;
import org.hyperledger.besu.ethereum.core.TransactionTestFixture;
import org.hyperledger.besu.ethereum.core.Util;
import org.hyperledger.besu.ethereum.eth.transactions.*;
import org.hyperledger.besu.metrics.noop.NoOpMetricsSystem;

/** Tiny in-memory layer exercise, without a node, RPC, mining or attack workload. */
public class LayerTraceHarness {
  public static void main(String[] args) {
    var key = SignatureAlgorithmFactory.getInstance().generateKeyPair();
    var sender = Util.publicKeyToAddress(key.getPublicKey());
    var first = new PendingTransaction.Remote(new TransactionTestFixture().nonce(0)
        .sender(sender).to(Optional.of(Address.ZERO)).gasPrice(Wei.of(3)).createTransaction(key));
    var tail = new PendingTransaction.Remote(new TransactionTestFixture().nonce(1)
        .sender(sender).to(Optional.of(Address.ZERO)).gasPrice(Wei.of(3)).createTransaction(key));
    var otherKey = SignatureAlgorithmFactory.getInstance().generateKeyPair();
    var high = new PendingTransaction.Remote(new TransactionTestFixture().nonce(0)
        .sender(Util.publicKeyToAddress(otherKey.getPublicKey())).to(Optional.of(Address.ZERO))
        .gasPrice(Wei.of(10)).createTransaction(otherKey));
    var config = ImmutableTransactionPoolConfiguration.builder()
        .pendingTransactionsLayerMaxCapacityBytes(first.memorySize() * 2L).build();
    var metrics = new TransactionPoolMetrics(new NoOpMetricsSystem());
    var end = new EndLayer(metrics);
    var ready = new ReadyTransactions(config, null, end, metrics, (oldTx, newTx) -> true, new BlobCache());
    ready.add(first, 0, AddReason.MOVE);
    ready.add(tail, 0, AddReason.MOVE);
    ready.add(high, 0, AddReason.MOVE);
    if (ready.getByHash(tail.getHash()).isPresent()) throw new AssertionError("highest nonce not removed");
    if (ready.getByHash(first.getHash()).isEmpty()) throw new AssertionError("candidate unexpectedly removed");
    if (ready.getByHash(high.getHash()).isEmpty()) throw new AssertionError("higher price missing");
    System.out.println("STATE: first=present tail=absent high=present");
    ready.remove(first, LayeredRemovalReason.PoolRemovalReason.INVALIDATED);
    ready.reset();
    System.out.println("STATE: reset=" + ready.getAll().size());
  }
}
