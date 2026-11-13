"""Erigon 3.5.0 read-only pool checkpoints; RPC omits BaseFee transactions."""
import hashlib
import json
import subprocess
import time
from pathlib import Path


def capture(rpc, container, out, name):
    out = Path(out)
    started = int(time.time() * 1000)
    block = rpc.height()
    trace_file = out / ("erigon_trace_" + name + ".log")
    with trace_file.open("w", encoding="utf-8") as handle:
        result = subprocess.run(["docker", "logs", "--timestamps", container],
                                stdout=handle, stderr=subprocess.STDOUT, text=True)
    trace_ok = result.returncode == 0
    locations = {}
    errors = {}
    counts = {"pending": None, "baseFee": "UNAVAILABLE", "queued": None}
    try:
        status = rpc.call("txpool_status")
        counts["pending"] = int(status["pending"], 16)
        counts["queued"] = int(status["queued"], 16)
    except (KeyError, ValueError, OSError, RuntimeError) as exc:
        errors["status"] = str(exc)
    try:
        content = rpc.call("txpool_content")
        if not isinstance(content, dict) or "pending" not in content or "queued" not in content:
            raise ValueError("incomplete txpool_content shape")
        for pool in ("pending", "queued"):
            for by_nonce in content[pool].values():
                for tx in by_nonce.values():
                    tx_hash = tx.get("hash")
                    if not isinstance(tx_hash, str):
                        raise ValueError("txpool_content entry missing hash")
                    key = tx_hash.lower()
                    if key in locations:
                        raise ValueError("duplicate hash in txpool_content")
                    locations[key] = pool.upper()
    except (KeyError, ValueError, OSError, RuntimeError) as exc:
        errors["content"] = str(exc)
        locations = {}
    finished_block = rpc.height()
    row = dict(name=name, block=block, finishedBlock=finished_block,
               snapshotStable=(block == finished_block), startedUnixMillis=started,
               finishedUnixMillis=int(time.time() * 1000),
               traceFile=trace_file.name, traceComplete=trace_ok,
               traceSha256=hashlib.sha256(trace_file.read_bytes()).hexdigest(),
               counts=counts, locations=locations, errors=errors,
               baseFeeGlobalCount="UNAVAILABLE",
               rpcLayers=["PENDING", "QUEUED"])
    (out / ("pool_" + name + ".json")).write_text(json.dumps(row, indent=2) + "\n")
    return row
