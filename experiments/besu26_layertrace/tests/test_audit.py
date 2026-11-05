import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit import replay
from run import network_config, prepare, rpc_tx_fee_cap_wei, validate_besu_cli
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
    def config(self):
        return json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())

    def test_besu_fee_cap_is_serialized_in_wei(self):
        config = self.config()
        params = network_config(config, "test")["participants"][0]["el_extra_params"]
        self.assertIn("--rpc-tx-feecap=5000000000000000000", params)
        self.assertNotIn("--rpc-tx-feecap=20", params)
        self.assertNotIn("--rpc-tx-feecap=20000000000000000000", params)
        # Reproduce the rejected deployment: 0.002 ETH is above 20 Wei.
        deploy_fee = 2_000_000 * config["setupGasPriceWei"]
        self.assertGreater(deploy_fee, 20)
        self.assertLess(deploy_fee, rpc_tx_fee_cap_wei(config))

    def test_fee_cap_cli_unsigned_long_limit(self):
        from types import SimpleNamespace
        config = self.config()
        config["rpcTxFeeCapEth"] = 18
        self.assertEqual(rpc_tx_fee_cap_wei(config), 18 * 10**18)
        for cap in (19, 20):
            with self.subTest(cap=cap), patch("run.shutil.which") as which:
                config["rpcTxFeeCapEth"] = cap
                with self.assertRaisesRegex(RuntimeError, "unsigned 64-bit"):
                    prepare(SimpleNamespace(image="test"), config)
                which.assert_not_called()

    def test_real_cli_preflight_uses_generated_options(self):
        from subprocess import CompletedProcess
        config = self.config()
        params = network_config(config, "test")["participants"][0]["el_extra_params"]
        with patch("run.subprocess.run", return_value=CompletedProcess([], 0, "besu/v26.6.1/linux\n", "")) as run:
            validate_besu_cli("test", params, "26.6.1")
        command = run.call_args.args[0]
        self.assertEqual(command[-3:], [*params, "--version"])
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_real_cli_preflight_rejects_parser_error(self):
        from subprocess import CompletedProcess
        with patch("run.subprocess.run", return_value=CompletedProcess([], 1, "", "exceeds range of unsigned long")):
            with self.assertRaisesRegex(RuntimeError, "exceeds range of unsigned long"):
                validate_besu_cli("test", ["--rpc-tx-feecap=20000000000000000000"], "26.6.1")

    def test_real_cli_preflight_rejects_wrong_version(self):
        from subprocess import CompletedProcess
        with patch("run.subprocess.run", return_value=CompletedProcess([], 0, "besu/v25.1.0/linux", "")):
            with self.assertRaisesRegex(RuntimeError, "preflight failed"):
                validate_besu_cli("test", [], "26.6.1")

    def test_real_cli_timeout_cleans_only_its_container(self):
        from subprocess import CompletedProcess, TimeoutExpired
        with patch("run.subprocess.run", side_effect=[TimeoutExpired("docker", 60), CompletedProcess([], 0)]) as run:
            with self.assertRaisesRegex(RuntimeError, "preflight timed out"):
                validate_besu_cli("test", [], "26.6.1")
        command = run.call_args_list[0].args[0]
        name = command[command.index("--name") + 1]
        self.assertTrue(name.startswith("besu-layertrace-cli-"))
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "-f", name])

    def test_rpc_fee_cap_checks_high_price_workload_before_startup(self):
        from types import SimpleNamespace
        config = self.config()
        config["rpcTxFeeCapEth"] = 1
        with patch("run.shutil.which") as which:
            with self.assertRaisesRegex(RuntimeError, "attack maximum fee"):
                prepare(SimpleNamespace(image="test"), config)
            which.assert_not_called()

    def test_invalid_rpc_fee_cap_is_rejected(self):
        for cap in (0, -1, True, 20.0, "20"):
            with self.subTest(cap=cap):
                config = self.config()
                config["rpcTxFeeCapEth"] = cap
                with self.assertRaisesRegex(RuntimeError, "positive integer"):
                    network_config(config, "test")

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


@unittest.skipUnless(os.environ.get("BESU_TEST_IMAGE"), "Set BESU_TEST_IMAGE to check the actual Docker image")
class BesuCliIntegrationTests(unittest.TestCase):
    def test_generated_options_are_accepted_by_actual_besu(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        image = os.environ["BESU_TEST_IMAGE"]
        params = network_config(config, image)["participants"][0]["el_extra_params"]
        result = validate_besu_cli(image, params, config["besuVersion"])
        self.assertIn("besu/v26.6.1/", result)

    def test_original_20_eth_value_reproduces_actual_parser_error(self):
        with self.assertRaisesRegex(RuntimeError, "exceeds range of unsigned long"):
            validate_besu_cli(os.environ["BESU_TEST_IMAGE"],
                              ["--tx-pool=LAYERED", "--rpc-tx-feecap=20000000000000000000"], "26.6.1")


if __name__ == "__main__":
    unittest.main()
