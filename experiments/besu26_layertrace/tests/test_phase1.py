import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from report import classify, summarize_evaluation, summarize_group
from run import evaluation_groups, network_config
from workload import delegate_tools


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class Phase1Tests(unittest.TestCase):
    def config(self):
        return json.loads((HERE / "config_phase1.json").read_text())

    def test_three_groups_and_historical_config(self):
        old = json.loads((HERE / "config.json").read_text())
        new = self.config()
        self.assertEqual(old["attackFirstPaddingBytes"], 8192)
        self.assertEqual(new["attackFirstPaddingBytes"], 0)
        self.assertEqual(new["attackTailPaddingBytes"], 8192)
        self.assertEqual(new["normalSenders"] * new["txsPerSender"], 7040)
        self.assertEqual(new["attackSenders"] * new["txsPerSender"], 16000)
        self.assertEqual([g[0] for g in evaluation_groups(new, "all")], ["G0", "G1", "G2"])
        self.assertEqual(evaluation_groups(new, "G2")[0][2], "alice-no-drain")
        self.assertEqual(network_config(old, "image")["participants"][0]["el_extra_params"],
                         network_config(new, "image")["participants"][0]["el_extra_params"])

    def test_control_delegation_matches_selector_without_value_transfer(self):
        drain = (HERE.parents[1] / "devtools/mpfuzz_style_exp5_pos/Alice7702Drain.sol").read_text()
        control = (HERE.parents[1] / "devtools/mpfuzz_style_exp5_pos/Alice7702NoDrain.sol").read_text()
        self.assertIn("function drainAll(address payable receiver)", drain)
        self.assertIn("function drainAll(address payable receiver)", control)
        self.assertEqual(delegate_tools.delegate_contract_spec(SimpleNamespace(case="alice-no-drain")),
                         ("Alice7702NoDrain.sol", "Alice7702NoDrain"))
        for forbidden in (".call{value:", ".transfer(", ".send(", "selfdestruct"):
            self.assertNotIn(forbidden, control.lower())

    def test_compiled_no_drain_runtime_has_no_external_calls(self):
        try:
            import solcx
        except ModuleNotFoundError:
            self.skipTest("py-solc-x is not installed")
        if "0.8.20" not in {str(version) for version in solcx.get_installed_solc_versions()}:
            self.skipTest("solc 0.8.20 is not installed")
        source = (HERE.parents[1] / "devtools/mpfuzz_style_exp5_pos/Alice7702NoDrain.sol").read_text()
        interface = delegate_tools.base.compile_with_solc(source, "Alice7702NoDrain", "0.8.20", "london")
        runtime = bytes.fromhex(interface["bin-runtime"].removeprefix("0x"))
        forbidden = {0xf1, 0xf2, 0xf4, 0xfa, 0xff}
        index = 0
        while index < len(runtime):
            opcode = runtime[index]
            self.assertNotIn(opcode, forbidden)
            index += 1
            if 0x60 <= opcode <= 0x7f:
                index += opcode - 0x5f

    def test_classification_requires_final_drop_and_capacity_choice(self):
        record = dict(kind="normal", hash="0x1")
        state = dict(observedState="POOL_DROPPED")
        self.assertEqual(classify(record, state, None, {"0x1"}, True), "CAPACITY_EVICTION")
        self.assertEqual(classify(record, state, None, set(), True), "UNKNOWN")
        self.assertEqual(classify(record, state, None, {"0x1"}, False), "UNKNOWN")
        self.assertEqual(classify(record, dict(observedState="REMOVED_INVALIDATED"), None, set(), True), "INVALIDATION")
        self.assertEqual(classify(record, dict(observedState="IN_LAYER"), None, set(), True), "IN_POOL")
        self.assertEqual(classify(record, state, dict(status="0x0"), {"0x1"}, True), "CONFIRMATION")
        self.assertEqual(classify(dict(hash="", error="rejected"), {}, None, set(), True), "REJECTION")

    def test_report_keeps_failed_receipt_fee_and_phase(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            group = root / "G1"
            group.mkdir()
            write_json(root / "config.json", config)
            write_json(group / "workload_manifest.json", dict(config=config, group="G1", mode="attack",
                       delegateCase="alice-sender", seed="seed01"))
            write_json(group / "summary.json", dict(setupFeeWei="100", setupFeeByStageWei={"deploy": "100"},
                       receiverBalanceDeltaWei="1000", windows={"1": {}, "10": {
                           "normal": {"included": 1}, "attack": {"included": 2, "successful": 1,
                           "failed": 1, "feeWei": "85"}}}))
            normal = [dict(kind="normal", label=f"n{i}", sender=f"0xn{i}", nonce=0, hash=f"0xn{i}") for i in range(4)]
            attack = [dict(kind="attack", label=f"a-x{i}", sender="0xa", nonce=i, hash=f"0xa{i}") for i in (1, 2)]
            (group / "normal.jsonl").write_text("".join(json.dumps(r) + "\n" for r in normal))
            (group / "attack.jsonl").write_text("".join(json.dumps(r) + "\n" for r in attack))
            write_csv(group / "tx_layers.csv", [
                dict(hash="0xn0", observedState="POOL_DROPPED", layer="", lastReason="DROPPED", lastUnixMillis="250", history="prioritized -> ready -> sparse"),
                dict(hash="0xn1", observedState="REMOVED_INVALIDATED", layer="", lastReason="INVALIDATED", lastUnixMillis="240", history="ready"),
                dict(hash="0xn2", observedState="REMOVED_CONFIRMED", layer="", lastReason="CONFIRMED", lastUnixMillis="280", history="ready"),
                dict(hash="0xn3", observedState="IN_LAYER", layer="prioritized", lastReason="NEW", lastUnixMillis="210", history="prioritized"),
                dict(hash="0xa1", observedState="REMOVED_CONFIRMED", layer="", lastReason="CONFIRMED", lastUnixMillis="290", history="ready"),
                dict(hash="0xa2", observedState="REMOVED_CONFIRMED", layer="", lastReason="CONFIRMED", lastUnixMillis="300", history="ready")])
            write_csv(group / "capacity_choices.csv", [dict(hash="0xn0", layer="sparse")])
            write_csv(group / "layer_history.csv", [
                dict(hash="0xn0", event="enter", layer="prioritized", seq=1, unixMillis=120),
                dict(hash="0xn0", event="enter", layer="ready", seq=2, unixMillis=180),
                dict(hash="0xn0", event="enter", layer="sparse", seq=3, unixMillis=240),
                dict(hash="0xn0", event="drop", layer="end", seq=4, unixMillis=250)])
            write_json(group / "layer_audit.json", dict(evidenceComplete=True, issues=[],
                       checkpoint={"unixMillis": 400}, sourceSha256="fixture"))
            write_json(group / "normal_timing.json", dict(before={"unixMillis": 100}, afterUnixMillis=180))
            write_json(group / "attack_timing.json", dict(before={"unixMillis": 200}, afterUnixMillis=300))
            for window in (1, 10):
                write_json(group / f"window{window}_marker.json", dict(targetBlock=20 + window,
                           observedBlock=20 + window, unixMillis=350))
            write_json(group / "first_attack_balances.json", [])
            def receipt(block, status, gas, price):
                return dict(blockNumber=hex(block), status=status, gasUsed=hex(gas), effectiveGasPrice=hex(price))
            write_json(group / "receipts.json", [
                dict(hash="0xn2", receipt=receipt(25, "0x1", 10, 1)),
                dict(hash="0xa1", receipt=receipt(26, "0x0", 10, 5)),
                dict(hash="0xa2", receipt=receipt(27, "0x1", 7, 5))])
            result = summarize_group(group)
            self.assertTrue(result["layerEvidenceComplete"])
            self.assertEqual(result["normalCapacityEvictedConfirmed"], 1)
            self.assertEqual(result["normalCapacityEvictedByFullWindow"], 1)
            self.assertEqual(result["outcomes"]["normal"]["INVALIDATION"], 1)
            self.assertEqual(result["outcomes"]["normal"]["IN_POOL"], 1)
            self.assertEqual(result["normalCapacityEvictedByPhase"]["attack_broadcast"], 1)
            self.assertEqual(result["normalEverEnteredLayer"]["sparse"], 1)
            self.assertEqual(result["attackByRole"]["first"]["failed"], 1)
            self.assertEqual(result["attackByRole"]["first"]["feeWei"], "50")
            self.assertEqual(result["attackByRole"]["tail"]["feeWei"], "35")
            table = summarize_evaluation(root)
            self.assertEqual(table[0]["attackGasFeeWei"], "85")
            self.assertEqual(table[0]["firstAttackIncluded"], 1)
            self.assertEqual(table[0]["tailAttackIncluded"], 1)
            audit = json.loads((group / "layer_audit.json").read_text())
            audit["checkpoint"]["unixMillis"] = 100
            write_json(group / "layer_audit.json", audit)
            stale = summarize_group(group)
            self.assertFalse(stale["layerEvidenceComplete"])
            self.assertIsNone(stale["normalCapacityEvictedConfirmed"])
            self.assertEqual(stale["outcomes"]["normal"]["UNKNOWN"], 3)


if __name__ == "__main__":
    unittest.main()
