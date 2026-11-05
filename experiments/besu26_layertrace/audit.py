#!/usr/bin/env python3
"""Replay native layer events through the last flushed checkpoint, never RPC absence."""
import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


def replay(path, cohort=None):
    data = Path(path).read_bytes()
    events, issues, checkpoint = [], [], None
    for number, raw in enumerate(data.splitlines(), 1):
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            issues.append(f"malformed_line:{number}")
            continue
        if event.get("schema") != 1:
            issues.append(f"unsupported_schema:{number}")
        events.append(event)
        if event.get("event") in ("checkpoint", "session_end"):
            checkpoint = len(events) - 1
    if checkpoint is None:
        issues.append("no_flushed_checkpoint")
        usable = []
        watermark = {}
    else:
        usable = events[:checkpoint + 1]
        watermark = usable[-1]
    sessions, expected, written = [], 1, 0
    states, history, choices = {}, [], []
    for e in usable:
        event = e.get("event")
        session = e.get("session")
        if event == "session_start":
            if session in sessions:
                issues.append("duplicate_session_start")
            sessions.append(session)
            if len(sessions) > 1:
                issues.append("multiple_node_sessions")
                for state in states.values():
                    state.update(observedState="UNKNOWN_RESTART", layer="")
            expected, written = 1, 0
            continue
        if not sessions or session != sessions[-1]:
            issues.append("missing_or_mismatched_session_start")
        if event in ("checkpoint", "session_end"):
            if e.get("dropped", 0):
                issues.append("observer_events_lost")
            if e.get("written") != written:
                issues.append("checkpoint_count_mismatch")
            if event == "session_end" and e.get("assigned") != written:
                issues.append("unflushed_events_at_shutdown")
            continue
        if e.get("seq") != expected:
            issues.append(f"sequence_gap_or_reorder:{expected}:{e.get('seq')}")
        expected = e.get("seq", expected) + 1
        written += 1
        if event == "block_error":
            issues.append("pool_block_processing_error")
        if event == "reset":
            for state in states.values():
                if state.get("layer") == e.get("layer"):
                    state.update(observedState="UNKNOWN_RESET", layer="")
            continue
        tx_hash = e.get("hash", "").lower()
        if not tx_hash or (cohort is not None and tx_hash not in cohort):
            continue
        state = states.setdefault(tx_hash, {"hash": tx_hash, "observedState": "UNOBSERVED", "layer": "", "history": []})
        state.update(sender=e.get("sender"), nonce=e.get("nonce"), lastSeq=e.get("seq"), lastUnixMillis=e.get("unixMillis"))
        history.append(e)
        reason = e.get("reason", "").upper()
        if event == "capacity_choice":
            choices.append(e)
        elif event == "enter":
            state.update(observedState="IN_LAYER", layer=e["layer"], lastReason=reason)
            state["history"].append(e["layer"])
        elif event == "leave":
            state.update(observedState=("REMOVED_" + reason if e.get("scope") == "POOL" else "IN_TRANSIT"), layer="", lastReason=reason)
        elif event == "drop":
            # EndLayer may be reached without previously residing in Sparse.
            state.update(observedState="POOL_DROPPED", layer="", lastReason=reason)
    if cohort is not None:
        for tx_hash, record in cohort.items():
            state = states.setdefault(tx_hash, {"hash": tx_hash, "observedState": "UNOBSERVED", "layer": "", "history": []})
            state.update(kind=record.get("kind", ""), label=record.get("label", ""), submissionError=record.get("error", ""))
    issues = sorted(set(issues))
    for state in states.values():
        state["evidenceComplete"] = not issues
        state["history"] = " -> ".join(state["history"])
    return dict(
        evidenceComplete=not issues, issues=issues, sessions=sessions,
        checkpoint=watermark, eventsAfterCheckpoint=len(events) - len(usable),
        sourceSha256=hashlib.sha256(data).hexdigest(),
        counts=dict(Counter(s["observedState"] for s in states.values())),
        states=list(states.values()), history=history, capacityChoices=choices,
    )


def csv_write(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def audit(trace, out, records=()):
    cohort = {} if records else None
    for path in records:
        for line in Path(path).read_text().splitlines():
            r = json.loads(line)
            if r.get("hash"):
                cohort[r["hash"].lower()] = r
    result = replay(trace, cohort)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for key, name in (("states", "tx_layers.csv"), ("history", "layer_history.csv"), ("capacityChoices", "capacity_choices.csv")):
        csv_write(out / name, result.pop(key))
    (out / "layer_audit.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trace", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--records", type=Path, nargs="*", default=[])
    a = p.parse_args()
    result = audit(a.trace, a.out, a.records)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["evidenceComplete"] else 2)
