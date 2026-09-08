#!/usr/bin/env python3
"""Build realistic-fee calibration profiles for phased Deter-Z2 runs.

The script does not send transactions. It reads mainnet fee_history output,
estimates normal-transaction costs under a few candidate fee profiles, and
writes reproducible commands for the phased runner.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shlex
import statistics
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


GWEI = 10**9
ETHER = 10**18
DEFAULT_ATTACK_TARGET_ETH = 16.836499999983164


@dataclass(frozen=True)
class Workload:
    normal_senders: int
    normal_rate_per_block: int
    warmup_blocks: int
    saturation_blocks: int
    attack_blocks: int
    recovery_blocks: int
    drain_blocks: int
    normal_gas_limit: int
    normal_gas_used: int
    normal_calldata_bytes: int

    @property
    def active_blocks(self) -> int:
        return self.warmup_blocks + self.saturation_blocks + self.attack_blocks + self.recovery_blocks

    @property
    def normal_total(self) -> int:
        return self.active_blocks * self.normal_rate_per_block

    @property
    def normal_txs_per_sender_upper_bound(self) -> int:
        return math.ceil(self.normal_total / self.normal_senders)


@dataclass(frozen=True)
class FeeProfile:
    name: str
    field: str
    multiplier: float
    jitter: float
    floor_gwei: float | None
    cap_gwei: float | None
    sampling_strategy: str
    description: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare realistic-fee calibration profiles and phased-run commands.")
    parser.add_argument("--fee-dir", default=None, help="Directory containing fee_history.csv and fee_summary.json.")
    parser.add_argument("--fee-history", default=None, help="Path to fee_history.csv. Overrides --fee-dir.")
    parser.add_argument("--fee-summary", default=None, help="Path to fee_summary.json. Overrides --fee-dir.")
    parser.add_argument("--out-dir", default=None, help="Output directory for calibration summary and commands.")

    parser.add_argument("--normal-field", default="effectiveP50Gwei", help="fee_history.csv column for normal tx gas-price sampling.")
    parser.add_argument("--target-attack-cost-eth", type=float, default=DEFAULT_ATTACK_TARGET_ETH)
    parser.add_argument("--seed", default="realistic-fee-calibration")

    parser.add_argument("--normal-senders", type=int, default=100)
    parser.add_argument("--normal-rate-per-block", type=int, default=800)
    parser.add_argument("--warmup-blocks", type=int, default=2)
    parser.add_argument("--saturation-blocks", type=int, default=4)
    parser.add_argument("--attack-blocks", type=int, default=4)
    parser.add_argument("--recovery-blocks", type=int, default=4)
    parser.add_argument("--drain-blocks", type=int, default=10)
    parser.add_argument("--normal-gas-limit", type=int, default=150_000)
    parser.add_argument("--normal-gas-used", type=int, default=102_920)
    parser.add_argument("--normal-calldata-bytes", type=int, default=8192)

    parser.add_argument("--realistic-jitter", type=float, default=0.35)
    parser.add_argument("--realistic-floor-mode", choices=["none", "min", "p10"], default="p10")
    parser.add_argument("--realistic-cap-mode", choices=["none", "p95", "p99"], default="p99")
    parser.add_argument("--realistic-sampling-strategy", choices=["per-tx", "per-sender", "sender-monotonic"], default="per-tx")

    parser.add_argument("--aligned-jitter", type=float, default=0.25)
    parser.add_argument("--aligned-floor-gwei", type=float, default=2.0)
    parser.add_argument("--aligned-cap-gwei", type=float, default=20.0)
    parser.add_argument("--aligned-sampling-strategy", choices=["per-tx", "per-sender", "sender-monotonic"], default="sender-monotonic")

    parser.add_argument("--rpc-var", default="RPC", help="Shell variable name used for the local devnet RPC in emitted commands.")
    parser.add_argument("--enclave", default="besu-lighthouse-7702-phased-formal")
    parser.add_argument("--client", default="besu", choices=["besu", "erigon"])
    parser.add_argument("--trial-prefix", default="realistic_fee_calibration")
    parser.add_argument("--normal-fund-eth", type=float, default=5.0)

    parser.add_argument("--attack-senders", type=int, default=125)
    parser.add_argument("--attack-rate-per-block", type=int, default=4000)
    parser.add_argument("--attack-sender-activation", choices=["all", "new-per-block", "cumulative"], default="new-per-block")
    parser.add_argument("--attack-gas", type=int, default=500_000)
    parser.add_argument("--attack-calldata-padding-bytes", type=int, default=8192)
    parser.add_argument("--attack-first-calldata-padding-bytes", type=int, default=0)
    return parser.parse_args()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "count": len(values),
        "min": round(min(values), 9),
        "max": round(max(values), 9),
        "avg": round(sum(values) / len(values), 9),
        "median": round(statistics.median(values), 9),
        "p10": round(percentile(values, 10), 9),
        "p25": round(percentile(values, 25), 9),
        "p50": round(percentile(values, 50), 9),
        "p75": round(percentile(values, 75), 9),
        "p90": round(percentile(values, 90), 9),
        "p95": round(percentile(values, 95), 9),
        "p99": round(percentile(values, 99), 9),
    }


def latest_fee_dir(exp_dir: Path) -> Path:
    fee_data = exp_dir / "fee_data"
    candidates = [path for path in fee_data.glob("mainnet_latest_*") if path.is_dir()]
    if not candidates:
        raise SystemExit(f"no mainnet_latest_* fee directories found under {fee_data}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def resolve_fee_paths(args: argparse.Namespace, exp_dir: Path) -> tuple[Path, Path, Path]:
    fee_dir = Path(args.fee_dir) if args.fee_dir else latest_fee_dir(exp_dir)
    history = Path(args.fee_history) if args.fee_history else fee_dir / "fee_history.csv"
    summary = Path(args.fee_summary) if args.fee_summary else fee_dir / "fee_summary.json"
    if not history.exists():
        raise SystemExit(f"fee history not found: {history}")
    if not summary.exists():
        raise SystemExit(f"fee summary not found: {summary}")
    return fee_dir, history, summary


def read_fee_values_gwei(history_path: Path, field: str) -> list[float]:
    values: list[float] = []
    with history_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if field not in (reader.fieldnames or []):
            raise SystemExit(f"field {field!r} not found in {history_path}")
        for row in reader:
            raw = (row.get(field) or "").strip()
            if not raw:
                continue
            value = float(raw)
            if value > 0:
                values.append(value)
    if not values:
        raise SystemExit(f"no positive values found in {history_path} column {field}")
    return values


def profile_floor(values: list[float], mode: str) -> float | None:
    if mode == "none":
        return None
    if mode == "min":
        return min(values)
    if mode == "p10":
        return percentile(values, 10)
    raise RuntimeError(f"unknown floor mode: {mode}")


def profile_cap(values: list[float], mode: str) -> float | None:
    if mode == "none":
        return None
    if mode == "p95":
        return percentile(values, 95)
    if mode == "p99":
        return percentile(values, 99)
    raise RuntimeError(f"unknown cap mode: {mode}")


def apply_fee_profile(raw_gwei: float, rng: random.Random, profile: FeeProfile) -> float:
    value = raw_gwei * profile.multiplier
    if profile.jitter:
        value *= 1 + rng.uniform(-profile.jitter, profile.jitter)
    if profile.floor_gwei is not None:
        value = max(profile.floor_gwei, value)
    if profile.cap_gwei is not None:
        value = min(profile.cap_gwei, value)
    return max(value, 0.0)


def sender_order(workload: Workload) -> list[int]:
    order: list[int] = []
    remaining = workload.normal_total
    while remaining > 0:
        batch_count = min(workload.normal_rate_per_block, remaining)
        for offset in range(batch_count):
            order.append(offset % workload.normal_senders)
        remaining -= batch_count
    return order


def simulate_profile(values: list[float], profile: FeeProfile, workload: Workload, seed: str) -> dict[str, Any]:
    rng = random.Random(f"{seed}:{profile.name}")
    generated: list[float] = []
    sender_price: dict[int, float] = {}
    sender_last_price: dict[int, float] = {}
    order = sender_order(workload)

    for sender in order:
        if profile.sampling_strategy == "per-sender":
            if sender not in sender_price:
                sender_price[sender] = apply_fee_profile(rng.choice(values), rng, profile)
            generated.append(sender_price[sender])
            continue
        if profile.sampling_strategy == "sender-monotonic":
            candidate = apply_fee_profile(rng.choice(values), rng, profile)
            previous = sender_last_price.get(sender)
            if previous is not None:
                candidate = max(previous, candidate)
            sender_last_price[sender] = candidate
            generated.append(candidate)
            continue
        generated.append(apply_fee_profile(rng.choice(values), rng, profile))

    total_eth = sum(generated) * GWEI * workload.normal_gas_used / ETHER
    floor_hits = 0
    cap_hits = 0
    if profile.floor_gwei is not None:
        floor_hits = sum(1 for value in generated if abs(value - profile.floor_gwei) < 1e-12)
    if profile.cap_gwei is not None:
        cap_hits = sum(1 for value in generated if abs(value - profile.cap_gwei) < 1e-12)

    generated_stats = stats(generated)
    generated_stats["floorHits"] = floor_hits
    generated_stats["capHits"] = cap_hits
    generated_stats["floorHitRate"] = round(floor_hits / len(generated), 6) if generated else 0.0
    generated_stats["capHitRate"] = round(cap_hits / len(generated), 6) if generated else 0.0
    return {
        "profile": profile.__dict__,
        "normalSubmitted": workload.normal_total,
        "normalGasUsedReference": workload.normal_gas_used,
        "estimatedNormalCostEth": round(total_eth, 12),
        "estimatedAverageGasPriceGwei": round(sum(generated) / len(generated), 9) if generated else 0.0,
        "generatedGasPriceGwei": generated_stats,
    }


def with_multiplier(profile: FeeProfile, multiplier: float) -> FeeProfile:
    return FeeProfile(
        name=profile.name,
        field=profile.field,
        multiplier=multiplier,
        jitter=profile.jitter,
        floor_gwei=profile.floor_gwei,
        cap_gwei=profile.cap_gwei,
        sampling_strategy=profile.sampling_strategy,
        description=profile.description,
    )


def solve_multiplier(values: list[float], base_profile: FeeProfile, workload: Workload, seed: str, target_eth: float) -> float:
    low = 0.01
    high = 1.0
    while simulate_profile(values, with_multiplier(base_profile, high), workload, seed)["estimatedNormalCostEth"] < target_eth:
        high *= 2
        if high > 10_000:
            raise RuntimeError("could not bracket target multiplier")

    for _ in range(40):
        mid = (low + high) / 2
        estimate = simulate_profile(values, with_multiplier(base_profile, mid), workload, seed)["estimatedNormalCostEth"]
        if estimate < target_eth:
            low = mid
        else:
            high = mid
    return high


def format_float(value: float | None, digits: int = 6) -> str:
    if value is None:
        return ""
    rendered = f"{value:.{digits}f}"
    return rendered.rstrip("0").rstrip(".")


def command_lines(
    repo_root: Path,
    script_rel: str,
    mode: str,
    trial_id: str,
    args: argparse.Namespace,
    workload: Workload,
    profile: FeeProfile,
    history_path: Path,
    normal_fund_eth: float,
) -> list[str]:
    cmd = [
        "PYTHONUNBUFFERED=1",
        ".venv-exp5/bin/python",
        script_rel,
        "--mode",
        mode,
        "--trial-id",
        trial_id,
        "--rpc",
        f'"${args.rpc_var}"',
        "--enclave",
        args.enclave,
        "--client",
        args.client,
        "--normal-senders",
        str(workload.normal_senders),
        "--normal-rate-per-block",
        str(workload.normal_rate_per_block),
        "--warmup-blocks",
        str(workload.warmup_blocks),
        "--saturation-blocks",
        str(workload.saturation_blocks),
        "--attack-blocks",
        str(workload.attack_blocks),
        "--recovery-blocks",
        str(workload.recovery_blocks),
        "--drain-blocks",
        str(workload.drain_blocks),
        "--normal-calldata-bytes",
        str(workload.normal_calldata_bytes),
        "--normal-gas",
        str(workload.normal_gas_limit),
        "--normal-fund-eth",
        format_float(normal_fund_eth, 3),
        "--normal-gas-price-mode",
        "sampled",
        "--normal-fee-history",
        str(history_path),
        "--normal-fee-field",
        profile.field,
        "--normal-fee-multiplier",
        format_float(profile.multiplier, 6),
        "--normal-fee-jitter",
        format_float(profile.jitter, 3),
        "--normal-fee-sampling-strategy",
        profile.sampling_strategy,
        "--receipt-scope",
        "none",
    ]
    if profile.floor_gwei is not None:
        cmd.extend(["--normal-fee-floor-gwei", format_float(profile.floor_gwei, 9)])
    if profile.cap_gwei is not None:
        cmd.extend(["--normal-fee-cap-gwei", format_float(profile.cap_gwei, 9)])
    if mode == "attack":
        cmd.extend(
            [
                "--attack-senders",
                str(args.attack_senders),
                "--attack-rate-per-block",
                str(args.attack_rate_per_block),
                "--attack-sender-activation",
                args.attack_sender_activation,
                "--attack-gas",
                str(args.attack_gas),
                "--attack-calldata-padding-bytes",
                str(args.attack_calldata_padding_bytes),
                "--attack-first-calldata-padding-bytes",
                str(args.attack_first_calldata_padding_bytes),
            ]
        )

    rendered: list[str] = [f"cd {shlex.quote(str(repo_root))}", f'export {args.rpc_var}="http://127.0.0.1:PORT"']
    rendered.append("\\\n  ".join(cmd))
    return rendered


def write_shell_script(path: Path, lines: list[str]) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n\n" + "\n\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def write_markdown(
    path: Path,
    fee_dir: Path,
    history_path: Path,
    summary_path: Path,
    workload: Workload,
    source_stats: dict[str, float],
    profile_summaries: list[dict[str, Any]],
    commands: dict[str, Path],
) -> None:
    rows = []
    for item in profile_summaries:
        profile = item["profile"]
        gas_stats = item["generatedGasPriceGwei"]
        rows.append(
            "| {name} | {strategy} | {multiplier} | {jitter} | {floor} | {cap} | {avg} | {p50} | {p90} | {cost} ETH | {cap_rate}% |".format(
                name=profile["name"],
                strategy=profile["sampling_strategy"],
                multiplier=format_float(profile["multiplier"], 4),
                jitter=format_float(profile["jitter"], 3),
                floor=format_float(profile["floor_gwei"], 4) if profile["floor_gwei"] is not None else "none",
                cap=format_float(profile["cap_gwei"], 4) if profile["cap_gwei"] is not None else "none",
                avg=format_float(gas_stats["avg"], 4),
                p50=format_float(gas_stats["p50"], 4),
                p90=format_float(gas_stats["p90"], 4),
                cost=format_float(item["estimatedNormalCostEth"], 6),
                cap_rate=format_float(100 * gas_stats["capHitRate"], 2),
            )
        )

    content = [
        "# Realistic-Fee Calibration",
        "",
        f"Fee data: `{fee_dir}`",
        f"History: `{history_path}`",
        f"Summary: `{summary_path}`",
        "",
        "## Mainnet Source Field",
        "",
        "| field | count | avg gwei | median gwei | p75 gwei | p90 gwei | p99 gwei |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        "| effectiveP50Gwei | {count} | {avg} | {median} | {p75} | {p90} | {p99} |".format(
            count=int(source_stats["count"]),
            avg=format_float(source_stats["avg"], 6),
            median=format_float(source_stats["median"], 6),
            p75=format_float(source_stats["p75"], 6),
            p90=format_float(source_stats["p90"], 6),
            p99=format_float(source_stats["p99"], 6),
        ),
        "",
        "## Workload Reference",
        "",
        "| normal submitted | reference gasUsed | active blocks | rate/block | senders |",
        "| ---: | ---: | ---: | ---: | ---: |",
        f"| {workload.normal_total} | {workload.normal_gas_used} | {workload.active_blocks} | {workload.normal_rate_per_block} | {workload.normal_senders} |",
        "",
        "## Candidate Profiles",
        "",
        "| profile | sampling | multiplier | jitter | floor gwei | cap gwei | avg gwei | p50 gwei | p90 gwei | estimated normal cost | cap hit rate |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *rows,
        "",
        "## Commands",
        "",
    ]
    for label, command_path in commands.items():
        content.extend([f"- `{label}`: `{command_path}`"])
    content.append("")
    path.write_text("\n".join(content), encoding="utf-8")


def main() -> int:
    args = parse_args()
    exp_dir = Path(__file__).resolve().parent
    repo_root = exp_dir.parents[1]
    fee_dir, history_path, summary_path = resolve_fee_paths(args, exp_dir)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    workload = Workload(
        normal_senders=args.normal_senders,
        normal_rate_per_block=args.normal_rate_per_block,
        warmup_blocks=args.warmup_blocks,
        saturation_blocks=args.saturation_blocks,
        attack_blocks=args.attack_blocks,
        recovery_blocks=args.recovery_blocks,
        drain_blocks=args.drain_blocks,
        normal_gas_limit=args.normal_gas_limit,
        normal_gas_used=args.normal_gas_used,
        normal_calldata_bytes=args.normal_calldata_bytes,
    )
    values = read_fee_values_gwei(history_path, args.normal_field)
    source_stats = stats(values)
    floor_gwei = profile_floor(values, args.realistic_floor_mode)
    cap_gwei = profile_cap(values, args.realistic_cap_mode)

    realistic_profile = FeeProfile(
        name="realistic-mainnet",
        field=args.normal_field,
        multiplier=1.0,
        jitter=args.realistic_jitter,
        floor_gwei=floor_gwei,
        cap_gwei=cap_gwei,
        sampling_strategy=args.realistic_sampling_strategy,
        description="Mainnet-derived normal gas price distribution without stress multiplier.",
    )
    aligned_base = FeeProfile(
        name="fee-aligned-stress",
        field=args.normal_field,
        multiplier=1.0,
        jitter=args.aligned_jitter,
        floor_gwei=args.aligned_floor_gwei,
        cap_gwei=args.aligned_cap_gwei,
        sampling_strategy=args.aligned_sampling_strategy,
        description="Mainnet-derived distribution scaled so normal total fee is close to the first-0 attack cost.",
    )
    aligned_multiplier = solve_multiplier(values, aligned_base, workload, args.seed, args.target_attack_cost_eth)
    aligned_profile = with_multiplier(aligned_base, aligned_multiplier)

    profile_summaries = [
        simulate_profile(values, realistic_profile, workload, args.seed),
        simulate_profile(values, aligned_profile, workload, args.seed),
    ]

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else exp_dir / "scale_runs" / f"realistic_fee_calibration_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    commands = {
        "realistic-baseline": out_dir / "run_realistic_baseline.sh",
        "fee-aligned-baseline": out_dir / "run_fee_aligned_baseline.sh",
        "fee-aligned-first0-attack": out_dir / "run_fee_aligned_first0_attack.sh",
    }
    script_rel = "devtools/mpfuzz_style_exp5_pos/mp_exp5_7702_phased_pos.py"
    write_shell_script(
        commands["realistic-baseline"],
        command_lines(
            repo_root,
            script_rel,
            "baseline",
            f"{args.trial_prefix}_realistic_baseline_trial01",
            args,
            workload,
            realistic_profile,
            history_path,
            args.normal_fund_eth,
        ),
    )
    write_shell_script(
        commands["fee-aligned-baseline"],
        command_lines(
            repo_root,
            script_rel,
            "baseline",
            f"{args.trial_prefix}_fee_aligned_baseline_trial01",
            args,
            workload,
            aligned_profile,
            history_path,
            args.normal_fund_eth,
        ),
    )
    write_shell_script(
        commands["fee-aligned-first0-attack"],
        command_lines(
            repo_root,
            script_rel,
            "attack",
            f"{args.trial_prefix}_fee_aligned_first0_attack_trial01",
            args,
            workload,
            aligned_profile,
            history_path,
            args.normal_fund_eth,
        ),
    )

    output = {
        "feeDir": str(fee_dir),
        "feeHistory": str(history_path),
        "feeSummary": str(summary_path),
        "sourceDataKind": summary.get("dataKind", "block-level gas price percentile history"),
        "sourceField": args.normal_field,
        "sourceStatsGwei": source_stats,
        "targetAttackCostEth": args.target_attack_cost_eth,
        "workload": workload.__dict__,
        "profiles": profile_summaries,
        "commands": {key: str(value) for key, value in commands.items()},
        "notes": [
            "realistic-mainnet is for calibration and reporting of true mainnet-derived normal fee levels.",
            "fee-aligned-stress is not a claim about normal mainnet fees; it scales the same distribution to compare total cost against the first-0 attack run.",
            "The emitted commands use a placeholder local RPC port. Replace http://127.0.0.1:PORT with the current Kurtosis EL RPC.",
        ],
    }
    summary_out = out_dir / "realistic_fee_calibration_summary.json"
    markdown_out = out_dir / "realistic_fee_calibration.md"
    summary_out.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    write_markdown(markdown_out, fee_dir, history_path, summary_path, workload, source_stats, profile_summaries, commands)

    print(f"outDir={out_dir}")
    print(f"summary={summary_out}")
    print(f"report={markdown_out}")
    for key, path in commands.items():
        print(f"{key}={path}")
    for profile in profile_summaries:
        item = profile["profile"]
        print(
            "profile "
            f"{item['name']}: multiplier={item['multiplier']:.6f} "
            f"avgGwei={profile['estimatedAverageGasPriceGwei']:.6f} "
            f"estimatedNormalCostEth={profile['estimatedNormalCostEth']:.6f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
