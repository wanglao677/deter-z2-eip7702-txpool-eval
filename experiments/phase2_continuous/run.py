"""Phase 2 private-devnet orchestration, preparation and offline audit."""
from __future__ import annotations

import argparse
import csv
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
ROOT = HERE.parents[1]
BESU = HERE.parent / "besu26_layertrace"
ERIGON = HERE.parent / "erigon35_phase1"
LEGACY = ROOT / "devtools" / "mpfuzz_style_exp5_pos"
sys.path.insert(0, str(BESU))
sys.path.insert(0, str(LEGACY))
import mp_exp5_7702_scale_pos as scale
from normal_fee import parameters as fee_parameters
from metrics import jsonl, summarize_slots
from plan import schedule, write_schedule

CLIENTS = {
    "besu": dict(config=HERE / "config_besu.json", phase1=BESU,
                 service="el-1-besu-lighthouse", port="rpc",
                 funder=BESU / ".build" / "phase1_funder.json"),
    "erigon": dict(config=HERE / "config_erigon.json", phase1=ERIGON,
                   service="el-1-erigon-lighthouse", port="ws-rpc",
                   funder=ERIGON / ".build" / "phase1_funder.json"),
}
GROUPS = {"G0": ("baseline", "alice-sender"),
          "G1": ("attack", "alice-sender"),
          "G2": ("attack", "alice-no-drain")}


