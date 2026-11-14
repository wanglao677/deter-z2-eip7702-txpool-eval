#!/usr/bin/env python3
"""Prepare, run, or re-audit Erigon 3.5.0 Phase 1 on isolated Kurtosis PoS devnets."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BESU = HERE.parent / "besu26_layertrace"
LEGACY = HERE.parents[1] / "devtools" / "mpfuzz_style_exp5_pos"
sys.path.insert(1, str(BESU))
sys.path.insert(2, str(LEGACY))
from funder import create_funder, load_funder
from normal_fee import parameters as fee_parameters
from report import summarize_evaluation, summarize_group
import mp_exp5_7702_scale_pos as scale

BUILD = HERE / ".build"
SERVICE = "el-1-erigon-lighthouse"
RPC_PORT = "ws-rpc"  # ethereum-package c4a8e15 exposes Erigon HTTP and WS on this port ID.
GROUPS = {"G0": ("baseline", "alice-sender"),
          "G1": ("attack", "alice-sender"),
          "G2": ("attack", "alice-no-drain")}


def output(command):
    return subprocess.check_output(command, text=True).strip()


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(config):
    required = {"G0": dict(mode="baseline", delegateCase="alice-sender"),
                "G1": dict(mode="attack", delegateCase="alice-sender"),
                "G2": dict(mode="attack", delegateCase="alice-no-drain")}
    if config.get("evaluationGroups") != required:
        raise RuntimeError("G0/G1/G2 semantics changed")
    expected = dict(erigonVersion="3.5.0", chainId=13371350, secondsPerSlot=300,
                    normalSenders=55, attackSenders=125, txsPerSender=128,
                    normalGas=21000, attackGas=500000, normalPaddingBytes=0,
                    attackFirstPaddingBytes=0, attackTailPaddingBytes=0,
                    priceUnitWei=571428571428, normalPriceMultiplier=3,
                    attackPriceMultiplier=7, normalSendOrder="account",
                    attackSendOrder="round-robin", rpcTxFeeCap=0,
                    commonWindowBlocks=1, fullWindowBlocks=10,
                    followupWindowBlocks=12)
    for name, wanted in expected.items():
        if config.get(name) != wanted:
            raise RuntimeError(f"Erigon Phase 1 requires {name}={wanted}")
    if config.get("erigonImage") != "erigontech/erigon:v3.5.0":
        raise RuntimeError("Erigon image must be pinned to v3.5.0")
    if not config.get("poolPolicy", "").startswith("Erigon 3.5.0 release defaults"):
        raise RuntimeError("Do not override Erigon pool capacity")
    if config["funderGenesisBalanceEth"] < (config["normalSenders"] * config["normalFundEth"] +
                                             config["attackSenders"] * config["attackFundEth"] + 1):
        raise RuntimeError("Insufficient genesis sponsor reserve")
    fee_parameters(config)


def trace_senders(config, seed):
    normal = scale.derive_accounts("n", config["normalSenders"], seed + ":erigon")
    attack = scale.derive_accounts("a", config["attackSenders"], seed + ":erigon")
    return [row["address"] for row in normal + attack]


def network_config(config, funder_address, seed):
    if not funder_address:
        raise RuntimeError("Run init-funder before plan/prepare/evaluation")
    prefunded = json.dumps({funder_address: {"balance": f"{config['funderGenesisBalanceEth']}ETH"}},
                           separators=(",", ":"))
    params = ["--rpc.txfeecap=0",
              "--txpool.trace.senders=" + ",".join(trace_senders(config, seed))]
    return dict(participants=[dict(el_type="erigon", el_image=config["erigonImage"],
                                   cl_type="lighthouse", cl_image=config["lighthouseImage"], count=1,
                                   el_extra_params=params)],
                network_params=dict(network_id=str(config["chainId"]), seconds_per_slot=config["secondsPerSlot"],
                                    prefunded_accounts=prefunded),
                additional_services=[])


def prepare(args, config, funder_address):
    for tool in ("docker", "git", "kurtosis"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing command: {tool}")
    try:
        import web3
        import solcx
    except ImportError as exc:
        raise RuntimeError("Install web3 and py-solc-x in the selected Python environment") from exc
    if "0.8.20" not in {str(v) for v in solcx.get_installed_solc_versions()}:
        raise RuntimeError("Install solc 0.8.20 with py-solc-x before prepare")
    for contract in ("Alice7702Drain.sol", "Alice7702NoDrain.sol"):
        solcx.compile_files([str(LEGACY / contract)], solc_version="0.8.20", evm_version="london")
    if not args.key_csv.is_file():
        raise RuntimeError(f"Missing local test key CSV: {args.key_csv}")
    if not (args.geth_repo / "go.mod").is_file():
        raise RuntimeError(f"Missing geth Go module: {args.geth_repo}")
    image = json.loads(output(["docker", "image", "inspect", config["erigonImage"]]))[0]
    network = network_config(config, funder_address, args.seed)
    check = subprocess.run(["docker", "run", "--rm", "--network", "none", config["erigonImage"],
                            *network["participants"][0]["el_extra_params"], "--version"],
                           capture_output=True, text=True, timeout=90)
    version_text = check.stdout + "\n" + check.stderr
    if check.returncode or "3.5.0" not in version_text:
        raise RuntimeError("Erigon 3.5.0 image/CLI preflight failed:\n" + version_text[-2000:])
    BUILD.mkdir(parents=True, exist_ok=True)
    helper = BUILD / "eip7702_scale_helper"
    subprocess.run([str(args.go), "build", "-o", str(helper), str(LEGACY / "eip7702_scale_helper.go")],
                   cwd=args.geth_repo, check=True)
    package = BUILD / "ethereum-package"
    if not (package / ".git").exists():
        subprocess.run(["git", "clone", "--no-checkout", "--filter=blob:none",
                        "https://github.com/ethpandaops/ethereum-package.git", str(package)], check=True)
    commit = config["ethereumPackageCommit"]
    subprocess.run(["git", "-C", str(package), "fetch", "--depth=1", "origin", commit], check=True)
    subprocess.run(["git", "-C", str(package), "checkout", "--detach", commit], check=True)
    manifest = dict(configSha256=sha(args.config), helperSourceSha256=sha(LEGACY / "eip7702_scale_helper.go"),
                    helperSha256=sha(helper), goVersion=output([str(args.go), "version"]),
                    imageTag=config["erigonImage"], imageId=image["Id"], imageRepoDigests=image["RepoDigests"],
                    imageCliPreflight=version_text.strip(), packageCommit=output(["git", "-C", str(package), "rev-parse", "HEAD"]),
                    gitCommit=output(["git", "-C", str(HERE.parents[1]), "rev-parse", "HEAD"]),
                    pythonVersion=sys.version, seed=args.seed, networkSha256=hashlib.sha256(
                        json.dumps(network, sort_keys=True).encode()).hexdigest())
    save(BUILD / "runtime_manifest.json", manifest)
    save(BUILD / "network.json", network)
    return helper, package, manifest


def container(enclave):
    ids = output(["docker", "ps", "-aq", "--filter", f"label=com.kurtosistech.enclave-name={enclave}",
                  "--filter", f"label=com.kurtosistech.id={SERVICE}"]).splitlines()
    if len(ids) != 1:
        raise RuntimeError(f"Expected one Erigon container in {enclave}; got {len(ids)}")
    return ids[0]


def rpc_url(enclave):
    port = output(["kurtosis", "port", "print", "--format", "number", enclave, SERVICE, RPC_PORT])
    return f"http://127.0.0.1:{port}"


def run_groups(args, config, funder_address, helper, package, manifest):
    os.umask(0o077)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (args.out or HERE / "runs" / f"{config['name']}_{args.seed}_{stamp}").resolve()
    root.mkdir(parents=True, exist_ok=False)
    save(root / "config.json", config)
    save(root / "runtime_manifest.json", manifest)
    save(root / "network.json", network_config(config, funder_address, args.seed))
    selected = list(GROUPS) if args.group == "all" else [args.group]
    print(f"Experiment directory: {root}", flush=True)
    for group in selected:
        mode, delegate = GROUPS[group]
        folder = root / group
        folder.mkdir()
        enclave = f"e35-p1-{stamp.replace('_', '-')}-{uuid.uuid4().hex[:6]}-{group.lower()}"
        save(folder / "network_identity.json", dict(enclave=enclave))
        print(f"Starting {group}: {enclave}", flush=True)
        with (folder / "network.log").open("w") as handle:
            launched = subprocess.run(["kurtosis", "run", "--enclave", enclave,
                                       "--args-file", str(root / "network.json"), str(package)],
                                      stdout=handle, stderr=subprocess.STDOUT)
        if launched.returncode:
            raise RuntimeError(f"Network startup failed; preserve {folder / 'network.log'} and {enclave}")
        rpc = rpc_url(enclave)
        cid = container(enclave)
        details = json.loads(output(["docker", "inspect", cid]))[0]
        save(folder / "network_identity.json", dict(enclave=enclave, rpc=rpc, container=cid,
             imageId=details["Image"], command=details["Config"]["Cmd"],
             entrypoint=details["Config"]["Entrypoint"]))
        command = [sys.executable, "-u", str(BESU / "workload.py"), "--client", "erigon",
                   "--container", cid, "--mode", mode, "--delegate-case", delegate,
                   "--config", str(root / "config.json"), "--rpc", rpc, "--out", str(folder),
                   "--helper", str(helper), "--key-csv", str(args.key_csv), "--seed", args.seed,
                   "--group", group, "--funder-file", str(args.funder_file)]
        with (folder / "run.log").open("w") as log:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    log.flush()
                code = process.wait()
        with (folder / "erigon.log").open("w") as handle:
            subprocess.run(["docker", "logs", "--timestamps", cid], stdout=handle,
                           stderr=subprocess.STDOUT, check=True)
        if code:
            raise RuntimeError(f"Workload failed; evidence and enclave preserved at {folder}")
        summarize_group(folder)
        summarize_evaluation(root)
        subprocess.run(["kurtosis", "enclave", "stop", enclave], check=True)
    print(f"Phase 1 Erigon evaluation complete: {root}", flush=True)
    return root


def audit_existing(root):
    root = Path(root)
    for group in GROUPS:
        if (root / group / "receipts.json").exists():
            summarize_group(root / group)
    return summarize_evaluation(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init-funder", "plan", "prepare", "evaluation", "audit"))
    parser.add_argument("--config", type=Path, default=HERE / "config_fixed.json")
    parser.add_argument("--group", choices=(*GROUPS, "all"), default="all")
    parser.add_argument("--seed", default="seed01")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--geth-repo", type=Path, default=Path.home() / "ETH/pos-exp/go-ethereum-pos")
    parser.add_argument("--go", type=Path, default=Path("/usr/local/go/bin/go"))
    parser.add_argument("--key-csv", type=Path)
    args = parser.parse_args()
    args.config = args.config.resolve()
    config = json.loads(args.config.read_text())
    validate(config)
    args.funder_file = BUILD / "phase1_funder.json"
    if args.action == "init-funder":
        print("Local Erigon Phase 1 funder address:", create_funder(args.funder_file))
        return
    if args.action == "audit":
        if not args.out:
            parser.error("audit requires --out with an existing run directory")
        print(json.dumps(audit_existing(args.out), indent=2))
        return
    if not args.funder_file.is_file():
        raise RuntimeError("Run Erigon init-funder before plan, prepare or evaluation")
    funder_address = load_funder(args.funder_file)["address"]
    if args.action == "plan":
        print(json.dumps(dict(config=config, network=network_config(config, funder_address, args.seed)), indent=2))
        return
    args.key_csv = args.key_csv or args.geth_repo / "devtools/mpfuzz_style_exp5_pos/key_prive_exp5.csv"
    helper, package, manifest = prepare(args, config, funder_address)
    if args.action == "prepare":
        print("Erigon image, contracts, helper, key CSV and package prepared; no network started.")
    else:
        run_groups(args, config, funder_address, helper, package, manifest)


if __name__ == "__main__":
    main()
