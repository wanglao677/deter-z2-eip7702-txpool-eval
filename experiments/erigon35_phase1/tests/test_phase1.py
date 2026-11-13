"""Synthetic fixtures only; no PoS network is started here."""
import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from run import network_config, validate, GROUPS
from report import classify, drain_cost_comparable, parse_trace, summarize_group, summarize_evaluation
BESU = HERE.parent / "besu26_layertrace"
sys.path.insert(1, str(BESU))
from normal_fee import plan, sequence_sha256
from workload import erigon_admission_error, fee_wei, receipts

H1 = "0x" + "11" * 32
H2 = "0x" + "22" * 32
H3 = "0x" + "33" * 32


def receipt(block, gas, price):
    return dict(blockNumber=hex(block), gasUsed=hex(gas), effectiveGasPrice=hex(price),
                status="0x1", blockHash="0xabc", type="0x0")


class ConfigTests(unittest.TestCase):
    def config(self):
        return json.loads((HERE / "config_fixed.json").read_text())

    def test_fixed_group_and_pool_defaults(self):
        c = self.config()
        validate(c)
        self.assertEqual(c["normalSenders"] * c["txsPerSender"], 7040)
        self.assertEqual(c["attackSenders"] * c["txsPerSender"], 16000)
        self.assertEqual(c["normalGas"], 21000)
        self.assertEqual(c["normalPaddingBytes"], 0)
        self.assertEqual(c["attackFirstPaddingBytes"], 0)
        self.assertEqual(c["attackTailPaddingBytes"], 0)
        self.assertEqual(GROUPS["G2"], ("attack", "alice-no-drain"))
        # The no-drain implementation has no transfer/call and is delegated for the whole sender.
        source = (HERE.parents[1] / "devtools/mpfuzz_style_exp5_pos/Alice7702NoDrain.sol").read_text()
        self.assertIn("function drainAll(address payable receiver)", source)
        self.assertNotIn("receiver.call", source)
        self.assertNotIn("receiver.transfer", source)
        self.assertNotIn("selfdestruct", source)
        helper = (HERE.parents[1] / "devtools/mpfuzz_style_exp5_pos/eip7702_scale_helper.go").read_text()
        self.assertIn("types.NewTransaction(nonce, to, value, gas, gasPrice, data)", helper)
        self.assertIn("drainAll(address)", helper)

    def test_network_uses_version_and_trace_without_capacity_overrides(self):
        c = self.config()
        n = network_config(c, "0x" + "12" * 20, "seed01")
        node = n["participants"][0]
        self.assertEqual(node["el_image"], "erigontech/erigon:v3.5.0")
        self.assertIn("--rpc.txfeecap=0", node["el_extra_params"])
        trace = next(x for x in node["el_extra_params"] if x.startswith("--txpool.trace.senders="))
        self.assertEqual(len(trace.split("=")[1].split(",")), 180)
        self.assertFalse(any("slots" in x or "limit" in x for x in node["el_extra_params"]))
        self.assertEqual(n["network_params"]["seconds_per_slot"], 300)

    def test_random_prices_repeat_and_match_across_groups(self):
        c = json.loads((HERE / "config_random_fee.json").read_text())
        validate(c)
        accounts = [dict(label=f"n{i:05}", address="0x" + f"{i:040x}") for i in range(55)]
        plans = [plan(c, accounts) for _ in GROUPS]
        self.assertEqual(plans[0], plans[1])
        self.assertEqual(plans[1], plans[2])
        self.assertEqual(len(plans[0]), 7040)
        self.assertGreater(len({x["gasPriceWei"] for x in plans[0]}), 1)
        center = c["priceUnitWei"] * c["normalPriceMultiplier"]
        self.assertTrue(all(center * 9 // 10 <= x["gasPriceWei"] <= center * 11 // 10
                            for x in plans[0]))
        self.assertEqual({x["nonce"] for x in plans[0][:128]}, set(range(128)))
        self.assertEqual(c["normalGas"], 21000)

    def test_only_explicit_admission_errors_are_rejections(self):
        self.assertTrue(erigon_admission_error("pending sub-pool is full"))
        self.assertFalse(erigon_admission_error("Post http://127.0.0.1:8545: connection reset by peer"))

    def test_receipt_cost_is_actual_not_gas_limit(self):
        self.assertEqual(fee_wei(receipt(4, 21000, 100)), 2100000)
        class Rpc:
            def batch(self, method, args):
                self_method = method
                assert self_method == "eth_getTransactionReceipt"
                return [receipt(4, 21000, 100) for _ in args]
        rows = receipts(Rpc(), [dict(kind="normal", label="n1", hash=H1),
                                 dict(kind="attack", label="a1", error="pending sub-pool is full")])
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1]["receipt"])


