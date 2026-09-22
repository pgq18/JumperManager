"""Engine contracts for target listener process counts, independent of clients."""
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

from support import temp_directory
from test_engine import fake_refresh, payload
from jumper_manager.engine import Manager


class TargetUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = temp_directory()
        self.root = Path(self.directory.__enter__())
        self.refresh = patch.object(Manager, "refresh_hosts", fake_refresh)
        self.refresh.start()
        self.manager = Manager(self.root, str(self.root / "config"))

    def tearDown(self):
        self.manager.close()
        self.refresh.stop()
        self.directory.__exit__(None, None, None)

    def test_partial_or_invalid_counts_never_become_authoritative_zero_or_one(self):
        mapping = self.manager.create(payload())
        cases = [
            ({"process_count": 1, "listening": True, "complete": False, "message": "invisible owners"}, True),
            ({"process_count": True, "listening": True, "complete": True}, None),
            ({"process_count": 0, "listening": True, "complete": True}, None),
            ({"process_count": None, "listening": None, "complete": False}, None),
            ({"process_count": 1, "listening": False, "complete": True}, None),
        ]
        for value, expected_listening in cases:
            with self.subTest(value=value), patch.object(self.manager, "_target_process_snapshot", return_value=value):
                result = self.manager._sample_target_usage(mapping)
            self.assertIsNone(result["process_count"])
            self.assertFalse(result["complete"])
            self.assertIs(result["listening"], expected_listening)
            self.assertTrue(result["checked_at"])

    def test_remote_marker_is_parsed_and_missing_helper_is_unknown(self):
        mapping = self.manager.create(payload())
        output = 'JM_TARGET_PROCESSES:{"process_count":2,"listening":true,"complete":true,"message":"2 listeners"}'
        with patch.object(self.manager, "_run", return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
            result = self.manager._sample_target_usage(mapping)
        self.assertEqual(result["process_count"], 2)
        self.assertTrue(result["complete"])
        self.assertEqual(run.call_args.args[0][-2:], ["server-b", "python3 -"])
        with patch.object(self.manager, "_run", return_value=subprocess.CompletedProcess([], 127, "", "python3 missing")):
            result = self.manager._sample_target_usage(mapping)
        self.assertIsNone(result["process_count"])
        self.assertIsNone(result["listening"])
        self.assertFalse(result["complete"])

    def test_sampling_exception_does_not_fail_or_remove_a_ready_tunnel(self):
        with socket.socket() as target, socket.socket() as source:
            target.bind(("127.0.0.1", 0))  # Intentionally no target listener.
            source.bind(("127.0.0.1", 0))
            source_port = source.getsockname()[1]
            source.close()
            mapping = self.manager.create(payload(source_host="local", target_host="local",
                                                  source_port=source_port, target_port=target.getsockname()[1]))
            self.assertIsNone(mapping["target_usage"])
            with patch.object(self.manager, "_target_process_snapshot", side_effect=PermissionError("owner information unavailable")):
                result = self.manager.start(mapping["id"])
            self.assertEqual(result["status"], "running")
            self.assertTrue(result["health"]["tunnel_ok"])
            self.assertFalse(result["health"]["target_ok"])
            self.assertIsNone(result["target_usage"]["process_count"])
            self.assertIn("owner information unavailable", result["target_usage"]["message"])
            self.assertIn(mapping["id"], self.manager._relays)
            self.assertNotIn("failure_stage", result)
            self.assertIsNone(self.manager.stop(mapping["id"])["target_usage"])
            self.assertIsNone(self.manager.update(mapping["id"], payload())["target_usage"])

    def test_target_probe_unexpected_error_is_diagnostic_not_start_failure(self):
        with socket.socket() as target, socket.socket() as source:
            target.bind(("127.0.0.1", 0))
            target.listen(4)
            source.bind(("127.0.0.1", 0))
            source_port = source.getsockname()[1]
            source.close()
            target_port = target.getsockname()[1]
            mapping = self.manager.create(payload(source_host="local", target_host="local",
                                                  source_port=source_port, target_port=target_port))
            original = self.manager._endpoint_check
            def probe(host, operation, address, port):
                if operation == "connect" and port == target_port:
                    raise TypeError("malformed target diagnostic")
                return original(host, operation, address, port)
            with patch.object(self.manager, "_endpoint_check", side_effect=probe):
                result = self.manager.start(mapping["id"])
            self.assertEqual(result["status"], "running")
            self.assertIsNone(result["health"]["target_ok"])
            self.assertIsNone(result["error"])
            self.assertEqual(result["target_usage"]["process_count"], 1)
            self.assertEqual(result["usage"]["active_connections"], 0)


if __name__ == "__main__":
    unittest.main()
