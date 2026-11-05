/* SPDX-License-Identifier: Apache-2.0 */
package org.hyperledger.besu.ethereum.eth.transactions.layered;

import org.hyperledger.besu.ethereum.eth.transactions.PendingTransaction;

import java.io.BufferedWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** Research observer. Disabled unless BESU_LAYER_TRACE_FILE is set; never changes pool decisions. */
final class LayerTrace {
  private record BlockContext(long number, String hash) {}

  private static volatile BlockContext block = new BlockContext(-1, "");
  private static final Sink SINK = open();

  private LayerTrace() {}

  static void transition(
      final String event, final PendingTransaction tx, final String layer,
      final String target, final String reason, final String scope,
      final long bytesAfter, final int countAfter) {
    if (SINK == null) return;
    try {
      SINK.offer("\"event\":" + quote(event) + txFields(tx)
          + ",\"layer\":" + quote(layer) + ",\"targetLayer\":" + quote(target)
          + ",\"reason\":" + quote(reason) + ",\"scope\":" + quote(scope)
          + ",\"layerBytesAfter\":" + bytesAfter + ",\"layerCountAfter\":" + countAfter);
    } catch (RuntimeException e) {
      SINK.dropped.incrementAndGet();
    }
  }

  static void choice(
      final PendingTransaction candidate, final PendingTransaction actual,
      final String layer, final String target, final long requestedBytes,
      final int requestedCount, final long bytesBefore, final int countBefore) {
    if (SINK == null) return;
    try {
      SINK.offer("\"event\":\"capacity_choice\"" + txFields(actual)
          + ",\"layer\":" + quote(layer) + ",\"targetLayer\":" + quote(target)
          + ",\"candidateHash\":" + quote(candidate.getHash().toHexString())
          + ",\"candidateSender\":" + quote(candidate.getSender().toHexString())
          + ",\"candidateNonce\":" + quote(Long.toUnsignedString(candidate.getNonce()))
          + ",\"candidateScore\":" + candidate.getScore()
          + ",\"candidatePriority\":" + candidate.hasPriority()
          + ",\"candidateSequence\":" + candidate.getSequence()
          + ",\"requestedBytes\":" + requestedBytes + ",\"requestedCount\":" + requestedCount
          + ",\"layerBytesBefore\":" + bytesBefore + ",\"layerCountBefore\":" + countBefore);
    } catch (RuntimeException e) {
      SINK.dropped.incrementAndGet();
    }
  }

  static void reset(final String layer, final int count) {
    if (SINK != null) SINK.offer("\"event\":\"reset\",\"layer\":" + quote(layer)
        + ",\"previousCount\":" + count);
  }

  static void block(final long number, final String hash, final String event) {
    if (SINK != null) {
      block = new BlockContext(number, hash);
      SINK.offer("\"event\":" + quote(event));
    }
  }

  private static String txFields(final PendingTransaction tx) {
    return ",\"hash\":" + quote(tx.getHash().toHexString())
        + ",\"sender\":" + quote(tx.getSender().toHexString())
        + ",\"nonce\":" + quote(Long.toUnsignedString(tx.getNonce()))
        + ",\"txSequence\":" + tx.getSequence() + ",\"score\":" + tx.getScore()
        + ",\"priority\":" + tx.hasPriority() + ",\"local\":" + tx.isReceivedFromLocalSource()
        + ",\"memoryBytes\":" + tx.memorySize()
        + ",\"maxGasPriceWei\":" + quote(tx.getTransaction().getMaxGasPrice().getAsBigInteger().toString());
  }

  private static String quote(final String s) {
    return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"")
        .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + "\"";
  }

  private static Sink open() {
    final String filename = System.getenv("BESU_LAYER_TRACE_FILE");
    if (filename == null || filename.isBlank()) return null;
    try {
      final Path path = Path.of(filename);
      if (path.getParent() != null) Files.createDirectories(path.getParent());
      final Sink sink = new Sink(Files.newBufferedWriter(path, StandardCharsets.UTF_8,
          StandardOpenOption.CREATE, StandardOpenOption.APPEND));
      sink.worker.start();
      Runtime.getRuntime().addShutdownHook(new Thread(sink::close, "layer-trace-close"));
      return sink;
    } catch (Exception e) {
      System.err.println("BESU_LAYER_TRACE_DISABLED: " + e.getClass().getSimpleName());
      return null;
    }
  }

  private static final class Sink {
    private final String session = UUID.randomUUID().toString();
    private final ArrayBlockingQueue<String> queue = new ArrayBlockingQueue<>(8192);
    private final AtomicLong sequence = new AtomicLong();
    private final AtomicLong dropped = new AtomicLong();
    private final BufferedWriter writer;
    private final Thread worker;
    private volatile boolean closing;
    private volatile boolean failed;

    Sink(final BufferedWriter writer) {
      this.writer = writer;
      worker = new Thread(this::writeLoop, "besu-layer-trace-writer");
      worker.setDaemon(true);
    }

    synchronized void offer(final String fields) {
      try {
        final BlockContext context = block;
        final long seq = sequence.incrementAndGet();
        final String line = "{\"schema\":1,\"session\":" + quote(session)
            + ",\"seq\":" + seq + ",\"unixMillis\":" + System.currentTimeMillis()
            + ",\"monoNanos\":" + System.nanoTime()
            + ",\"poolBlockNumber\":" + context.number()
            + ",\"poolBlockHash\":" + quote(context.hash()) + "," + fields + "}";
        if (closing || failed || !queue.offer(line)) dropped.incrementAndGet();
      } catch (RuntimeException e) {
        dropped.incrementAndGet();
      }
    }

    private void writeLoop() {
      long written = 0;
      long lastFlush = System.nanoTime();
      try (writer) {
        writer.write("{\"schema\":1,\"event\":\"session_start\",\"session\":" + quote(session)
            + ",\"besuCommit\":\"983c53fb797b7e2e8cf940dc0361770705675a93\"}");
        writer.newLine();
        writer.flush();
        while (!closing || !queue.isEmpty()) {
          final String line = queue.poll(100, TimeUnit.MILLISECONDS);
          if (line != null) {
            writer.write(line);
            writer.newLine();
            written++;
          }
          if (System.nanoTime() - lastFlush >= TimeUnit.SECONDS.toNanos(1)) {
            checkpoint("checkpoint", written);
            lastFlush = System.nanoTime();
          }
        }
        checkpoint("session_end", written);
      } catch (Exception e) {
        failed = true;
        System.err.println("BESU_LAYER_TRACE_WRITE_FAILED: " + e.getClass().getSimpleName());
      }
    }

    private void checkpoint(final String event, final long written) throws java.io.IOException {
      writer.write("{\"schema\":1,\"event\":" + quote(event) + ",\"session\":" + quote(session)
          + ",\"unixMillis\":" + System.currentTimeMillis() + ",\"written\":" + written
          + ",\"assigned\":" + sequence.get() + ",\"dropped\":" + dropped.get() + "}");
      writer.newLine();
      writer.flush();
    }

    void close() {
      closing = true;
      try {
        worker.join(5000);
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
      }
    }
  }
}