class TraceTests(unittest.TestCase):
    def test_trace_lifecycle_and_reaccept(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "erigon.log"
            log.write_text(
                f"2026-11-13T01:00:00Z [INFO] TX TRACING: Inserted txn by nonce idHash={H1[2:]} sender=1 nonce=0\n"
                f"2026-11-13T01:00:01Z [INFO] TX TRACING: added to subpool Pending, IdHash={H1[2:]}, sender=1, nonce=0\n"
                f"2026-11-13T01:00:02Z [INFO] TX TRACING: Deleted txn by nonce idHash={H1[2:]} sender=1 nonce=0 reason=\"pending sub-pool is full\"\n")
            events = parse_trace(log)[H1]
            checkpoint = dict(block=5, locations={}, errors={})
            record = dict(hash=H1, label="n1", kind="normal")
            self.assertEqual(classify(record, None, events, checkpoint, True)[0], "POOL_EVICTED")
            events.append(dict(action="ADD", layer="PENDING", reason=None))
            self.assertEqual(classify(record, None, events, checkpoint, True)[0], "UNKNOWN")
            self.assertEqual(classify(record, None, events, dict(checkpoint, locations={H1: "PENDING"}), True)[0],
                             "PENDING")

    def test_missing_trace_and_state_invalidation(self):
        r = dict(hash=H1, kind="normal", label="n1")
        cp = dict(block=5, locations={}, errors={})
        self.assertEqual(classify(r, None, [], cp, True)[0], "UNKNOWN")
        e = [dict(action="INSERT", layer=None, reason=None),
             dict(action="DELETE", layer=None, reason="insufficient funds")]
        self.assertEqual(classify(r, None, e, cp, True)[0], "STATE_INVALIDATED")
        self.assertEqual(classify(r, None, e, cp, False)[0], "UNKNOWN")
        self.assertEqual(classify(dict(r, error="pending sub-pool is full", hash=None), None, [], cp, True)[0],
                         "REJECTED")
        self.assertEqual(classify(r, receipt(4, 21000, 100), e, cp, True)[0], "INCLUDED")


class ReportTests(unittest.TestCase):
    def test_drain_cost_gate_requires_matched_eviction_and_windows(self):
        groups = {"G1": dict(comparable=True, normalAdmitted=7040, normalCapacityEvicted=7000, normalIncluded=40, normalUnknown=0),
                  "G2": dict(comparable=True, normalAdmitted=7040, normalCapacityEvicted=7000, normalIncluded=40, normalUnknown=0)}
        self.assertTrue(drain_cost_comparable(groups))
        self.assertFalse(drain_cost_comparable({"G1": groups["G1"]}))
        self.assertFalse(drain_cost_comparable(dict(groups, G2=dict(groups["G2"], normalCapacityEvicted=6900))))
        self.assertFalse(drain_cost_comparable(dict(groups, G2=dict(groups["G2"], comparable=False))))

    def test_synthetic_csv_json_conservation_and_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = json.loads((HERE / "config_fixed.json").read_text())
            c.update(normalSenders=1, attackSenders=1, txsPerSender=2)
            (root / "config.json").write_text(json.dumps(c))
            folder = root / "G1"
            folder.mkdir()
            (folder / "workload_manifest.json").write_text(json.dumps(dict(
                clientVersion="erigon/3.5.0/linux-amd64/go1.25.11",
                group="G1", mode="attack", delegateCase="alice-sender")))
            normal = [dict(kind="normal", label="n00000-n1", hash=H1, nonce=0, gasPriceWei="100"),
                      dict(kind="normal", label="n00000-n2", hash=H2, nonce=1, gasPriceWei="100")]
            attack = [dict(kind="attack", label="a00000-x1", hash=H3, nonce=1, gasPriceWei="200"),
                      dict(kind="attack", label="a00000-x2", nonce=2, gasPriceWei="200",
                           error="pending sub-pool is full")]
            for kind, data in (("normal", normal), ("attack", attack)):
                (folder / (kind + ".jsonl")).write_text("".join(json.dumps(r) + "\n" for r in data))
            recs = [dict(kind="normal", label=normal[0]["label"], hash=H1, receipt=None),
                    dict(kind="normal", label=normal[1]["label"], hash=H2, receipt=receipt(5, 21000, 80)),
                    dict(kind="attack", label=attack[0]["label"], hash=H3, receipt=receipt(4, 30000, 150)),
                    dict(kind="attack", label=attack[1]["label"], hash=None, receipt=None)]
            (folder / "receipts.json").write_text(json.dumps(recs))
            (folder / "summary.json").write_text(json.dumps(dict(setupFeeByStageWei={
                "deploy": "10", "authorize": "20"}, receiverBalanceDeltaWei="5",
                attackBroadcastOverranWindow=False, windowAnchorBlock=2)))
            (folder / "normal_fee_plan.json").write_text(json.dumps(dict(sequenceSha256=sequence_sha256([100, 100]))))
            with (folder / "normal_fee_plan.csv").open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=("label", "gasPriceWei"))
                writer.writeheader()
                writer.writerows(dict(label=r["label"], gasPriceWei=100) for r in normal)
            lines = [
                f"[INFO] TX TRACING: Inserted txn by nonce idHash={H1[2:]} sender=1 nonce=0",
                f"[INFO] TX TRACING: added to subpool Pending, IdHash={H1[2:]}, sender=1, nonce=0",
                f"[INFO] TX TRACING: Inserted txn by nonce idHash={H2[2:]} sender=1 nonce=1",
                f"[INFO] TX TRACING: added to subpool Pending, IdHash={H2[2:]}, sender=1, nonce=1",
                f"[INFO] TX TRACING: Inserted txn by nonce idHash={H3[2:]} sender=2 nonce=1",
                f"[INFO] TX TRACING: added to subpool Pending, IdHash={H3[2:]}, sender=2, nonce=1",
                f"[INFO] TX TRACING: Deleted txn by nonce idHash={H1[2:]} sender=1 nonce=0 reason=\"pending sub-pool is full\"",
            ]
            for name, block in (("T0", 1), ("T1", 2), ("T2", 2), ("T3", 3), ("window1", 3), ("window10", 12), ("window12", 14)):
                log = folder / ("erigon_trace_" + name + ".log")
                log.write_text("\n".join([] if name == "T0" else (lines[:4] if name == "T1" else lines)) + "\n")
                snap = dict(name=name, block=block, snapshotStable=True, traceComplete=True,
                            traceFile=log.name, traceSha256=hashlib.sha256(log.read_bytes()).hexdigest(),
                            counts=dict(pending=2, baseFee="UNAVAILABLE", queued=0),
                            locations={}, errors={})
                (folder / ("pool_" + name + ".json")).write_text(json.dumps(snap))
            for w in (1, 10, 12):
                (folder / f"window{w}_marker.json").write_text(json.dumps(
                    dict(targetBlock=2+w, observedBlock=2+w, comparable=True)))
            out = summarize_group(folder)
            self.assertEqual(out["windows"]["10"]["normal"]["states"]["POOL_EVICTED"], 1)
            self.assertEqual(out["windows"]["10"]["normal"]["states"]["INCLUDED"], 1)
            self.assertEqual(out["windows"]["10"]["attack"]["rejected"], 1)
            self.assertEqual(out["windows"]["10"]["attack"]["actualPaidFeeWei"], str(30000 * 150))
            self.assertEqual(out["T4"]["firstAttackIncludedBlock"], 4)
            self.assertTrue((folder / "tx_states_window10.csv").exists())
            comp = summarize_evaluation(root)
            self.assertEqual(comp["groups"]["G1"]["normalCapacityEvicted"], 1)


if __name__ == "__main__":
    unittest.main()
