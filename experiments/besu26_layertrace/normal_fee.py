"""Deterministic per-transaction legacy gas prices for the Phase 1 normal cohort."""
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path


GWEI = Decimal(10**9)


def parameters(config):
    center = config["priceUnitWei"] * config["normalPriceMultiplier"]
    if type(center) is not int or center <= 0:
        raise RuntimeError("Normal gas price must be positive integer Wei")
    mode = config.get("normalGasPriceMode", "fixed")
    if mode == "fixed":
        return dict(mode=mode, centerWei=center, minimumWei=center, maximumWei=center,
                    spreadBps=0, randomSeed=None)
    if mode != "randomized-uniform":
        raise RuntimeError(f"Unsupported normalGasPriceMode: {mode}")
    spread = config.get("normalFeeSpreadBps")
    seed = config.get("normalFeeRandomSeed")
    if type(spread) is not int or not 0 < spread < 10_000:
        raise RuntimeError("normalFeeSpreadBps must be an integer between 1 and 9999")
    if not isinstance(seed, str) or not seed:
        raise RuntimeError("normalFeeRandomSeed must be a nonempty string")
    minimum = (center * (10_000 - spread) + 9_999) // 10_000
    maximum = center * (10_000 + spread) // 10_000
    if minimum <= 0 or minimum > maximum:
        raise RuntimeError("Invalid normal fee range")
    return dict(mode=mode, centerWei=center, minimumWei=minimum, maximumWei=maximum,
                spreadBps=spread, randomSeed=seed)


def _draw(seed, index, minimum, maximum):
    """Sample integer Wei uniformly, with SHA-256 rejection to avoid modulo bias."""
    span = maximum - minimum + 1
    ceiling = (1 << 256) - ((1 << 256) % span)
    retry = 0
    while True:
        payload = f"besu26-normal-fee-v1:{seed}:{index}:{retry}".encode("utf-8")
        sample = int.from_bytes(hashlib.sha256(payload).digest(), "big")
        if sample < ceiling:
            return minimum + sample % span
        retry += 1


def prices(config, count):
    if type(count) is not int or count < 0:
        raise RuntimeError("Normal fee count must be nonnegative")
    spec = parameters(config)
    if spec["mode"] == "fixed":
        return [spec["centerWei"]] * count
    return [_draw(spec["randomSeed"], index, spec["minimumWei"], spec["maximumWei"])
            for index in range(count)]


def sequence_sha256(values):
    return hashlib.sha256("".join(f"{value}\n" for value in values).encode("ascii")).hexdigest()


def plan(config, accounts):
    """Return the exact sequence consumed by the Go helper's normal send loop."""
    count = config["txsPerSender"]
    if type(count) is not int or count <= 0:
        raise RuntimeError("txsPerSender must be positive")
    order = config["normalSendOrder"]
    if order == "account":
        positions = ((account, offset) for account in accounts for offset in range(count))
    elif order == "round-robin":
        positions = ((account, offset) for offset in range(count) for account in accounts)
    else:
        raise RuntimeError("normalSendOrder must be account or round-robin")
    values = prices(config, len(accounts) * count)
    return [dict(index=index, label=f"{account['label']}-n{offset + 1}" if count > 1 else account["label"],
                 sender=account["address"], nonce=offset, gasPriceWei=value)
            for index, ((account, offset), value) in enumerate(zip(positions, values))]


def write_plan(folder, config, rows):
    folder = Path(folder)
    values = [row["gasPriceWei"] for row in rows]
    with (folder / "normal_fee_plan.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("index", "label", "sender", "nonce", "gasPriceWei"))
        writer.writeheader()
        writer.writerows(rows)
    spec = parameters(config)
    metadata = dict(**spec, count=len(rows), sequenceSha256=sequence_sha256(values),
                    signedTxType="legacy-0", gasLimit=config["normalGas"])
    (folder / "normal_fee_plan.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if spec["mode"] == "randomized-uniform":
        price_file = folder / "normal_fee_prices.txt"
        price_file.write_text("".join(f"{value}\n" for value in values), encoding="ascii")
        return price_file
    return None


def verify_records(records, rows):
    if len(records) != len(rows):
        raise RuntimeError("Normal fee plan and broadcast record lengths differ")
    for index, (record, expected) in enumerate(zip(records, rows)):
        for key in ("label", "sender", "nonce", "gasPriceWei"):
            actual = str(record.get(key, "")).lower()
            wanted = str(expected[key]).lower()
            if actual != wanted:
                raise RuntimeError(f"Normal fee plan differs from broadcast record {index} at {key}")


def gwei(value):
    return format(Decimal(value) / GWEI, "f")


def statistics(values):
    if not values:
        raise RuntimeError("Cannot summarize an empty normal fee sequence")
    ordered = sorted(values)
    count = len(ordered)
    return dict(count=count, meanGwei=gwei(Decimal(sum(ordered)) / count),
                medianGwei=gwei((Decimal(ordered[(count - 1) // 2]) + Decimal(ordered[count // 2])) / 2),
                minGwei=gwei(ordered[0]), maxGwei=gwei(ordered[-1]),
                p10Gwei=gwei(ordered[(10 * count + 99) // 100 - 1]),
                p90Gwei=gwei(ordered[(90 * count + 99) // 100 - 1]),
                percentileMethod="nearest-rank", priceMeaning="signed legacy gasPrice, not paid fee")


def bins(config):
    spec = parameters(config)
    if spec["mode"] == "fixed":
        edges = [spec["centerWei"], spec["centerWei"] + 1]
    else:
        span = spec["maximumWei"] - spec["minimumWei"] + 1
        edges = [spec["minimumWei"] + span * i // 4 for i in range(4)] + [spec["maximumWei"] + 1]
    return [dict(bin=f"B{index + 1}", lowerWei=edges[index], upperExclusiveWei=edges[index + 1])
            for index in range(len(edges) - 1)]
