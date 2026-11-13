#!/usr/bin/env python3
"""Prepare, run, or collect Besu 26.6.1 layertrace experiments."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from audit import audit
from report import summarize_evaluation, summarize_group
from build_image import COMMIT
from funder import create_funder, load_funder
from normal_fee import parameters as normal_fee_parameters
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
    if cap_wei > 2**64 - 1:
        raise RuntimeError("rpcTxFeeCapEth exceeds Besu 26.6.1's unsigned 64-bit Wei CLI limit; use at most 18 ETH")
    fees = {
        "deployment": 2_000_000 * config["setupGasPriceWei"],
        "authorization": 250_000 * config["setupGasPriceWei"],
        "funding": 21_000 * config["setupGasPriceWei"],
        "normal": config["normalGas"] * normal_fee_parameters(config)["maximumWei"],
        "attack": config["attackGas"] * config["priceUnitWei"] * config["attackPriceMultiplier"],
    }
    for stage, fee in fees.items():
        if fee > cap_wei:
            raise RuntimeError(f"{stage} maximum fee {fee} Wei exceeds RPC fee cap {cap_wei} Wei")
    return cap_wei


def network_config(config, image, funder_address=None):
    fee_cap_wei = rpc_tx_fee_cap_wei(config)
    network_params = dict(network_id="13371349", seconds_per_slot=config["secondsPerSlot"])
    if config.get("evaluationGroups"):
        if not funder_address:
            raise RuntimeError("Phase 1 needs a local devnet funder; run init-funder first")
        balance = config.get("funderGenesisBalanceEth")
        minimum = config["normalSenders"] * config["normalFundEth"] + config["attackSenders"] * config["attackFundEth"] + 1
        if type(balance) is not int or balance < minimum:
            raise RuntimeError("funderGenesisBalanceEth must cover all setup funding plus a gas reserve")
        # The pinned package json.encodes this field into an unquoted shell export.
        # It must be a JSON string: a map loses its inner quotes in the shell.
        network_params["prefunded_accounts"] = json.dumps(
            {funder_address: {"balance": f"{balance}ETH"}}, separators=(",", ":"))
    # JSON is valid YAML, so no additional YAML dependency is required.
    return dict(participants=[dict(el_type="besu", el_image=image, cl_type="lighthouse",
        cl_image=config["lighthouseImage"], count=1,
        el_extra_env_vars={"BESU_LAYER_TRACE_FILE": TRACE},
        el_extra_params=["--tx-pool=LAYERED", "--rpc-tx-feecap=" + str(fee_cap_wei)])],
        network_params=network_params, additional_services=[])


def validate_besu_cli(image, params, version):
    # --version still invokes Besu's option converters, without starting a node.
    name = "besu-layertrace-cli-" + uuid.uuid4().hex
    command = ["docker", "run", "--rm", "--name", name, "--network", "none",
               "--hostname", "localhost", "--memory", "1g", "--entrypoint", "/opt/besu/bin/besu",
               image, *params, "--version"]
    print("Preflight: checking generated CLI options with the actual Besu image...", flush=True)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as error:
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Besu CLI preflight timed out; check container {name}") from error
        raise RuntimeError("Besu CLI preflight timed out; no enclave was created") from error
    message = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode or f"besu/v{version}/" not in result.stdout:
        raise RuntimeError(f"Besu CLI preflight failed; no enclave was created:\n{message}")
    print("Preflight: Besu CLI options accepted.", flush=True)
    return message


def prepare(args, config):
    network = network_config(config, args.image, getattr(args, "funder_address", None))
    if config.get("evaluationGroups"):
        if load_funder(args.funder_file)["address"] != args.funder_address:
            raise RuntimeError("Phase 1 funder changed; rerun init-funder and plan")
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
    cli_preflight = validate_besu_cli(args.image, network["participants"][0]["el_extra_params"], config["besuVersion"])
    # The slim evaluation repo has no go.mod; compile its exact helper using the existing geth module.
    if not (args.geth_repo / "go.mod").is_file():
        raise RuntimeError(f"Missing geth Go module: {args.geth_repo}")
    if not args.key_csv.is_file():
        raise RuntimeError(f"Missing local test key CSV: {args.key_csv}")
    if not config.get("evaluationGroups"):
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
    manifest = dict(configSha256=sha(args.config), helperSourceSha256=sha(LEGACY / "eip7702_scale_helper.go"),
        helperSha256=sha(helper), goVersion=output([str(args.go), "version"]),
        gethCommit=output(["git", "-C", str(args.geth_repo), "rev-parse", "HEAD"]),
        gethModuleSha256=sha(args.geth_repo / "go.mod"), imageId=image["Id"], imageLabels=image["Config"].get("Labels", {}),
        gethDirtyStatus=output(["git", "-C", str(args.geth_repo), "status", "--short"]),
        verification=verification, cliPreflight=cli_preflight,
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


def evaluation_groups(config, requested):
    expected = {
        "G0": dict(mode="baseline", delegateCase="alice-sender"),
        "G1": dict(mode="attack", delegateCase="alice-sender"),
        "G2": dict(mode="attack", delegateCase="alice-no-drain"),
    }
    if config.get("evaluationGroups") != expected:
        raise RuntimeError("Phase 1 requires the fixed G0/G1/G2 mode and delegate mapping")
    if config["besuVersion"] != "26.6.1":
        raise RuntimeError("Phase 1 requires Besu 26.6.1")
    if not config["poolPolicy"].startswith("LAYERED release defaults"):
        raise RuntimeError("Phase 1 requires the release-default LAYERED pool")
    normal_fee_parameters(config)
    for field in ("attackFirstPaddingBytes", "attackTailPaddingBytes"):
        if type(config[field]) is not int or config[field] < 0:
            raise RuntimeError(f"{field} must be a non-negative byte count")
    selected = list(expected) if requested == "all" else [requested]
    return [(group, expected[group]["mode"], expected[group]["delegateCase"]) for group in selected]


def run_groups(args, config, helper, package, manifest, groups, evaluation=False):
    os.umask(0o077)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = args.out or HERE / "runs" / (config["name"] + "_" + args.seed + "_" + stamp)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    save(root / "runtime_manifest.json", manifest)
    save(root / "config.json", config)
    save(root / "network.json", network_config(config, args.image, args.funder_address))
    print(f"实验目录：{root}", flush=True)
    for group, mode, delegate_case in groups:
        out = root / group
        out.mkdir()
        suffix = group.lower() if evaluation else ("b" if mode == "baseline" else "a")
        enclave = f"bz2-trace-{stamp.replace('_', '-')}-{uuid.uuid4().hex[:6]}-{suffix}"
        save(out / "network_identity.json", dict(enclave=enclave))
        print(f"启动 {group}：{enclave}", flush=True)
        with (out / "network.log").open("w") as log:
            launched = subprocess.run(["kurtosis", "run", "--enclave", enclave, "--args-file", str(root / "network.json"), str(package)], stdout=log, stderr=subprocess.STDOUT)
        if launched.returncode:
            raise RuntimeError(f"Network startup failed; preserved {out / 'network.log'} and enclave {enclave}")
        port = output(["kurtosis", "port", "print", "--format", "number", enclave, SERVICE, "rpc"])
        rpc = f"http://127.0.0.1:{port}"
        save(out / "network_identity.json", dict(enclave=enclave, rpc=rpc, container=container(enclave)))
        for attempt in range(30):
            if subprocess.run(["docker", "exec", container(enclave), "test", "-s", TRACE]).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Observer did not initialize; no workload submitted")
        command = [sys.executable, "-u", str(HERE / "workload.py"), "--mode", mode,
                   "--delegate-case", delegate_case, "--config", str(root / "config.json"),
                   "--rpc", rpc, "--out", str(out), "--helper", str(helper),
                   "--key-csv", str(args.key_csv), "--seed", args.seed]
        if evaluation:
            command += ["--group", group, "--funder-file", str(args.funder_file)]
        with (out / "run.log").open("w") as log:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    log.flush()
                code = process.wait()
        time.sleep(2)
        result = collect(enclave, out)
        if code:
            raise RuntimeError(f"Workload failed. Evidence saved in {out}; enclave preserved for diagnosis")
        marker = json.loads((out / f"window{config['fullWindowBlocks']}_marker.json").read_text())
        if not result["evidenceComplete"] or result["checkpoint"].get("unixMillis", 0) < marker["unixMillis"]:
            raise RuntimeError(f"Layer trace incomplete/stale; inspect {out / 'layer_audit.json'}; enclave preserved")
        if evaluation:
            summarize_group(out)
            summarize_evaluation(root)
        subprocess.run(["kurtosis", "enclave", "stop", enclave], check=True)
    if evaluation:
        print(f"完成 Phase 1 评估：{root}", flush=True)
    else:
        save(root / "pair_summary.json", {mode: json.loads((root / mode / "summary.json").read_text()) for mode in ("baseline", "attack")})
        print(f"完成配对观测：{root}", flush=True)


def pair(args, config, helper, package, manifest):
    run_groups(args, config, helper, package, manifest,
               [("baseline", "baseline", "alice-sender"), ("attack", "attack", "alice-sender")])


def evaluation(args, config, helper, package, manifest):
    run_groups(args, config, helper, package, manifest,
               evaluation_groups(config, args.group), evaluation=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("init-funder", "plan", "prepare", "pair", "evaluation", "collect"))
    p.add_argument("--seed", default="seed01")
    p.add_argument("--config", type=Path, default=HERE / "config.json")
    p.add_argument("--group", choices=("G0", "G1", "G2", "all"), default="all")
    p.add_argument("--image", default="besu:26.6.1-layertrace")
    p.add_argument("--geth-repo", type=Path, default=Path.home() / "ETH/pos-exp/go-ethereum-pos")
    p.add_argument("--go", type=Path, default=Path("/usr/local/go/bin/go"))
    p.add_argument("--key-csv", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--enclave")
    args = p.parse_args()
    args.config = args.config.resolve()
    config = json.loads(args.config.read_text())
    phase1 = bool(config.get("evaluationGroups"))
    args.funder_file = BUILD / "phase1_funder.json" if phase1 else None
    args.funder_address = None
    if args.action == "init-funder":
        evaluation_groups(config, "all")
        print("Local Phase 1 devnet funder address:", create_funder(args.funder_file))
        return
    if args.action == "evaluation":
        evaluation_groups(config, args.group)
    if args.action == "pair" and phase1:
        p.error("Phase 1 config requires the evaluation action")
    if phase1 and args.action != "collect":
        args.funder_address = load_funder(args.funder_file)["address"]
    if args.action == "plan":
        print(json.dumps(dict(config=config, network=network_config(config, args.image, args.funder_address)), indent=2))
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
    elif args.action == "evaluation":
        evaluation(args, config, helper, package, manifest)
    else:
        pair(args, config, helper, package, manifest)


if __name__ == "__main__":
    main()