def save(path: Path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def load_config(args):
    client = CLIENTS[args.client]
    config = json.loads((args.config or client["config"]).read_text())
    if args.client == "besu":
        if config.get("besuVersion") != "26.6.1":
            raise RuntimeError("Phase 2 pins Besu 26.6.1")
    elif config.get("erigonVersion") != "3.5.0":
        raise RuntimeError("Phase 2 pins Erigon 3.5.0")
    if config.get("evaluationGroups") != {g: dict(mode=m, delegateCase=d)
                                          for g, (m, d) in GROUPS.items()}:
        raise RuntimeError("Phase 2 G0/G1/G2 group mapping changed")
    if config["attackSenders"] != 125 or config["txsPerSender"] != 128:
        raise RuntimeError("Attack strength must remain 125 x 128")
    if config["attackFirstPaddingBytes"] != 0:
        raise RuntimeError("First attack transaction must have no padding")
    if args.fee_mode:
        config["normalGasPriceMode"] = args.fee_mode
    fee_parameters(config)
    phase = config["phase2"]
    for key in ("preSlots", "attackSlots", "postSlots", "normalArrivalRatePerSlot",
                "maxNormalGasBudgetBps"):
        if type(phase.get(key)) is not int or phase[key] <= 0:
            raise RuntimeError(f"Invalid phase2.{key}")
    if not 0 < phase["maxNormalGasBudgetBps"] < 10_000:
        raise RuntimeError("maxNormalGasBudgetBps must be below 10000")
    if config["secondsPerSlot"] != 300:
        raise RuntimeError("Phase 2 preserves the Phase 1 slot duration")
    return config


def effective_config(args, config):
    folder = HERE / ".build"
    folder.mkdir(exist_ok=True)
    path = folder / f"effective_{args.client}_{args.fee_mode or config['normalGasPriceMode']}.json"
    save(path, config)
    return path


def phase1_command(args, action, config_path):
    phase1 = CLIENTS[args.client]["phase1"]
    command = [sys.executable, str(phase1 / "run.py"), action,
               "--config", str(config_path), "--seed", args.seed]
    if action == "prepare":
        command += ["--geth-repo", str(args.geth_repo), "--go", str(args.go),
                    "--key-csv", str(args.key_csv)]
    return command


def output(command):
    return subprocess.check_output(command, text=True).strip()


def prepare(args, config_path):
    if not args.key_csv.is_file():
        raise RuntimeError(f"Missing private local test key CSV: {args.key_csv}")
    subprocess.run(phase1_command(args, "prepare", config_path), check=True)
    client = CLIENTS[args.client]
    build = client["phase1"] / ".build"
    for path in (build / "eip7702_scale_helper", build / "ethereum-package", build / "network.json"):
        if not path.exists():
            raise RuntimeError(f"Phase 1 prepare did not produce {path}")
    return build


def public_plan(args, config):
    accounts = scale.derive_accounts("n", config["normalSenders"], args.seed + ":" + args.client)
    rows = schedule(config, args.seed, accounts, args.client)
    return rows


def network_container(enclave: str, service: str):
    ids = output(["docker", "ps", "-aq",
                  "--filter", f"label=com.kurtosistech.enclave-name={enclave}",
                  "--filter", f"label=com.kurtosistech.id={service}"]).splitlines()
    if len(ids) != 1:
        raise RuntimeError(f"Expected one execution container in {enclave}; got {len(ids)}")
    return ids[0]


def run_group(args, config, root: Path, build: Path, group: str):
    details = CLIENTS[args.client]
    folder = root / group
    folder.mkdir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    enclave = f"p2-{args.client}-{stamp}-{uuid.uuid4().hex[:6]}-{group.lower()}"
    save(folder / "network_identity.json", dict(enclave=enclave))
    print(f"Starting {args.client} {group}: {enclave}", flush=True)
    with (folder / "network.log").open("w") as log:
        subprocess.run(["kurtosis", "run", "--enclave", enclave,
                        "--args-file", str(root / "network.json"),
                        str(build / "ethereum-package")],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    port = output(["kurtosis", "port", "print", "--format", "number",
                   enclave, details["service"], details["port"]])
    rpc = f"http://127.0.0.1:{port}"
    cid = network_container(enclave, details["service"])
    container_info = json.loads(output(["docker", "inspect", cid]))[0]
    manifest = json.loads((root / "runtime_manifest.json").read_text())
    if container_info["Image"] != manifest["imageId"]:
        raise RuntimeError("Running execution image differs from pinned preparation image")
    save(folder / "network_identity.json", dict(enclave=enclave, rpc=rpc, container=cid,
         imageId=container_info["Image"], command=container_info["Config"]["Cmd"],
         entrypoint=container_info["Config"]["Entrypoint"]))
    if args.client == "besu":
        trace = "/tmp/besu-layertrace/events.jsonl"
        for _ in range(30):
            if subprocess.run(["docker", "exec", cid, "test", "-s", trace],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Besu layertrace observer was not initialized")
    mode, delegate = GROUPS[group]
    command = [sys.executable, "-u", str(HERE / "workload.py"),
               "--client", args.client, "--group", group, "--mode", mode,
               "--delegate-case", delegate, "--config", str(root / "config.json"),
               "--schedule", str(root / "normal_schedule.jsonl"), "--rpc", rpc,
               "--out", str(folder), "--helper", str(build / "eip7702_scale_helper"),
               "--key-csv", str(args.key_csv), "--funder-file", str(details["funder"]),
               "--seed", args.seed]
    with (folder / "run.log").open("w") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            code = process.wait()
    with (folder / f"{args.client}.log").open("w") as log:
        subprocess.run(["docker", "logs", "--timestamps", cid], stdout=log,
                       stderr=subprocess.STDOUT, check=True)
    if args.client == "besu":
        subprocess.run(["docker", "cp", f"{cid}:/tmp/besu-layertrace/events.jsonl",
                        str(folder / "layertrace.jsonl")], check=True)
    if code:
        raise RuntimeError(f"Phase 2 workload failed; evidence and enclave preserved at {folder}")
    subprocess.run(["kurtosis", "enclave", "stop", enclave], check=True)
    return json.loads((folder / "phase2_summary.json").read_text())


def _read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def audit_group(root: Path, group: str):
    folder = root / group
    config = json.loads((root / "config.json").read_text())
    anchor = json.loads((folder / "anchor.json").read_text())
    manifest = json.loads((folder / "workload_manifest.json").read_text())
    payload = (folder / "normal_schedule.jsonl").read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != anchor["scheduleSha256"] or digest != manifest["scheduleSha256"]:
        raise RuntimeError(f"{group}: normal schedule hash mismatch")
    blocks = jsonl(folder / "blocks.jsonl")
    receipts = {r["transactionHash"].lower(): r for r in jsonl(folder / "block_receipts.jsonl")}
    submissions = jsonl(folder / "normal.jsonl") + jsonl(folder / "attack.jsonl")
    snapshots = jsonl(folder / "pool_snapshots.jsonl")
    genesis_timestamp = int(manifest["genesis"]["timestamp"], 16)
    expected = summarize_slots(config, manifest["client"], group, genesis_timestamp,
        anchor["blockNumber"], anchor["slotNumber"], anchor["unixMillis"],
        blocks, receipts, submissions, snapshots)
    actual = _read_csv(folder / "per_block_metrics.csv")
    if len(actual) != len(expected):
        raise RuntimeError(f"{group}: slot row count changed")
    for index, (recorded, computed) in enumerate(zip(actual, expected)):
        for field, value in computed.items():
            if recorded[field] != str(value):
                raise RuntimeError(f"{group}: slot {index} field {field} differs from saved evidence")
    summary = json.loads((folder / "phase2_summary.json").read_text())
    for kind in ("normal", "attack"):
        if int(summary[kind + "Included"]) != sum(int(row[kind + "Included"]) for row in actual):
            raise RuntimeError(f"{group}: summary {kind} inclusion differs")
        if int(summary[kind + "PaidFeeWei"]) != sum(int(row[kind + "PaidFeeWei"]) for row in actual):
            raise RuntimeError(f"{group}: summary {kind} fee differs")
    if int(summary["normalAttempts"]) != len(jsonl(folder / "normal_schedule.jsonl")):
        raise RuntimeError(f"{group}: normal schedule has incomplete submissions")
    if "setupFeeWei" in summary:
        setup_fee = sum(int(item["receipt"]["gasUsed"], 16) *
                        int(item["receipt"]["effectiveGasPrice"], 16)
                        for item in json.loads((folder / "setup_receipts.json").read_text()))
        if int(summary["setupFeeWei"]) != setup_fee:
            raise RuntimeError(f"{group}: setup fee differs from raw receipts")
    return actual


def comparison(root: Path):
    groups = [group for group in GROUPS if (root / group / "phase2_summary.json").exists()]
    if not groups:
        raise RuntimeError("No complete Phase 2 group")
    summaries = {g: json.loads((root / g / "phase2_summary.json").read_text()) for g in groups}
    rows = {g: audit_group(root, g) for g in groups}
    config = json.loads((root / "config.json").read_text())
    sha = hashlib.sha256((root / "normal_schedule.jsonl").read_bytes()).hexdigest()
    for group in groups:
        if json.loads((root / group / "anchor.json").read_text())["scheduleSha256"] != sha:
            raise RuntimeError(f"{group}: plan differs from root")
    recovery = dict(status="NOT_EVALUABLE_UNTIL_G0_AND_G1", relativeSlotIndex=None)
    if "G0" in rows and "G1" in rows:
        pre = config["phase2"]["preSlots"]
        attack = config["phase2"]["attackSlots"]
        marker = json.loads((root / "G1" / "phase_markers.json").read_text())
        observed_end = (int(marker["injectionEndUnixMillis"]) - int(marker["anchorUnixMillis"]) +
                        config["secondsPerSlot"] * 1000 - 1) // (config["secondsPerSlot"] * 1000)
        threshold_start = max(pre + attack, observed_end)
        streak = 0
        recovery = dict(status="NOT_OBSERVED" if threshold_start < len(rows["G0"]) else
                        "NOT_EVALUABLE_INJECTION_OVER_RAN_HORIZON", relativeSlotIndex=None,
                        criterion="G1 normal count and paid fee each >=90% of matched G0 for 3 consecutive valid post slots")
        for index in range(threshold_start, len(rows["G0"])):
            g0, g1 = rows["G0"][index], rows["G1"][index]
            valid = (g0["blockPresent"] == "True" and g1["blockPresent"] == "True" and
                     int(g0["normalIncluded"]) > 0 and float(g0["normalPaidFeeEth"]) > 0)
            restored = valid and (10 * int(g1["normalIncluded"]) >= 9 * int(g0["normalIncluded"]) and
                                  10 * int(g1["normalPaidFeeWei"]) >= 9 * int(g0["normalPaidFeeWei"]))
            streak = streak + 1 if restored else 0
            if streak >= 3:
                recovery = dict(status="OBSERVED", relativeSlotIndex=index - 2,
                                criterion=recovery["criterion"])
                break
    result = dict(configName=config["name"], normalScheduleSha256=sha,
                  groups=summaries, recovery=recovery,
                  conventionalSpamBaseline="Estimated from G0 included transaction gasPrice maximum times block gas limit; not measured expenditure")
    save(root / "phase2_evaluation_summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init-funder", "plan", "prepare", "evaluation", "audit"))
    parser.add_argument("--client", choices=tuple(CLIENTS), required=True)
    parser.add_argument("--group", default="G0,G1",
                        help="Comma-separated G0,G1,G2 or all; default G0,G1")
    parser.add_argument("--seed", default="seed01")
    parser.add_argument("--fee-mode", choices=("fixed", "randomized-uniform"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--geth-repo", type=Path, default=Path.home() / "ETH/pos-exp/go-ethereum-pos")
    parser.add_argument("--go", type=Path, default=Path("/usr/local/go/bin/go"))
    parser.add_argument("--key-csv", type=Path)
    args = parser.parse_args()
    args.key_csv = args.key_csv or args.geth_repo / "devtools/mpfuzz_style_exp5_pos/key_prive_exp5.csv"
    config = load_config(args)
    selected = list(GROUPS) if args.group == "all" else args.group.split(",")
    if not selected or len(selected) != len(set(selected)) or any(g not in GROUPS for g in selected):
        parser.error("--group must be a unique comma-separated subset of G0,G1,G2 or all")
    config_path = effective_config(args, config)
    if args.action == "init-funder":
        subprocess.run(phase1_command(args, "init-funder", config_path), check=True)
        return
    if args.action == "audit":
        if not args.out:
            parser.error("audit requires --out with an existing Phase 2 run directory")
        print(json.dumps(comparison(args.out), indent=2))
        return
    if args.action == "plan":
        rows = public_plan(args, config)
        phase = config["phase2"]
        print(json.dumps(dict(client=args.client, feeMode=config["normalGasPriceMode"],
            normalTransactions=len(rows), normalArrivalRatePerSlot=phase["normalArrivalRatePerSlot"],
            maxNormalGasPerSlot=max((sum(1 for row in rows if row["slotIndex"] == slot) *
                                   config["normalGas"] for slot in range(
                                       phase["preSlots"] + phase["attackSlots"] + phase["postSlots"])), default=0),
            attackTransactions=config["attackSenders"] * config["txsPerSender"],
            phase2=phase, scheduleSha256=hashlib.sha256("".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in rows).encode()).hexdigest()), indent=2))
        return
    build = prepare(args, config_path)
    if args.action == "prepare":
        print("Phase 2 dependencies prepared; no network started.")
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (args.out or HERE / "runs" / f"{config['name']}_{config['normalGasPriceMode']}_{args.seed}_{stamp}").resolve()
    rows = public_plan(args, config)
    schedule_payload = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows).encode()
    if root.exists():
        if not args.out or not (root / "config.json").is_file():
            raise RuntimeError("Existing --out must be a Phase 2 run directory")
        if json.loads((root / "config.json").read_text()) != config:
            raise RuntimeError("Cannot append groups with a different effective configuration")
        if hashlib.sha256((root / "normal_schedule.jsonl").read_bytes()).digest() != hashlib.sha256(schedule_payload).digest():
            raise RuntimeError("Cannot append groups with a different normal arrival plan/seed")
    else:
        root.mkdir(parents=True, exist_ok=False)
        save(root / "config.json", config)
        shutil.copyfile(build / "network.json", root / "network.json")
        shutil.copyfile(build / "runtime_manifest.json", root / "runtime_manifest.json")
        write_schedule(root, config, args.seed, args.client, rows, dict(
            plannedNormalTransactions=len(rows), plannedNormalGas=len(rows) * config["normalGas"],
            actualBlockGasLimit="VALIDATED_BY_WORKLOAD_AT_NETWORK_START"))
    print(f"Phase 2 run directory: {root}", flush=True)
    for group in selected:
        run_group(args, config, root, build, group)
        print(json.dumps(comparison(root), indent=2), flush=True)
    print(f"Phase 2 evaluation complete: {root}", flush=True)


if __name__ == "__main__":
    main()
