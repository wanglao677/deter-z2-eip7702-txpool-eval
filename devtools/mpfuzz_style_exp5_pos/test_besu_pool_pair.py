import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit_phased_tx_final_status as audit
import besu_pool_pair as pair
import mp_exp5_7702_phased_pos as phased


class PairTests(unittest.TestCase):
    def test_generated_enclave_name_is_kurtosis_compatible(self):
        run_id = pair.re.sub(r"[^A-Za-z0-9-]", "-", "20260912_031919")
        enclave = f"bp-default-{run_id}-b"
        self.assertRegex(enclave, r"^[-A-Za-z0-9]{1,60}$")
        self.assertNotIn("_", enclave)

    def test_profiles_generate_valid_paired_workloads(self):
        for profile in pair.PROFILES:
            plan = pair.make_plan(profile, "seed01", "3.5", "10")
            plans = []
            for mode in ("baseline", "attack"):
                command = pair.runner_command(plan, mode, "http://127.0.0.1:1", "test", Path("out"), Path("helper"))
                with patch.object(sys, "argv", command[2:]):
                    args = phased.parse_args()
                phased.validate_args(args)
                rows = phased.build_normal_workload_plan(args, phased.phase_plan(args))
                plans.append([(r["globalTick"], r["normalCount"]) for r in rows])
                self.assertEqual(sum(r["normalCount"] for r in rows), plan["normalTotal"])
                if mode == "attack":
                    self.assertEqual(args.attack_first_price, 3_500_000_000)
                    self.assertEqual(args.attacker_price, 10_000_000_000)
            self.assertEqual(plans[0], plans[1])

    def test_default_has_no_sequenced_overrides(self):
        config = pair.network_config(pair.make_plan("default", "seed01"))
        self.assertNotIn("el_extra_params", config["participants"][0])

    def test_all_intermediate_blocks_have_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pair.write_csv(root / "timeseries.csv", [dict(blockNumber=13, observationStartBlock=11,
                                                          phase="attack_on", phaseTick=1, globalTick=3)])
            mapping = audit.read_block_phase_index(root)
            self.assertEqual(set(mapping), {11, 12, 13})
            self.assertEqual(mapping[12]["includedPhase"], "attack_on")

    def fixture(self, root):
        head = {"hash": "0xanchor", "number": "0x14"}
        pair.save_json(root / "chain_identity.json", {"chainId": 123, "anchor": head})
        pair.write_csv(root / "timeseries.csv", [dict(blockNumber=11, observationStartBlock=11,
                                                       phase="warmup", phaseTick=1, globalTick=1),
                                                 dict(blockNumber=12, observationStartBlock=12,
                                                      phase="attack_on", phaseTick=1, globalTick=2)])
        normal = [{"sender": "0xnormal", "nonce": i, "hash": f"0xn{i}", "gasPriceWei": "3",
                   "phase": "warmup" if i == 0 else "attack_on", "globalTick": i + 1} for i in range(2)]
        attack = [{"sender": "0xattacker", "nonce": i, "hash": f"0xa{i}", "gasPriceWei": "7" if i == 0 else "10",
                   "phase": "attack_on", "globalTick": 2} for i in range(2)]
        for group, records in (("normal", normal), ("attack", attack)):
            (root / f"{group}_records.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        receipt = {"transactionHash": "0xn0", "status": "0x1", "blockNumber": "0xb",
                   "blockHash": "0xbhash", "gasUsed": "0x5208", "effectiveGasPrice": "0x3"}
        content = {"pending": {}, "queued": {"0xattacker": {"1": {"hash": "0xa1", "from": "0xattacker",
                                                                            "nonce": "0x1", "gasPrice": "0xa"}}}}

        def rpc(url, method, params=None):
            if method == "eth_chainId":
                return "0x7b"
            if method == "eth_getBlockByNumber":
                if params[0] == "0xb":
                    return {"number": "0xb", "hash": "0xbhash"}
                return head
            if method == "txpool_content":
                return content
            if method == "eth_getTransactionCount":
                return "0x0" if params[0] == "0xattacker" else "0x1"
            if method == "eth_getBalance":
                return "0xde0b6b3a7640000"
            if method == "eth_getCode":
                return "0xef0100abcd" if params[0] == "0xattacker" else "0x"
            raise AssertionError(method)

        def batch(url, method, params):
            return [receipt if p[0] == "0xn0" else None for p in params]

        return rpc, batch

    def test_zero_included_attack_is_a_valid_result_with_nonce_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc, batch = self.fixture(root)
            with patch.object(audit, "rpc_call", side_effect=rpc), patch.object(pair, "rpc_batch", side_effect=batch):
                summary = pair.collect_evidence(root, "http://127.0.0.1:1")
            self.assertEqual(summary["byGroup"]["attack"]["included"], 0)
            self.assertEqual(summary["byGroup"]["attack"]["feeWei"], 0)
            self.assertEqual(summary["byRole"]["first"]["states"], {"DROPPED_OR_EVICTED": 1})
            self.assertEqual(summary["byRole"]["tail"]["states"], {"QUEUED_FINAL": 1})
            self.assertEqual(summary["byGroup"]["normal"]["includedByPhase"], {"warmup": 1})
            self.assertEqual(summary["sendersWithNonceGap"], 1)
            blocks = pair.read_csv(root / "block_stats.csv")
            self.assertEqual(blocks[1]["normalIncluded"], "0")

    def test_anchor_mismatch_stops_before_receipt_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            with patch.object(audit, "rpc_call", side_effect=["0x7b", {"hash": "0xwrong"}]), \
                    patch.object(pair, "rpc_batch") as batch:
                with self.assertRaisesRegex(RuntimeError, "锚点"):
                    pair.collect_evidence(root, "http://127.0.0.1:1")
                batch.assert_not_called()

    def test_batch_response_order_and_errors(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps(self.values).encode()

        response = Response()
        response.values = [{"id": 1, "result": None}, {"id": 0, "result": {"status": "0x1"}}]
        with patch.object(pair.urllib.request, "urlopen", return_value=response):
            self.assertEqual(pair.rpc_batch("http://127.0.0.1:1", "test", [[], []]), [{"status": "0x1"}, None])
            response.values[1] = {"id": 0, "error": {"message": "failed"}}
            with self.assertRaises(RuntimeError):
                pair.rpc_batch("http://127.0.0.1:1", "test", [[], []])

    def test_failed_send_with_hash_is_not_eviction(self):
        row = audit.classify_record("unused", "normal", {"hash": "0xtx", "error": "rejected"},
                                    {}, {}, True, 0, 0, {"0xtx": None})
        self.assertEqual(row["finalState"], "REJECTED_OR_SEND_ERROR")

    def test_failed_receipt_counts_towards_cost(self):
        row = audit.classify_record("unused", "attack", {"hash": "0xtx"}, {}, {}, True, 0, 0,
                                    {"0xtx": {"status": "0x0", "blockNumber": "0xa", "gasUsed": "0x5208",
                                              "effectiveGasPrice": "0x7"}})
        self.assertEqual(row["finalState"], "INCLUDED_FAILED")
        self.assertEqual(row["gasUsed"] * row["effectiveGasPrice"], 147000)

    def test_unstable_pool_snapshot_does_not_claim_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc, batch = self.fixture(root)
            height = 20

            def changing_rpc(url, method, params=None):
                nonlocal height
                if method == "eth_getBlockByNumber" and params[0] == "latest":
                    height += 1
                    return {"number": hex(height), "hash": f"0xhead{height}"}
                return rpc(url, method, params)

            with patch.object(audit, "rpc_call", side_effect=changing_rpc), patch.object(pair, "rpc_batch", side_effect=batch):
                summary = pair.collect_evidence(root, "http://127.0.0.1:1")
            self.assertFalse(summary["stableHead"])
            self.assertEqual(summary["byGroup"]["attack"]["states"], {"UNKNOWN_UNSTABLE_AUDIT": 2})

    def test_wrong_delegation_stops_workload(self):
        from types import SimpleNamespace
        args = SimpleNamespace(normal_fund_eth=1, attack_balance_eth=1)
        rpc = unittest.mock.Mock()
        rpc.call.side_effect = [{"pending": "0x0", "queued": "0x0"}, "0x"]
        with tempfile.TemporaryDirectory() as tmp, patch.object(phased.base, "balance", return_value=10**18):
            with self.assertRaisesRegex(RuntimeError, "delegation"):
                phased.verify_setup(rpc, args, [], [{"address": "0xsender"}], [], "0xdelegate", Path(tmp))

    def test_rpc_discovery_ignores_lighthouse_and_engine_ports(self):
        from types import SimpleNamespace
        response = SimpleNamespace(stdout="cl http: 4000/tcp -> http://127.0.0.1:12345\n"
                                    "engine-rpc: 8551/tcp -> 127.0.0.1:12346\n"
                                    "rpc: 8545/tcp -> 127.0.0.1:12347\n")
        with patch.object(pair.subprocess, "run", return_value=response):
            self.assertEqual(pair.discover_rpc("test"), "http://127.0.0.1:12347")


if __name__ == "__main__":
    unittest.main()
