import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit import replay
from run import network_config
from workload import Rpc, validate_funder_key, validate_setup
from workload import base


class AuditTests(unittest.TestCase):
    def replay(self, events, dropped=0, trailer=b"", checkpoint=True):
        rows = [dict(schema=1, event="session_start", session="s")]
        rows += [dict(schema=1, session="s", seq=i, hash="0xaa", sender="0xbb", nonce="1", **e)
                 for i, e in enumerate(events, 1)]
        if checkpoint:
            rows.append(dict(schema=1, event="checkpoint", session="s", written=len(events), assigned=len(events), dropped=dropped))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.jsonl"
            path.write_bytes(b"\n".join(json.dumps(row).encode() for row in rows) + b"\n" + trailer)
            return replay(path, {"0xaa": {"kind": "normal"}, "0xcc": {"kind": "attack"}})

    def test_layer_move_is_not_pool_eviction(self):
        r = self.replay([dict(event="enter", layer="ready", reason="NEW"),
                         dict(event="leave", layer="ready", scope="LAYER", reason="evicted"),
                         dict(event="enter", layer="sparse", reason="MOVE")])
        self.assertTrue(r["evidenceComplete"])
        self.assertEqual(r["states"][0]["layer"], "sparse")
        self.assertEqual(r["states"][0]["observedState"], "IN_LAYER")
        self.assertEqual(r["states"][1]["observedState"], "UNOBSERVED")

    def test_final_drop_and_reaccept(self):
        r = self.replay([dict(event="drop", layer="end", reason="dropped", scope="POOL"),
                         dict(event="enter", layer="ready", reason="NEW")])
        self.assertEqual(r["states"][0]["observedState"], "IN_LAYER")

    def test_invalidation_is_not_capacity_drop(self):
        r = self.replay([dict(event="leave", layer="ready", reason="invalidated", scope="POOL")])
        self.assertEqual(r["states"][0]["observedState"], "REMOVED_INVALIDATED")

    def test_promotion_and_replacement(self):
        r = self.replay([dict(event="enter", layer="sparse", reason="NEW"),
                         dict(event="leave", layer="sparse", reason="promoted", scope="LAYER"),
                         dict(event="enter", layer="ready", reason="PROMOTED"),
                         dict(event="leave", layer="ready", reason="cross_layer_replaced", scope="POOL")])
        self.assertEqual(r["states"][0]["observedState"], "REMOVED_CROSS_LAYER_REPLACED")
        self.assertEqual(r["states"][0]["history"], "sparse -> ready")

    def test_reset_loses_location(self):
        r = self.replay([dict(event="enter", layer="ready", reason="NEW"), dict(event="reset", layer="ready")])
        self.assertEqual(r["states"][0]["observedState"], "UNKNOWN_RESET")

    def test_dropped_events_block_claims(self):
        r = self.replay([dict(event="drop", layer="end", reason="dropped")], dropped=1)
        self.assertFalse(r["evidenceComplete"])
        self.assertFalse(r["states"][0]["evidenceComplete"])

    def test_no_checkpoint_is_untrusted(self):
        r = self.replay([dict(event="enter", layer="ready")], checkpoint=False)
        self.assertFalse(r["evidenceComplete"])
        self.assertEqual(r["states"][0]["observedState"], "UNOBSERVED")

    def test_unflushed_events_excluded(self):
        trailer = json.dumps(dict(schema=1, session="s", seq=2, event="drop", hash="0xaa", layer="end", reason="dropped")).encode() + b"\n"
        r = self.replay([dict(event="enter", layer="ready")], trailer=trailer)
        self.assertTrue(r["evidenceComplete"])
        self.assertEqual(r["eventsAfterCheckpoint"], 1)
        self.assertEqual(r["states"][0]["observedState"], "IN_LAYER")

    def test_truncated_record_is_not_silently_accepted(self):
        r = self.replay([], trailer=b'{"schema":')
        self.assertFalse(r["evidenceComplete"])

    def test_duplicate_sequence_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace"
            rows = [dict(schema=1, session="s", event="session_start"),
                    dict(schema=1, session="s", seq=1, event="block_begin"),
                    dict(schema=1, session="s", seq=1, event="block_end"),
                    dict(schema=1, session="s", event="checkpoint", written=2, assigned=2, dropped=0)]
            path.write_text("\n".join(map(json.dumps, rows)))
            self.assertFalse(replay(path)["evidenceComplete"])


class WorkloadTests(unittest.TestCase):
    def test_missing_sponsor_key_fails_before_network_start(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "EXP5_FUNDER_PRIVATE_KEY"):
                validate_funder_key()

    def test_wrong_sponsor_key_rejected_without_disclosing_it(self):
        with patch.dict("os.environ", {"EXP5_FUNDER_PRIVATE_KEY": "0x" + "11" * 32}):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                validate_funder_key()

    def test_valid_sponsor_key_sets_legacy_account(self):
        from eth_account import Account
        key = "0x" + "11" * 32
        with patch.dict("os.environ", {"EXP5_FUNDER_PRIVATE_KEY": key}), patch.dict(
            base.DEFAULT_FUNDER, {"address": Account.from_key(key).address}
        ):
            validate_funder_key()
            self.assertEqual(base.DEFAULT_FUNDER["private_key"], key)

    def test_rpc_batches_match_ids_not_response_order(self):
        rpc = Rpc("unused")
        rpc.request = lambda request: [dict(id=1, result="second"), dict(id=0, result="first")]
        self.assertEqual(rpc.batch("eth_getBalance", [[], []]), ["first", "second"])

    def test_rpc_batch_error_not_treated_as_absence(self):
        rpc = Rpc("unused")
        rpc.request = lambda request: [dict(id=0, error={"message": "unavailable"})]
        with self.assertRaises(RuntimeError):
            rpc.batch("eth_getTransactionReceipt", [[]])

    def test_fixed_design_and_upfront_balance(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        self.assertEqual(config["normalSenders"] * config["txsPerSender"], 7040)
        self.assertEqual(config["attackSenders"] * config["txsPerSender"], 16000)
        self.assertEqual(config["attackFirstPaddingBytes"], config["attackTailPaddingBytes"])
        self.assertFalse(any("capacity" in p for p in network_config(config, "test")["participants"][0]["el_extra_params"]))
        for attack in (False, True):
            row = dict(label="a000" if attack else "n000", balanceWei=str(config["attackFundEth" if attack else "normalFundEth"] * 10**18),
                       nonce=1 if attack else 0, code="0xef0100" + "12" * 20 if attack else "0x")
            validate_setup([row], config, "0x" + "12" * 20)
            row["balanceWei"] = "1"
            with self.assertRaises(RuntimeError):
                validate_setup([row], config, "0x" + "12" * 20)


if __name__ == "__main__":
    unittest.main()
