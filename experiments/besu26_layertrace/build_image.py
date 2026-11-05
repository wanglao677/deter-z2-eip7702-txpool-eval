#!/usr/bin/env python3
"""Build only the observed classes against the pinned Besu release classpath."""
import argparse
import hashlib
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMIT = "983c53fb797b7e2e8cf940dc0361770705675a93"
BASE_IMAGE = "hyperledger/besu@sha256:ac85d23b4250308ce5f6d004fff7fa6779f82932d1ba32fd3d4811531d43d0bc"
PACKAGE = "ethereum/eth/src/main/java/org/hyperledger/besu/ethereum/eth/transactions/layered"
FILES = ["AbstractTransactionsLayer.java", "LayeredPendingTransactions.java", "EndLayer.java"]


def prepare():
    root = HERE / ".build" / "image"
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    # A git apply inside an enclosing worktree can silently skip outside-prefix paths.
    # Give the downloaded source its own root and assert each hook was installed.
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    hashes = {}
    for name in FILES:
        relative = f"{PACKAGE}/{name}"
        with urllib.request.urlopen(f"https://raw.githubusercontent.com/besu-eth/besu/{COMMIT}/{relative}", timeout=60) as response:
            data = response.read()
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[relative] = hashlib.sha256(data).hexdigest()
    subprocess.run(["git", "apply", "--check", str(HERE / "besu-26.6.1-layertrace.patch")], cwd=source, check=True)
    subprocess.run(["git", "apply", str(HERE / "besu-26.6.1-layertrace.patch")], cwd=source, check=True)
    for name in FILES:
        if "LayerTrace." not in (source / PACKAGE / name).read_text():
            raise RuntimeError(f"Observer patch was not applied to {name}")
    shutil.copy2(HERE / "LayerTrace.java", source / PACKAGE / "LayerTrace.java")
    (root / ".dockerignore").write_text("source/.git\n")
    dockerfile = f'''FROM {BASE_IMAGE}
USER root
COPY source /tmp/layertrace-source
RUN mkdir /tmp/layertrace-classes && \\
    javac -proc:none -cp '/opt/besu/lib/*' -d /tmp/layertrace-classes /tmp/layertrace-source/{PACKAGE}/*.java && \\
    jar --update --file /opt/besu/lib/besu-ethereum-eth-26.6.1.jar -C /tmp/layertrace-classes .
LABEL research.besu.commit="{COMMIT}" research.layertrace.schema="1"
'''
    (root / "Dockerfile").write_text(dockerfile)
    manifest = dict(besuCommit=COMMIT, baseImage=BASE_IMAGE, sourceSha256=hashes,
                    patchedSourceSha256={name: hashlib.sha256((source / PACKAGE / name).read_bytes()).hexdigest() for name in FILES},
                    buildScriptSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    for name in ("LayerTrace.java", "besu-26.6.1-layertrace.patch"):
        manifest[name + "Sha256"] = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
    (root / "build_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="besu:26.6.1-layertrace")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = prepare()
    if not args.prepare_only:
        subprocess.run(["docker", "build", "--label", "research.layertrace.build=" + hashlib.sha256((root / "build_manifest.json").read_bytes()).hexdigest(), "-t", args.image, str(root)], check=True)
        subprocess.run(["docker", "run", "--rm", args.image, "--version"], check=True)
    print(f"Build manifest: {root / 'build_manifest.json'}")


if __name__ == "__main__":
    main()
