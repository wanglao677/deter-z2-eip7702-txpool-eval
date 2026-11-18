"""Small deterministic Phase 2 fixtures; no Kurtosis network or real attack."""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from metrics import FIELDS, collect, setup_fee_totals, summarize_slots
from plan import phase_for_slot, schedule, validate_plan
from run import load_config, audit_group, comparison, recover_group
from plot import plot as plot_figure

ETH = 10**18


class Args:
    def __init__(self, client, fee_mode=None):
        self.client = client
        self.config = None
        self.fee_mode = fee_mode


class PlanTests(unittest.TestCase):
    def accounts(self, count):
        return [dict(label=f"n{i:05d}", address=f"0x{i+1:040x}") for i in range(count)]

    def test_matched_deterministic_arrivals_and_nonce(self):
        for client, rate in (("besu", 160), ("erigon", 600)):
            config = load_config(Args(client))
            accounts = self.accounts(55)
            first = schedule(config, "seed01", accounts, client)
            self.assertEqual(first, schedule(config, "seed01", accounts, client))
            self.assertNotEqual(first, schedule(config, "seed02", accounts, client))
            self.assertEqual(first, schedule(config, "seed01", accounts, client))  # G0/G1/G2
            self.assertGreater(len(set(row["slotIndex"] for row in first)), 45)
            counts = {i: 0 for i in range(55)}
            for row in first:
                self.assertEqual(row["nonce"], counts[row["senderIndex"]])
                self.assertEqual(row["sender"], accounts[row["senderIndex"]]["address"])
                counts[row["senderIndex"]] += 1
            self.assertLess(max(row["slotIndex"] for row in first), 50)
            self.assertGreater(len(set(sum(1 for row in first if row["slotIndex"] == s)
                                       for s in range(50))), 1)
            budget = validate_plan(config, first, accounts, 60_000_000)
            self.assertLess(budget["maxPlannedGasPerSlot"], 36_000_000)
            self.assertLess(budget["maxSenderUpfrontWei"], config["normalFundEth"] * ETH)
            self.assertEqual(config["phase2"]["normalArrivalRatePerSlot"], rate)

    def test_erigon_60s_profile_keeps_matched_plan_and_gas_budget(self):
        args = Args("erigon")
        args.config = HERE / "config_erigon_60s.json"
        short = load_config(args)
        original = load_config(Args("erigon"))
        self.assertEqual(short["secondsPerSlot"], 60)
        self.assertEqual(original["secondsPerSlot"], 300)
        for key in ("phase2", "normalGas", "attackGas", "attackSenders",
                    "txsPerSender", "normalGasPriceMode"):
            self.assertEqual(short[key], original[key])
        accounts = self.accounts(55)
        rows = schedule(short, "seed01", accounts, "erigon")
        self.assertEqual(rows, schedule(short, "seed01", accounts, "erigon"))
        self.assertLess(rows[-1]["offsetMillis"], 50 * 60 * 1000)
        self.assertEqual(rows[0]["gasPriceWei"],
                         short["priceUnitWei"] * short["normalPriceMultiplier"])
        budget = validate_plan(short, rows, accounts, 60_000_000)
        self.assertLess(budget["maxPlannedGasPerSlot"], 36_000_000)
        self.assertLess(budget["maxSenderUpfrontWei"], short["normalFundEth"] * ETH)

    def test_fixed_and_randomized_prices_keep_other_parameters(self):
        fixed = load_config(Args("besu"))
        random_fee = load_config(Args("besu", "randomized-uniform"))
        accounts = self.accounts(55)
        a = schedule(random_fee, "seed01", accounts, "besu")
        b = schedule(random_fee, "seed01", accounts, "besu")
        self.assertEqual(a, b)
        self.assertGreater(len({row["gasPriceWei"] for row in a}), 1)
        self.assertEqual([(r["slotIndex"], r["nonce"], r["sender"]) for r in a],
                         [(r["slotIndex"], r["nonce"], r["sender"])
                          for r in schedule(fixed, "seed01", accounts, "besu")])
        self.assertEqual(fixed["attackGas"], random_fee["attackGas"])
        self.assertEqual(fixed["attackFirstPaddingBytes"], 0)
        self.assertEqual(random_fee["attackTailPaddingBytes"], 8192)

    def test_budget_rejects_invalid_capacity_or_funding(self):
        config = load_config(Args("besu", "randomized-uniform"))
        accounts = self.accounts(55)
        rows = schedule(config, "seed01", accounts, "besu")
        with self.assertRaises(RuntimeError):
            validate_plan(config, rows, accounts, 20_000_000)
        config["normalFundEth"] = 1
        with self.assertRaises(RuntimeError):
            validate_plan(config, rows, accounts, 60_000_000)

    def test_local_funder_replaces_legacy_default_before_csv_load(self):
        import importlib.util
        from eth_account import Account
        spec = importlib.util.spec_from_file_location("phase2_workload", HERE / "workload.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.base.DEFAULT_FUNDER.copy()
        key = "0x" + "11" * 32
        address = Account.from_key(key).address
        self.assertNotEqual(original["address"].lower(), address.lower())
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "key_prive_exp5.csv"
                path.write_text("label,pub_key,priv_key\n")
                accounts = module.load_accounts(path, dict(address=address, private_key=key))
                self.assertEqual(accounts["funded0"]["address"].lower(), address.lower())
                self.assertEqual(accounts["funded0"]["private_key"], key)
        finally:
            module.base.DEFAULT_FUNDER.clear()
            module.base.DEFAULT_FUNDER.update(original)

    def test_real_derived_normal_account_signs_legacy_transaction(self):
        import importlib.util
        from eth_account import Account
        spec = importlib.util.spec_from_file_location("phase2_workload", HERE / "workload.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for client in ("besu", "erigon"):
            with self.subTest(client=client):
                config = load_config(Args(client))
                account = module.scale.derive_accounts("n", 1, "seed01:" + client)[0]
                recipient = module.normal_signer_addresses([account])[0]
                self.assertEqual(recipient.lower(), account["address"].lower())
                self.assertNotEqual(recipient, account["address"])
                row = dict(nonce=0, gasPriceWei=config["priceUnitWei"] *
                           config["normalPriceMultiplier"])
                signed = module.sign_normal(row, account, recipient,
                                            bytes(config["normalPaddingBytes"]), config,
                                            config.get("chainId", 13371349))
                self.assertEqual(Account.recover_transaction(signed.raw_transaction).lower(),
                                 account["address"].lower())

    def test_normal_sender_error_interrupts_wait(self):
        import importlib.util
        import threading
        import time
        spec = importlib.util.spec_from_file_location("phase2_workload", HERE / "workload.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "Normal sender failed"):
            module.sleep_until(start + 60, threading.Event(), ["TypeError: invalid to"])
        self.assertLess(time.monotonic() - start, 1)

    def test_phase_names(self):
        phase = dict(preSlots=1, attackSlots=1, postSlots=1)
        self.assertEqual([phase_for_slot(i, phase) for i in range(3)],
                         ["pre_attack", "attack_observation", "post_injection_observation"])
        with self.assertRaises(ValueError):
            phase_for_slot(3, phase)


class FakeRpc:
    def __init__(self, blocks, receipt):
        self.blocks = blocks
        self.receipt = receipt

    def call(self, method, params):
        if method != "eth_getBlockByNumber":
            raise AssertionError(method)
        return self.blocks[int(params[0], 16)]

    def batch(self, method, arguments):
        if method != "eth_getTransactionReceipt":
            raise AssertionError(method)
        return [self.receipt[a[0].lower()] for a in arguments]


class MetricTests(unittest.TestCase):
    def fixture(self):
        config = load_config(Args("besu"))
        config["phase2"].update(preSlots=1, attackSlots=1, postSlots=1)
        normal_hash, attack_hash = "0x" + "1" * 64, "0x" + "2" * 64
        blocks = {
            10: dict(number=hex(10), hash="0x" + "a" * 64, timestamp=hex(1000),
                     baseFeePerGas=hex(2), gasLimit=hex(100_000), gasUsed=hex(21000),
                     transactions=[dict(hash=normal_hash, gasPrice=hex(5), type="0x0")]),
            11: dict(number=hex(11), hash="0x" + "b" * 64, timestamp=hex(1600),
                     baseFeePerGas=hex(2), gasLimit=hex(100_000), gasUsed=hex(30000),
                     transactions=[dict(hash=attack_hash, gasPrice=hex(7), type="0x0")]),
        }
        receipts = {
            normal_hash: dict(transactionHash=normal_hash, blockHash=blocks[10]["hash"],
                              gasUsed=hex(21000), effectiveGasPrice=hex(5)),
            attack_hash: dict(transactionHash=attack_hash, blockHash=blocks[11]["hash"],
                              gasUsed=hex(30000), effectiveGasPrice=hex(7)),
        }
        submissions = [
            dict(kind="normal", hash=normal_hash, plannedUnixMillis=5000, attemptUnixMillis=10000),
            dict(kind="attack", hash=attack_hash, attemptUnixMillis=310000),
        ]
        snapshots = [dict(blockNumber=10, unixMillis=11000, pending=4, queued=2),
                     dict(blockNumber=11, unixMillis=611000, pending=1, queued=5)]
        return config, blocks, receipts, submissions, snapshots

    def test_receipt_fees_slots_and_spam_estimate(self):
        config, blocks, receipts, submissions, snapshots = self.fixture()
        data = summarize_slots(config, "besu", "G0", 1000, 10, 0, 0,
                               list(blocks.values()), receipts, submissions, snapshots)
        self.assertEqual(len(data), 3)
        self.assertEqual([row["phase"] for row in data],
                         ["pre_attack", "attack_observation", "post_injection_observation"])
        self.assertEqual([row["blockPresent"] for row in data], [True, False, True])
        self.assertEqual([row["relativeBlockIndex"] for row in data], [0, "", 1])
        self.assertEqual(data[0]["normalIncluded"], 1)
        self.assertEqual(data[0]["normalPaidFeeWei"], 105000)
        self.assertEqual(data[0]["normalBaseBurnWei"], 42000)
        self.assertEqual(data[0]["normalPriorityFeeWei"], 63000)
        self.assertEqual(data[0]["maxIncludedGasPriceWei"], 5)
        self.assertEqual(data[0]["estimatedConventionalSpamCostWei"], 500000)
        self.assertEqual(data[1]["spamEstimateStatus"], "NO_BLOCK")
        self.assertEqual(data[1]["blockNumber"], "")
        self.assertEqual(data[2]["attackPaidFeeWei"], 210000)
        self.assertEqual(data[2]["attackPriorityFeeWei"], 150000)
        self.assertEqual(data[0]["poolPending"], 4)
        self.assertEqual(data[0]["normalSubmitted"], 1)
        self.assertEqual(data[0]["normalBroadcastLagMeanMs"], 5000)
        self.assertEqual(data[0]["normalBroadcastLateCount"], 1)
        self.assertEqual(data[1]["attackSubmitted"], 1)
        self.assertEqual(data[2]["spamEstimateStatus"], "ESTIMATED_FROM_G0_BLOCK")
        g1 = summarize_slots(config, "besu", "G1", 1000, 10, 0, 0,
                             list(blocks.values()), receipts, submissions, snapshots)
        self.assertEqual(g1[0]["spamEstimateStatus"], "G0_ONLY")
        self.assertEqual(g1[0]["estimatedConventionalSpamCostWei"], "")

    def test_collect_writes_complete_csv_and_outcomes(self):
        config, blocks, receipts, submissions, snapshots = self.fixture()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "normal_schedule.jsonl").write_text(json.dumps(dict(index=0)) + "\n")
            (folder / "normal.jsonl").write_text(json.dumps(submissions[0]) + "\n")
            (folder / "attack.jsonl").write_text(json.dumps(submissions[1]) + "\n")
            (folder / "pool_snapshots.jsonl").write_text("".join(json.dumps(r) + "\n" for r in snapshots))
            summary = collect(FakeRpc(blocks, receipts), folder, config, "besu", "G0",
                              1000, 10, 0, 0, 11)
            self.assertEqual(summary["missedSlots"], 1)
            self.assertEqual(summary["normalPaidFeeWei"], "105000")
            self.assertEqual(summary["attackPaidFeeWei"], "210000")
            with (folder / "per_block_metrics.csv").open(newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames), FIELDS)
                self.assertEqual(len(list(reader)), 3)
            self.assertEqual(len((folder / "transaction_outcomes.jsonl").read_text().splitlines()), 2)

    def test_offline_audit_recomputes_saved_metrics(self):
        import hashlib
        config, blocks, receipts, submissions, snapshots = self.fixture()
        config["normalSenders"] = 1
        config["attackSenders"] = 1
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "G0"
            folder.mkdir()
            (root / "config.json").write_text(json.dumps(config))
            payload = json.dumps(dict(index=0)) + "\n"
            (folder / "normal_schedule.jsonl").write_text(payload)
            (root / "normal_schedule.jsonl").write_text(payload)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            (folder / "anchor.json").write_text(json.dumps(dict(
                blockNumber=10, slotNumber=0, unixMillis=0, scheduleSha256=digest)))
            (folder / "workload_manifest.json").write_text(json.dumps(dict(
                genesis=dict(timestamp=hex(1000)), client="besu", scheduleSha256=digest)))
            (folder / "normal.jsonl").write_text(json.dumps(submissions[0]) + "\n")
            (folder / "attack.jsonl").write_text(json.dumps(submissions[1]) + "\n")
            (folder / "pool_snapshots.jsonl").write_text("".join(json.dumps(r) + "\n" for r in snapshots))
            collect(FakeRpc(blocks, receipts), folder, config, "besu", "G0", 1000, 10, 0, 0, 11)
            with self.assertRaisesRegex(RuntimeError, "incomplete post-collection summary"):
                audit_group(root, "G0")
            setup_receipt = dict(status="0x1", gasUsed="0x64", effectiveGasPrice="0x2")
            setup_rows = [
                dict(stage="deploy", receipt=dict(kind="deploy", label="Alice7702Drain",
                                                  hash="0x1", receipt=setup_receipt)),
                dict(stage="fund_normal", receipt=setup_receipt),
                dict(stage="fund_attack", receipt=setup_receipt),
                dict(stage="authorize", receipt=setup_receipt),
            ]
            (folder / "setup_receipts.json").write_text(json.dumps(setup_rows))
            (folder / "receiver_balance.json").write_text(json.dumps(dict(
                beforeWei="5", afterWei="5", observedThroughBlock=11)))
            (folder / "phase_markers.json").write_text(json.dumps(dict(
                observationEndBlock=11, attackWasBroadcast=False)))
            recovered = recover_group(root, "G0")
            self.assertEqual(recovered["setupFeeWei"], "800")
            self.assertEqual(recovered["setupFeeByStageWei"]["deploy"], "200")
            self.assertTrue(recovered["recoveredFromSavedEvidence"])
            self.assertEqual(recover_group(root, "G0")["setupFeeWei"], "800")
            self.assertEqual(len(audit_group(root, "G0")), 3)
            self.assertEqual(comparison(root)["recovery"]["status"],
                             "NOT_EVALUABLE_UNTIL_G0_AND_G1")
            csv_path = folder / "per_block_metrics.csv"
            csv_path.write_text(csv_path.read_text().replace("105000", "105001", 1))
            with self.assertRaises(RuntimeError):
                audit_group(root, "G0")

    def test_setup_fee_validation_accepts_direct_receipts_and_rejects_missing_gas(self):
        config = self.fixture()[0]
        config["normalSenders"] = 1
        config["attackSenders"] = 1
        receipt = dict(status="0x1", gasUsed="0x64", effectiveGasPrice="0x2")
        rows = [dict(stage=stage, receipt=receipt) for stage in (
            "deploy", "fund_normal", "fund_attack", "authorize")]
        self.assertEqual(setup_fee_totals(rows, config), {
            "deploy": 200, "fund_normal": 200, "fund_attack": 200, "authorize": 200})
        rows[0] = dict(stage="deploy", receipt=dict(status="0x1", effectiveGasPrice="0x2"))
        with self.assertRaisesRegex(RuntimeError, "missing complete setup receipt"):
            setup_fee_totals(rows, config)
        with self.assertRaisesRegex(RuntimeError, "Setup receipt counts"):
            setup_fee_totals(rows[1:], config)

    def test_plot_synthetic_fixture_is_labeled(self):
        config, blocks, receipts, submissions, snapshots = self.fixture()
        config["syntheticFixture"] = True
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "G0").mkdir()
            (root / "G1").mkdir()
            (root / "config.json").write_text(json.dumps(config))
            (root / "G1" / "phase_markers.json").write_text(json.dumps(dict(
                anchorUnixMillis=0, injectionWindowStartUnixMillis=300000,
                injectionEndUnixMillis=310000, attackWasBroadcast=True)))
            for group in ("G0", "G1"):
                calculated = summarize_slots(config, "besu", group, 1000, 10, 0, 0,
                                             list(blocks.values()), receipts, submissions, snapshots)
                with (root / group / "per_block_metrics.csv").open("w", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=FIELDS)
                    writer.writeheader()
                    writer.writerows(calculated)
            manifest = plot_figure(root, root / "figures")
            self.assertTrue(manifest["syntheticFixture"])
            self.assertTrue((root / "figures" / "figure5_phase2.pdf").is_file())
            with (root / "figures" / "figure5_curves.csv").open(newline="") as handle:
                entries = list(csv.DictReader(handle))
            self.assertEqual(entries[0]["estimatedConventionalSpamCostEth"], "5e-13")
            self.assertEqual(entries[1]["g0BlockPresent"], "False")

    def test_mismatch_or_missing_receipt_fails(self):
        config, blocks, receipts, submissions, snapshots = self.fixture()
        with self.assertRaises(RuntimeError):
            summarize_slots(config, "besu", "G0", 1000, 10, 0, 0,
                            list(blocks.values()), {}, submissions, snapshots)
        broken = dict(receipts)
        broken["0x" + "1" * 64] = dict(broken["0x" + "1" * 64], gasUsed=hex(20000))
        with self.assertRaises(RuntimeError):
            summarize_slots(config, "besu", "G0", 1000, 10, 0, 0,
                            list(blocks.values()), broken, submissions, snapshots)
        missing_price = {n: dict(block) for n, block in blocks.items()}
        missing_price[10]["transactions"] = [dict(hash="0x" + "1" * 64)]
        rows = summarize_slots(config, "besu", "G0", 1000, 10, 0, 0,
                               list(missing_price.values()), receipts, submissions, snapshots)
        self.assertEqual(rows[0]["spamEstimateStatus"], "MISSING_INCLUDED_GAS_PRICE")
        self.assertEqual(rows[0]["estimatedConventionalSpamCostWei"], "")
        wrong_price = {n: dict(block) for n, block in blocks.items()}
        wrong_price[10]["transactions"] = [dict(hash="0x" + "1" * 64,
                                                 gasPrice=hex(6), type="0x0")]
        with self.assertRaises(RuntimeError):
            summarize_slots(config, "besu", "G0", 1000, 10, 0, 0,
                            list(wrong_price.values()), receipts, submissions, snapshots)


if __name__ == "__main__":
    unittest.main()
