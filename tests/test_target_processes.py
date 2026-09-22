"""Listener ownership, visibility limits, and genuine local process counts."""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

from support import temp_directory
from jumper_manager import target_processes as target


HEADER = "  sl  local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"


def proc_row(address, port=50051, inode=100, state="0A", byteorder="little"):
    packed = ipaddress.ip_address(address).packed
    if byteorder == "little":
        packed = b"".join(packed[index:index + 4][::-1] for index in range(0, len(packed), 4))
    return f" 0: {packed.hex()}:{port:04X} {'0' * (len(packed) * 2)}:0000 {state} 0:0 00:0 0 1000 0 {inode}\n"


def windows_table(family, rows):
    row_type = target.Tcp4Row if family == socket.AF_INET else target.Tcp6Row
    encoded = []
    for address, port, pid, state in rows:
        row = row_type()
        row.state = state
        row.local_port = socket.htons(port)
        row.pid = pid
        packed = ipaddress.ip_address(address).packed
        if family == socket.AF_INET:
            row.local_address = int.from_bytes(packed, "little")
        else:
            row.local_address[:] = packed
        encoded.append(bytes(row))
    return len(rows).to_bytes(4, "little") + b"".join(encoded)


class ListenerParsingTests(unittest.TestCase):
    def test_proc_only_listeners_exact_port_and_wildcard_mapped_addresses(self):
        wanted = {ipaddress.ip_address("127.0.0.1")}
        table = HEADER + proc_row("127.0.0.1", inode=10) + proc_row("0.0.0.0", inode=11)
        table += proc_row("::ffff:127.0.0.1", inode=12)
        table += proc_row("127.0.0.2", inode=13) + proc_row("127.0.0.1", 50052, 14)
        table += proc_row("127.0.0.1", inode=15, state="01")
        self.assertEqual(target.parse_proc_listeners(table, wanted, 50051, "little"), ({10, 11, 12}, False))

    def test_proc_ipv6_wildcard_is_not_assumed_to_accept_ipv4(self):
        table = HEADER + proc_row("::", inode=20)
        self.assertEqual(target.parse_proc_listeners(table, {ipaddress.ip_address("127.0.0.1")}, 50051, "little"), (set(), True))
        self.assertEqual(target.parse_proc_listeners(table, {ipaddress.ip_address("::1")}, 50051, "little"), ({20}, False))

    def test_proc_big_endian_and_corrupt_tables(self):
        wanted = {ipaddress.ip_address("::1")}
        self.assertEqual(target.parse_proc_listeners(HEADER + proc_row("::1", byteorder="big"), wanted, 50051, "big"), ({100}, False))
        for table in ("", "invalid\n", HEADER + "truncated\n", HEADER + proc_row("::1", inode=0)):
            with self.subTest(table=table), self.assertRaises(ValueError):
                target.parse_proc_listeners(table, wanted, 50051, "little")

    def test_windows_deduplicates_pid_across_multiple_listener_rows_and_families(self):
        v4 = windows_table(socket.AF_INET, [
            ("127.0.0.1", 50051, 100, 2), ("0.0.0.0", 50051, 100, 2),
            ("127.0.0.1", 50051, 999, 5), ("127.0.0.2", 50051, 101, 2),
            ("127.0.0.1", 50052, 102, 2)])
        v6 = windows_table(socket.AF_INET6, [("::ffff:127.0.0.1", 50051, 100, 2),
                                               ("::ffff:127.0.0.1", 50051, 103, 2)])
        with patch.object(target, "_windows_table", side_effect=[v4, v6]):
            result = target._windows_target_processes("127.0.0.1", 50051)
        self.assertEqual(result["process_count"], 2)
        self.assertTrue(result["complete"])
        self.assertTrue(result["listening"])
        self.assertEqual(set(result), {"process_count", "listening", "complete", "message"})

    def test_windows_missing_owner_wildcard_ambiguity_and_truncation(self):
        empty = windows_table(socket.AF_INET, [])
        scenarios = [
            (windows_table(socket.AF_INET, [("127.0.0.1", 50051, 0, 2)]), empty, True),
            (empty, windows_table(socket.AF_INET6, [("::", 50051, 100, 2)]), None),
        ]
        for v4, v6, listening in scenarios:
            with self.subTest(listening=listening), patch.object(target, "_windows_table", side_effect=[v4, v6]):
                result = target._windows_target_processes("127.0.0.1", 50051)
            self.assertIsNone(result["process_count"])
            self.assertFalse(result["complete"])
            self.assertIs(result["listening"], listening)
        table = windows_table(socket.AF_INET, [("127.0.0.1", 50051, 100, 2)])
        with self.assertRaises(ValueError):
            target.parse_windows_listeners(table[:-1], socket.AF_INET, {ipaddress.ip_address("127.0.0.1")}, 50051)


class LinuxOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(temp_directory()))
        (self.root / "net").mkdir()
        (self.root / "net" / "tcp").write_text(HEADER, encoding="ascii")
        (self.root / "net" / "tcp6").write_text(HEADER, encoding="ascii")
        (self.root / "mounts").write_text("proc /proc proc rw,nosuid,nodev,relatime 0 0\n", encoding="utf-8")
        self.links = {}

    def listener(self, address="127.0.0.1", inode=100):
        table = self.root / "net" / ("tcp6" if ":" in address else "tcp")
        table.write_text(table.read_text(encoding="ascii") + proc_row(address, inode=inode, byteorder=sys.byteorder), encoding="ascii")

    def owner(self, pid, inodes):
        fd = self.root / str(pid) / "fd"
        fd.mkdir(parents=True)
        for index, inode in enumerate(inodes):
            descriptor = fd / str(index)
            descriptor.touch()
            self.links[str(descriptor)] = f"socket:[{inode}]"

    def snapshot(self, address="127.0.0.1"):
        with patch.object(target.os, "readlink", side_effect=lambda path: self.links[str(path)]):
            return target._linux_target_processes(address, 50051, self.root)

    def test_multiple_fds_and_listeners_deduplicate_process_but_shared_socket_counts_all_owners(self):
        self.listener(inode=100)
        self.listener("0.0.0.0", 101)
        self.owner(10, [100, 100, 101])
        self.owner(11, [100])
        self.owner(12, [999])
        result = self.snapshot()
        self.assertEqual(result["process_count"], 2)
        self.assertTrue(result["complete"])
        self.assertNotIn("pid", json.dumps(result).lower())

    def test_visible_listener_outside_pid_namespace_is_unknown_not_zero(self):
        self.listener()
        self.owner(10, [999])
        result = self.snapshot()
        self.assertIsNone(result["process_count"])
        self.assertTrue(result["listening"])
        self.assertFalse(result["complete"])

    def test_partially_visible_owners_or_hidden_pids_are_not_a_complete_count(self):
        self.listener()
        self.owner(10, [100])
        (self.root / "mounts").write_text("proc /proc proc rw,hidepid=2 0 0\n", encoding="utf-8")
        result = self.snapshot()
        self.assertIsNone(result["process_count"])
        self.assertFalse(result["complete"])
        (self.root / "mounts").write_text("proc /proc proc rw 0 0\n", encoding="utf-8")
        self.owner(11, [999])
        original = Path.iterdir
        def partial(path):
            if path == self.root / "11" / "fd":
                raise PermissionError("test denied")
            return original(path)
        with patch.object(Path, "iterdir", partial):
            result = self.snapshot()
        self.assertIsNone(result["process_count"])
        self.assertTrue(result["listening"])

    def test_complete_empty_tables_return_zero_without_inspecting_processes(self):
        with patch.object(Path, "iterdir", side_effect=AssertionError("no owners to enumerate")):
            result = self.snapshot()
        self.assertEqual(result["process_count"], 0)
        self.assertFalse(result["listening"])
        self.assertTrue(result["complete"])

    def test_remote_helper_uses_identical_linux_logic_and_only_aggregate_output(self):
        self.listener()
        self.owner(10, [100])
        namespace = {"__name__": "__test__"}
        # Route only the helper's /proc root to a fixture; no real PID inspection.
        actual_path = Path
        def fixture_path(value):
            return self.root if value == "/proc" else actual_path(value)
        output = io.StringIO()
        script = target.remote_target_process_script("127.0.0.1", 50051)
        ast.parse(script, feature_version=(3, 9))
        with patch("pathlib.Path", fixture_path), patch("sys.platform", "linux"), \
             patch.object(target.os, "readlink", side_effect=lambda path: self.links[str(path)]), redirect_stdout(output):
            exec(script, namespace)
        result = json.loads(output.getvalue().removeprefix("JM_TARGET_PROCESSES:"))
        self.assertEqual(result["process_count"], 1)
        self.assertTrue(result["complete"])
        self.assertEqual(set(result), {"process_count", "listening", "complete", "message"})


class PublicSamplerTests(unittest.TestCase):
    def test_unsupported_or_read_errors_are_unknown(self):
        with patch.object(target.os, "name", "posix"), patch.object(target.sys, "platform", "darwin"):
            result = target.local_target_processes("127.0.0.1", 50051)
        self.assertIsNone(result["process_count"])
        self.assertFalse(result["complete"])
        with patch.object(target.os, "name", "nt"), patch.object(target, "_windows_table", side_effect=PermissionError()):
            result = target.local_target_processes("127.0.0.1", 50051)
        self.assertIsNone(result["process_count"])
        output = io.StringIO()
        with patch("sys.platform", "linux"), patch.object(Path, "read_text", side_effect=PermissionError()), redirect_stdout(output):
            exec(target.remote_target_process_script("127.0.0.1", 50051), {})
        result = json.loads(output.getvalue().removeprefix("JM_TARGET_PROCESSES:"))
        self.assertIsNone(result["process_count"])
        self.assertFalse(result["complete"])

    @unittest.skipUnless(os.name == "nt" or sys.platform.startswith("linux"), "supported local sampler")
    def test_real_listener_counts_one_process_with_zero_or_multiple_clients(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            port = listener.getsockname()[1]
            result = target.local_target_processes("127.0.0.1", port)
            if not result["complete"] and os.name != "nt":
                self.skipTest("Current Linux process permissions cannot establish a complete count")
            self.assertEqual(result["process_count"], 1, result)
            with socket.create_connection(("127.0.0.1", port), timeout=2) as first, \
                 socket.create_connection(("127.0.0.1", port), timeout=2) as second:
                accepted_one, _ = listener.accept()
                accepted_two, _ = listener.accept()
                with accepted_one, accepted_two:
                    self.assertEqual(target.local_target_processes("127.0.0.1", port)["process_count"], 1)
                    listener.close()
                    result = target.local_target_processes("127.0.0.1", port)
                    self.assertEqual(result["process_count"], 0, result)
                    self.assertFalse(result["listening"])
                    self.assertTrue(result["complete"])


if __name__ == "__main__":
    unittest.main()
