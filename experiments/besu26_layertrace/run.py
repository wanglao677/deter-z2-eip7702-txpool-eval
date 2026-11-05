#!/usr/bin/env python3
"""Prepare or run two fresh, sequential Besu 26.6.1 observation networks."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from audit import audit
from build_image import COMMIT
from workload import LEGACY, save, validate_funder_key

HERE = Path(__file__).resolve().parent
BUILD = HERE / ".build"
SERVICE = "el-1-besu-lighthouse"
TRACE = "/tmp/besu-layertrace/events.jsonl"


def output(command):
    return subprocess.check_output(command, text=True).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rpc_tx_fee_cap_wei(config):
    cap_eth = config["rpcTxFeeCapEth"]
    if type(cap_eth) is not int or cap_eth <= 0:
        raise RuntimeError("rpcTxFeeCapEth must be a positive integer number of ETH")
    # Besu 26.6.1 accepts --rpc-tx-feecap in Wei, not ETH.
    cap_wei = cap_eth * 10**18
    fees = {
        "deployment": 2_000_000 * config["setupGasPriceWei"],
        "authorization": 250_000 * config["setupGasPriceWei"],
        "funding": 21_000 * config["setupGasPriceWei"],
        "normal": config["normalGas"] * config["priceUnitWei"] * config["normalPriceMultiplier"],
        "attack": config["attackGas"] * config["priceUnitWei"] * config["attackPriceMultiplier"],
    }
    for stage, fee in fees.items():
        if fee > cap_wei:
            raise RuntimeError(f"{stage} maximum fee {fee} Wei exceeds RPC fee cap {cap_wei} Wei")
    return cap_wei


def network_config(config, image):
    fee_cap_wei = rpc_tx_fee_cap_wei(config)
    # JSON is valid YAML, so no additional YAML dependency is required.
    return dict(participants=[dict(el_type="besu", el_image=image, cl_type="lighthouse",
        cl_image=config["lighthouseImage"], count=1,
        el_extra_env_vars={"BESU_LAYER_TRACE_FILE": TRACE},
        el_extra_params=["--tx-pool=LAYERED", "--rpc-tx-feecap=" + str(fee_cap_wei)])],
        network_params=dict(network_id="13371349", seconds_per_slot=config["secondsPerSlot"]), additional_services=[])


def prepare(args, config):
    network = network_config(config, args.image)
    for tool in ("docker", "git", "kurtosis"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing command: {tool}")
    from web3 import Web3  # Check in this interpreter, before creating any network.
    import solcx
    if "0.8.20" not in {str(v) for v in solcx.get_installed_solc_versions()}:
        raise RuntimeError("Install solc first: python -c \"import solcx; solcx.install_solc('0.8.20')\"")
    image = json.loads(output(["docker", "image", "inspect", args.image]))[0]
    if image["Config"].get("Labels", {}).get("research.besu.commit") != COMMIT:
        raise RuntimeError("Build the observation image with build_image.py first")
    verification_path = BUILD / "verification" / "verification.json"
    verification = json.loads(verification_path.read_text()) if verification_path.exists() else {}
    if not verification.get("passed") or verification.get("imageId") != image["Id"]:
        raise RuntimeError("Run verify.py on this exact image before preparing a full experiment")
    # The slim evaluation repo has no go.mod; compile its exact helper using the existing geth module.
    if not (args.geth_repo / "go.mod").is_file():
        raise RuntimeError(f"Missing geth Go module: {args.geth_repo}")
    if not args.key_csv.is_file():
        raise RuntimeError(f"Missing local test key CSV: {args.key_csv}")
    validate_funder_key()
    BUILD.mkdir(parents=True, exist_ok=True)
    helper = BUILD / "eip7702_scale_helper"
    subprocess.run([str(args.go), "build", "-o", str(helper), str(LEGACY / "eip7702_scale_helper.go")], cwd=args.geth_repo, check=True)
    package = BUILD / "ethereum-package"
    if not (package / ".git").exists():
        subprocess.run(["git", "clone", "--no-checkout", "--filter=blob:none", "https://github.com/ethpandaops/ethereum-package.git", str(package)], check=True)
    # Never mutate an arbitrary user checkout: this package belongs to our ignored build directory.
    subprocess.run(["git", "-C", str(package), "fetch", "--depth=1", "origin", config["ethereumPackageCommit"]], check=True)
    subprocess.run(["git", "-C", str(package), "checkout", "--detach", config["ethereumPackageCommit"]], check=True)
    manifest = dict(configSha256=sha(HERE / "config.json"), helperSourceSha256=sha(LEGACY / "eip7702_scale_helper.go"),
        helperSha256=sha(helper), goVersion=output([str(args.go), "version"]),
        gethCommit=output(["git", "-C", str(args.geth_repo), "rev-parse", "HEAD"]),
        gethModuleSha256=sha(args.geth_repo / "go.mod"), imageId=image["Id"], imageLabels=image["Config"].get("Labels", {}),
        gethDirtyStatus=output(["git", "-C", str(args.geth_repo), "status", "--short"]),
        verification=verification,
        packageCommit=output(["git", "-C", str(package), "rev-parse", "HEAD"]),
        kurtosisVersion=output(["kurtosis", "version"]), pythonVersion=sys.version,
        pipFreeze=output([sys.executable, "-m", "pip", "freeze"]),
        gitCommit=output(["git", "-C", str(HERE), "rev-parse", "HEAD"]))
    save(BUILD / "runtime_manifest.json", manifest)
    save(BUILD / "network.json", network)
    return helper, package, manifest


def container(enclave):
    ids = output(["docker", "ps", "-aq", "--filter", f"label=com.kurtosistech.enclave-name={enclave}",
                  "--filter", f"label=com.kurtosistech.id={SERVICE}"]).splitlines()
    if len(ids) != 1:
        raise RuntimeError(f"Expected one Besu container in {enclave}; got {len(ids)}")
    return ids[0]


def collect(enclave, out):
    cid = container(enclave)
    details = json.loads(output(["docker", "inspect", cid]))[0]
    save(out / "node_runtime.json", dict(containerId=cid, imageId=details["Image"], state=details["State"],
         command=details["Config"]["Cmd"], entrypoint=details["Config"]["Entrypoint"]))
    with (out / "besu.log").open("w") as handle:
        subprocess.run(["docker", "logs", cid], stdout=handle, stderr=subprocess.STDOUT, check=True)
    subprocess.run(["docker", "cp", f"{cid}:{TRACE}", str(out / "layertrace.jsonl")], check=True)
    try:
        port = output(["kurtosis", "port", "print", "--format", "number", enclave, SERVICE, "metrics"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=15) as response:
            (out / "metrics.prom").write_bytes(response.read())
    except Exception as error:
        save(out / "metrics_error.json", dict(error=str(error)))
    paths = [out / "normal.jsonl", out / "attack.jsonl"]
    result = audit(out / "layertrace.jsonl", out, [p for p in paths if p.exists()])
    return result


def pair(args, config, helper, package, manifest):
    os.umask(0o077)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = args.out or HERE / "runs" / (config["name"] + "_" + args.seed + "_" + stamp)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    save(root / "runtime_manifest.json", manifest)
    save(root / "config.json", config)
    save(root / "network.json", network_config(config, args.image))
    print(f"实验目录：{root}", flush=True)
    for mode, suffix in (("baseline", "b"), ("attack", "a")):
        out = root / mode
        out.mkdir()
        enclave = f"bz2-trace-{stamp.replace('_', '-')}-{suffix}"
        save(out / "network_identity.json", dict(enclave=enclave))
        print(f"启动 {mode}：{enclave}", flush=True)
        with (out / "network.log").open("w") as log:
            launched = subprocess.run(["kurtosis", "run", "--enclave", enclave, "--args-file", str(root / "network.json"), str(package)], stdout=log, stderr=subprocess.STDOUT)
        if launched.returncode:
            raise RuntimeError(f"Network startup failed; preserved {out / 'network.log'} and enclave {enclave}")
        port = output(["kurtosis", "port", "print", "--format", "number", enclave, SERVICE, "rpc"])
        rpc = f"http://127.0.0.1:{port}"
        save(out / "network_identity.json", dict(enclave=enclave, rpc=rpc, container=container(enclave)))
        # Fail before sending if the observer failed to initialize.
        for attempt in range(30):
            if subprocess.run(["docker", "exec", container(enclave), "test", "-s", TRACE]).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Observer did not initialize; no workload submitted")
        command = [sys.executable, "-u", str(HERE / "workload.py"), "--mode", mode, "--rpc", rpc,
                   "--out", str(out), "--helper", str(helper), "--key-csv", str(args.key_csv), "--seed", args.seed]
        with (out / "run.log").open("w") as log:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    log.flush()
                code = process.wait()
        time.sleep(2)  # Allow the asynchronous observer to flush a checkpoint.
        result = collect(enclave, out)
        if code:
            raise RuntimeError(f"Workload failed. Evidence saved in {out}; enclave preserved for diagnosis")
        marker = json.loads((out / f"window{config['fullWindowBlocks']}_marker.json").read_text())
        if not result["evidenceComplete"] or result["checkpoint"].get("unixMillis", 0) < marker["unixMillis"]:
            raise RuntimeError(f"Layer trace incomplete/stale; inspect {out / 'layer_audit.json'}; enclave preserved")
        # Stop only the enclave created by this invocation, after all evidence has been copied.
        subprocess.run(["kurtosis", "enclave", "stop", enclave], check=True)
    save(root / "pair_summary.json", {mode: json.loads((root / mode / "summary.json").read_text()) for mode in ("baseline", "attack")})
    print(f"完成配对观测：{root}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("plan", "prepare", "pair", "collect"))
    p.add_argument("--seed", default="seed01")
    p.add_argument("--image", default="besu:26.6.1-layertrace")
    p.add_argument("--geth-repo", type=Path, default=Path.home() / "ETH/pos-exp/go-ethereum-pos")
    p.add_argument("--go", type=Path, default=Path("/usr/local/go/bin/go"))
    p.add_argument("--key-csv", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--enclave")
    args = p.parse_args()
    config = json.loads((HERE / "config.json").read_text())
    if args.action == "plan":
        print(json.dumps(dict(config=config, network=network_config(config, args.image)), indent=2))
        return
    if args.action == "collect":
        if not args.enclave or not args.out:
            p.error("collect requires --enclave and --out (existing run directory)")
        print(json.dumps(collect(args.enclave, args.out), indent=2))
        return
    args.key_csv = args.key_csv or args.geth_repo / "devtools/mpfuzz_style_exp5_pos/key_prive_exp5.csv"
    helper, package, manifest = prepare(args, config)
    if args.action == "prepare":
        print("准备完成：依赖、helper、固定版本镜像和网络配置已检查；未启动网络。")
    else:
        pair(args, config, helper, package, manifest)


if __name__ == "__main__":
    main()
