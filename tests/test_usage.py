"""Real loopback snapshots plus portable table/remote-helper regressions."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, mock_open, patch

from support import temp_directory
from test_engine import fake_refresh, payload
from jumper_manager import connections
from jumper_manager.engine import Manager, local_socket_check


def proc_line(local, local_port, remote, remote_port, state="01"):
    def encoded(host):
        data = ipaddress.ip_address(host).packed
        return b"".join(data[i:i + 4][::-1] for i in range(0, len(data), 4)).hex().upper()
    return f" 0: {encoded(local)}:{local_port:04X} {encoded(remote)}:{remote_port:04X} {state} 0:0\n"


class ConnectionTableTests(unittest.TestCase):
    def test_linux_ipv4_and_ipv6_exact_endpoint_and_established_only(self):
        for host in ("127.0.0.1", "::1"):
            table = "header\n" + proc_line(host, 12345, host, 43210)
            table += proc_line(host, 12345, host, 43211, "08")  # CLOSE_WAIT
            table += proc_line(host, 12345, host, 43212, "06")  # TIME_WAIT
            table += proc_line(host, 12346, host, 43213)
            table += proc_line("127.0.0.2" if ":" not in host else "::2", 12345, host, 43214)
            expected = {(host, 43210)}
            self.assertEqual(connections.parse_proc_rows(table, host, 12345, byteorder="little"), expected)
            output = io.StringIO()
            with patch("sys.platform", "linux"), patch("sys.byteorder", "little"), \
                 patch("builtins.open", mock_open(read_data=table)) as opened, redirect_stdout(output):
                exec(connections.remote_snapshot_script(host, 12345), {})
            actual = json.loads(output.getvalue().split("JM_CONNECTIONS:")[1])
            self.assertTrue(actual["ok"])
            self.assertEqual({tuple(peer) for peer in actual["peers"]}, expected)
            opened.assert_called_once_with("/proc/net/tcp6" if ":" in host else "/proc/net/tcp", encoding="ascii")

    def test_remote_unreadable_or_unsupported_is_not_an_empty_success(self):
        for platform, problem in (("linux", PermissionError("permission denied")), ("darwin", None)):
            with self.subTest(platform=platform):
                output = io.StringIO()
                with patch("sys.platform", platform), patch("builtins.open", side_effect=problem), redirect_stdout(output):
                    exec(connections.remote_snapshot_script("127.0.0.1", 12345), {})
                result = json.loads(output.getvalue().split("JM_CONNECTIONS:")[1])
                self.assertFalse(result["ok"])
                self.assertNotIn("peers", result)

    def test_windows_ipv4_ipv6_structures_filter_state_and_port(self):
        for host, row_type in (("127.0.0.1", connections.Tcp4Row), ("::1", connections.Tcp6Row)):
            rows = []
            for state, port in ((5, 12345), (8, 12345), (11, 12345), (5, 12346)):
                row = row_type()
                row.state = state
                row.local_port = socket.htons(port)
                row.remote_port = socket.htons(43210)
                packed = ipaddress.ip_address(host).packed
                if row_type is connections.Tcp4Row:
                    row.local_address = row.remote_address = int.from_bytes(packed, "little")
                else:
                    row.local_address[:] = row.remote_address[:] = packed
                rows.append(bytes(row))
            table = len(rows).to_bytes(4, "little") + b"".join(rows)
            self.assertEqual(connections.parse_windows_table(table, host, 12345), {(host, 43210)})
            with self.assertRaises(ValueError):
                connections.parse_windows_table(table[:-1], host, 12345)

    @unittest.skipUnless(os.name == "nt" or sys.platform.startswith("linux"), "supported local sampler")
    def test_real_listener_counts_established_clients_not_listening_or_closed(self):
        for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
            with self.subTest(host=host), socket.socket(family) as listener:
                listener.bind((host, 0))
                listener.listen(4)
                port = listener.getsockname()[1]
                self.assertEqual(connections.local_connections(host, port), set())
                with socket.create_connection((host, port), timeout=2) as client:
                    incoming, _ = listener.accept()
                    with incoming:
                        peer = connections.peer_key(*client.getsockname()[:2])
                        self.assertEqual(connections.local_connections(host, port), {peer})
                self.assertEqual(connections.local_connections(host, port), set())


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = temp_directory()
        self.root = Path(self.directory.__enter__())
        self.refresh = patch.object(Manager, "refresh_hosts", fake_refresh)
        self.refresh.start()
        self.manager = Manager(self.root, str(self.root / "config"))

    def tearDown(self):
        # All SSH entries below are test doubles, never OS process handles.
        self.manager._processes.clear()
        self.manager.close()
        self.refresh.stop()
        self.directory.__exit__(None, None, None)

    def mapping(self, **changes):
        return self.manager.create(payload(**changes))

    def test_startup_probe_is_excluded_and_expired_identity_can_be_reused(self):
        mapping = self.mapping(source_host="local")
        process = MagicMock()
        process.poll.return_value = None
        entry = {"kind": "-L", "process": process, "log": self.root / "missing", "offset": 0}
        with patch("jumper_manager.engine.local_socket_check", return_value={"ok": True, "probe_peer": ["127.0.0.1", 40001]}):
            self.manager._wait_ready(mapping["id"], entry, "127.0.0.1", mapping["source_port"])
        own, user = ("127.0.0.1", 40001), ("127.0.0.1", 40002)
        with patch.object(self.manager, "_connection_snapshot", side_effect=[{own, user}, set(), {own}]):
            self.assertEqual(self.manager._sample_usage(mapping["id"], mapping)["active_connections"], 1)
            self.assertEqual(self.manager._sample_usage(mapping["id"], mapping)["active_connections"], 0)
            self.assertEqual(self.manager._sample_usage(mapping["id"], mapping)["active_connections"], 1)

    def test_probe_exclusion_expires_even_if_no_snapshot_observed_disconnect(self):
        mapping = self.mapping(source_host="local")
        peer = ("127.0.0.1", 40001)
        with patch("jumper_manager.engine.time.monotonic", return_value=100):
            self.manager._remember_probe(mapping["id"], {"probe_peer": list(peer)})
        with patch.object(self.manager, "_connection_snapshot", return_value={peer}):
            with patch("jumper_manager.engine.time.monotonic", return_value=105):
                self.assertEqual(self.manager._sample_usage(mapping["id"], mapping)["active_connections"], 0)
            # No intermediate empty snapshot: this endpoint now belongs to a
            # later client and must not remain excluded for its whole lifetime.
            with patch("jumper_manager.engine.time.monotonic", return_value=135):
                self.assertEqual(self.manager._sample_usage(mapping["id"], mapping)["active_connections"], 1)

    def test_check_samples_before_active_probes_and_does_not_conflate_target_with_usage(self):
        mapping = self.mapping()
        live = self.manager._get(mapping["id"])
        live["status"] = "running"
        process = MagicMock()
        process.poll.return_value = None
        self.manager._processes[mapping["id"]] = [{"process": process, "alias": "server-a", "log": self.root / "missing", "offset": 0}]
        actions = []
        def snapshot(*args):
            actions.append("snapshot")
            return {("127.0.0.1", 40002)}
        def probe(host, operation, address, port):
            actions.append(host)
            return {"ok": host == "server-a", "message": "refused", "probe_peer": ["127.0.0.1", 40003]}
        with patch.object(self.manager, "_connection_snapshot", side_effect=snapshot), \
             patch.object(self.manager, "_endpoint_check", side_effect=probe):
            result = self.manager.check(mapping["id"])
        self.assertEqual(actions, ["snapshot", "server-b", "server-a"])
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["health"]["target_ok"])
        self.assertEqual(result["error"], "不可用：目标设备的 127.0.0.1:50051 无法连接。")
        self.assertEqual(result["usage"]["active_connections"], 1)
        self.assertTrue(result["usage"]["in_use"])
        self.assertIn(("127.0.0.1", 40003), self.manager._probe_peers[mapping["id"]])
        self.assertNotIn("probe_peer", result)

    def test_failed_sampling_is_unknown_and_remote_output_must_be_valid(self):
        mapping = self.mapping()
        for output, stderr, code in [('', 'python3 missing', 127),
                                     ('JM_CONNECTIONS:{"ok":false,"message":"permission denied"}', '', 0),
                                     ('JM_CONNECTIONS:{"ok":true,"peers":[["127.0.0.1",true]]}', '', 0)]:
            with patch.object(self.manager, "_run", return_value=subprocess.CompletedProcess([], code, output, stderr)):
                result = self.manager._sample_usage(mapping["id"], mapping)
            self.assertIsNone(result["active_connections"])
            self.assertIsNone(result["in_use"])
            self.assertTrue(result["checked_at"])
            self.assertIn("无法获取", result["message"])

    def test_local_relay_usage_zero_one_zero_without_counting_health_checks(self):
        with socket.socket() as target, socket.socket() as reservation:
            target.bind(("127.0.0.1", 0))
            target.listen(32)
            reservation.bind(("127.0.0.1", 0))
            source_port = reservation.getsockname()[1]
            reservation.close()
            mapping = self.mapping(source_host="local", target_host="local", source_port=source_port,
                                   target_port=target.getsockname()[1])
            initial = self.manager.start(mapping["id"])
            self.assertEqual(initial["usage"]["active_connections"], 0)
            relay = self.manager._relays[mapping["id"]]
            with socket.create_connection(("127.0.0.1", source_port), timeout=2):
                busy = self.manager.check(mapping["id"])
                self.assertEqual(busy["usage"]["active_connections"], 1)
                self.assertTrue(busy["usage"]["in_use"])
            idle = self.manager.check(mapping["id"])
            self.assertEqual(idle["usage"]["active_connections"], 0)
            self.assertFalse(idle["usage"]["in_use"])
            self.assertIs(self.manager._relays[mapping["id"]], relay)
            self.assertIsNone(self.manager.stop(mapping["id"])["usage"])


if __name__ == "__main__":
    unittest.main()
