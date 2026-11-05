#!/usr/bin/env python3
"""Compare real small-layer behavior with original, disabled and enabled observer images."""
import argparse
import json
import subprocess
from pathlib import Path
from audit import replay
from build_image import BASE_IMAGE

HERE = Path(__file__).resolve().parent
JAVA_MAIN = "org.hyperledger.besu.ethereum.eth.transactions.layered.LayerTraceHarness"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", default="besu:26.6.1-layertrace")
    args = p.parse_args()
    root = HERE / ".build" / "verification"
    root.mkdir(parents=True, exist_ok=True)
    trace = root / "trace.jsonl"
    trace.unlink(missing_ok=True)
    results = {}
    for name, image, enabled in (("original", BASE_IMAGE, False), ("observer_disabled", args.image, False), ("observer_enabled", args.image, True)):
        cmd = ["docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{HERE / 'tests'}:/test:ro", "-v", f"{root}:/out"]
        if enabled:
            cmd += ["-e", "BESU_LAYER_TRACE_FILE=/out/trace.jsonl"]
        cmd += [image, "-c", "mkdir /tmp/testclasses && javac -proc:none -cp '/opt/besu/lib/*' -d /tmp/testclasses /test/LayerTraceHarness.java && java -cp '/tmp/testclasses:/opt/besu/lib/*' " + JAVA_MAIN]
        result = subprocess.run(cmd, text=True, capture_output=True)
        (root / f"{name}.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"{name} harness failed:\n{result.stdout}{result.stderr}")
        results[name] = [line for line in result.stdout.splitlines() if line.startswith("STATE:")]
    if not results["original"] or len({tuple(value) for value in results.values()}) != 1:
        raise AssertionError(f"State comparison failed: {results}")
    evidence = replay(trace)
    if not evidence["evidenceComplete"]:
        raise AssertionError(evidence["issues"])
    choices = evidence["capacityChoices"]
    if not any(c["candidateNonce"] == "0" and c["nonce"] == "1" for c in choices):
        raise AssertionError("Missing candidate nonce 0 / actual eviction nonce 1 evidence")
    if not any(e["event"] == "drop" for e in evidence["history"]):
        raise AssertionError("Missing final pool drop evidence")
    if not any(e["event"] == "leave" and e.get("reason") == "invalidated" for e in evidence["history"]):
        raise AssertionError("Missing invalidation evidence")
    image_id = json.loads(subprocess.check_output(["docker", "image", "inspect", args.image], text=True))[0]["Id"]
    report = dict(passed=True, imageId=image_id, stateComparison=results, traceIssues=evidence["issues"],
                  note="Small in-memory layer harness only; not a full PoS experiment or timing-equivalence proof")
    (root / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
