import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from normal_fee import bins, gwei, parameters, plan, prices, sequence_sha256, statistics, verify_records, write_plan
from report import normal_record_price, receipt_fee, summarize_evaluation, summarize_normal_fee_bins
from run import rpc_tx_fee_cap_wei
from workload import validate_setup


class NormalFeeTests(unittest.TestCase):
    def setUp(self):
        self.fixed = json.loads((HERE / "config_phase1.json").read_text())
        self.random = json.loads((HERE / "config_phase1_random_fee.json").read_text())

    def test_random_config_only_changes_normal_fee_and_name(self):
        changed = {key for key in self.random if self.random.get(key) != self.fixed.get(key)}
        self.assertEqual(changed, {"name", "normalGasPriceMode", "normalFeeSpreadBps", "normalFeeRandomSeed"})
        self.assertEqual(self.fixed.get("normalGasPriceMode", "fixed"), "fixed")
        spec = parameters(self.random)
        self.assertEqual(spec["centerWei"], 1_714_285_714_284)
        self.assertEqual(spec["spreadBps"], 1000)
        self.assertLess(spec["minimumWei"], spec["centerWei"])
        self.assertGreater(spec["maximumWei"], spec["centerWei"])
        self.assertEqual(prices(self.fixed, 4), [spec["centerWei"]] * 4)
        with tempfile.TemporaryDirectory() as temp:
            fixed_plan = plan(dict(self.fixed, txsPerSender=2),
                              [{"label": "n00000", "address": "0x" + "12" * 20}])
            self.assertIsNone(write_plan(temp, self.fixed, fixed_plan))
            self.assertFalse((Path(temp) / "normal_fee_prices.txt").exists())

    def test_same_seed_generates_exact_ordered_prices_and_rejects_mismatch(self):
        accounts = [{"label": "n00000", "address": "0x" + "12" * 20},
                    {"label": "n00001", "address": "0x" + "34" * 20}]
        config = dict(self.random, txsPerSender=4)
        g0 = plan(config, accounts)
        g1 = plan(config, accounts)
        g2 = plan(config, accounts)
        self.assertEqual(g0, g1)
        self.assertEqual(g1, g2)
        other_accounts = [dict(account, address="0x" + "56" * 20) for account in accounts]
        self.assertEqual([row["gasPriceWei"] for row in plan(config, other_accounts)],
                         [row["gasPriceWei"] for row in g0])
        self.assertEqual([r["label"] for r in g0[:5]],
                         ["n00000-n1", "n00000-n2", "n00000-n3", "n00000-n4", "n00001-n1"])
        self.assertGreater(len({r["gasPriceWei"] for r in g0}), 1)
        self.assertTrue(all(parameters(config)["minimumWei"] <= r["gasPriceWei"] <= parameters(config)["maximumWei"]
                            for r in g0))
        records = [dict(row, gasPriceWei=str(row["gasPriceWei"]), hash=f"0x{index}")
                   for index, row in enumerate(g0)]
        verify_records(records, g0)
        with tempfile.TemporaryDirectory() as temp:
            file = write_plan(temp, config, g0)
            self.assertEqual(file.read_text().splitlines(),
                             [str(r["gasPriceWei"]) for r in g0])
            self.assertEqual(json.loads((Path(temp) / "normal_fee_plan.json").read_text())["sequenceSha256"],
                             sequence_sha256([r["gasPriceWei"] for r in g0]))
            with (Path(temp) / "normal_fee_plan.csv").open(newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), len(g0))
        records[2]["gasPriceWei"] = "1"
        with self.assertRaisesRegex(RuntimeError, "record 2 at gasPriceWei"):
            verify_records(records, g0)
        config["normalSendOrder"] = "round-robin"
        self.assertEqual([r["label"] for r in plan(config, accounts)[:4]],
                         ["n00000-n1", "n00001-n1", "n00000-n2", "n00001-n2"])

    def test_maximum_price_is_used_for_fee_cap_and_upfront_funding(self):
        config = dict(self.random, normalGas=2_800_000)
        with self.assertRaisesRegex(RuntimeError, "normal maximum fee"):
            rpc_tx_fee_cap_wei(config)
        config = self.random
        self.assertEqual(rpc_tx_fee_cap_wei(config), 5 * 10**18)
        row = dict(label="n00000", balanceWei=str(config["txsPerSender"] *
                   config["normalGas"] * parameters(config)["centerWei"]), code="0x", nonce=0)
        with self.assertRaisesRegex(RuntimeError, "insufficient cumulative upfront balance"):
            validate_setup([row], config, "0x" + "12" * 20)
        row["balanceWei"] = str(50 * 10**18)
        validate_setup([row], config, "0x" + "12" * 20)

    def test_stats_bins_and_receipt_fee_are_distinct(self):
        config = self.random
        definitions = bins(config)
        self.assertEqual(len(definitions), 4)
        values = [(item["lowerWei"] + item["upperExclusiveWei"] - 1) // 2 for item in definitions]
        stats = statistics(values)
        self.assertEqual(stats["count"], 4)
        self.assertEqual(stats["minGwei"], gwei(values[0]))
        self.assertEqual(stats["percentileMethod"], "nearest-rank")
        self.assertEqual(stats["p10Gwei"], stats["minGwei"])
        self.assertEqual(stats["p90Gwei"], stats["maxGwei"])
        receipt = dict(gasUsed="0xa", effectiveGasPrice="0x2", status="0x1")
        self.assertEqual(receipt_fee(receipt), 20)
        entries = [
            dict(price=values[0], outcome="CAPACITY_EVICTION", receipt=None, accepted=True, dropUnixMillis=150),
            dict(price=values[1], outcome="INVALIDATION", receipt=None, accepted=True),
            dict(price=values[2], outcome="CONFIRMATION", receipt=receipt, accepted=True),
            dict(price=values[3], outcome="IN_POOL", receipt=None, accepted=True),
        ]
        rows = summarize_normal_fee_bins(config, entries, True, "G1", 200)
        self.assertEqual([r["normalAccepted"] for r in rows], [1, 1, 1, 1])
        self.assertEqual([r["evictionRate"] for r in rows], ["1.000000", "0.000000", "0.000000", "0.000000"])
        self.assertEqual(rows[2]["includedPaidFeeWei"], "20")
        self.assertEqual(rows[0]["normalCapacityEvictedByFullWindow"], 1)
        self.assertEqual(rows[0]["evictionRateByFullWindow"], "1.000000")
        untrusted = summarize_normal_fee_bins(config, entries, False, "G1", 200)
        self.assertTrue(all(r["evictionRate"] is None for r in untrusted))
        self.assertTrue(all(r["normalCapacityEvictedConfirmed"] is None for r in untrusted))
        self.assertEqual(normal_record_price({}, self.fixed), parameters(self.fixed)["centerWei"])
        with self.assertRaisesRegex(RuntimeError, "lacks signed"):
            normal_record_price({}, self.random)

    def test_evaluation_refuses_different_group_fee_sequences(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "G0").mkdir()
            (root / "G1").mkdir()
            (root / "G0" / "group_summary.json").write_text('{"normalFeeSequenceSha256":"a"}')
            (root / "G1" / "group_summary.json").write_text('{"normalFeeSequenceSha256":"b"}')
            with self.assertRaisesRegex(RuntimeError, "sequences differ"):
                summarize_evaluation(root)


if __name__ == "__main__":
    unittest.main()
