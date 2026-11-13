"""Local, ignored Phase 1 devnet sponsor. Never serialize the private key into run evidence."""
import json
import os
import stat
from pathlib import Path


def load_funder(path):
    path = Path(path)
    if not path.is_file():
        raise RuntimeError("Phase 1 funder is missing; run: python experiments/besu26_layertrace/run.py init-funder --config experiments/besu26_layertrace/config_phase1.json")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("Phase 1 funder file must be readable only by its owner (chmod 600)")
    try:
        from eth_account import Account
        record = json.loads(path.read_text(encoding="utf-8"))
        key = record["private_key"]
        address = Account.from_key(key).address
        if address.lower() != record["address"].lower():
            raise ValueError("address mismatch")
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Phase 1 funder file is invalid; do not submit any transactions") from None
    return dict(address=address, private_key=key)


def create_funder(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        return load_funder(path)["address"]
    from eth_account import Account
    account = Account.create()
    key = account.key.hex()
    if not key.startswith("0x"):
        key = "0x" + key
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(address=account.address, private_key=key), handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return account.address
