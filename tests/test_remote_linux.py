"""Python-free remote metadata protocol and real POSIX shell sampling."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import unittest

from support import temp_directory
from jumper_manager import remote_linux as remote


def row(address="127.0.0.1", port=50051, inode=100, state="0A", byteorder="little", peer="127.0.0.2", peer_port=12345):
    def encode(value):
        packed = ipaddress.ip_address(value).packed
        if byteorder == "little":
            packed = b"".join(packed[i:i + 4][::-1] for i in range(0, len(packed), 4))
        return packed.hex().upper()
    if ":" in address and ":" not in peer:
        peer = "::1"
    return f"0: {encode(address)}:{port:04X} {encode(peer)}:{peer_port:04X} {state} 0:0 00:0 0 1000 0 {inode}"


def protocol(rows=(), owners=(), *, mode="target", address="127.0.0.1", visibility="complete", order="little", ipv6=True):
    parts = ["BEGIN 1", f"MODE {mode}", f"ORDER {order}", f"ADDR {address}"]
    for table in ("tcp", "tcp6"):
        parts.append(f"TABLE {table}" if table == "tcp" or ipv6 else "ABSENT tcp6")
        for value in rows:
            if (len(value.split()[1].split(":")[0]) == 32) == (table == "tcp6"):
                parts.append(f"ROW {table} {value}")
    parts += [f"OWNER {pid} {inode}" for pid, inode in owners]
    if mode == "target":
        parts.append(f"VISIBILITY {visibility}")
    parts.append("END 1")
    return "SSH login banner\n" + "\n".join(remote._PREFIX + part for part in parts) + "\n"


class SnapshotProtocolTests(unittest.TestCase):
    def test_complete_empty_tables_with_optional_ipv6(self):
        for ipv6 in (True, False):
            result = remote.parse_target_snapshot(protocol(ipv6=ipv6), "127.0.0.1", 50051)
            self.assertEqual(result["process_count"], 0)
            self.assertFalse(result["listening"])
            self.assertTrue(result["complete"])

    def test_shared_listener_and_multiple_fds_are_deduplicated_by_process(self):
        data = protocol([row(inode=100), row("0.0.0.0", inode=101)], [(10, 100), (10, 100), (10, 101), (11, 100)])
        result = remote.parse_target_snapshot(data, "127.0.0.1", 50051)
        self.assertEqual(result["process_count"], 2)
        self.assertTrue(result["complete"])
        self.assertNotIn("pid", json.dumps(result).lower())

    def test_other_address_and_port_do_not_affect_count(self):
        data = protocol([row(inode=100), row("127.0.0.2", inode=101), row(port=50052, inode=102)], [(10, 100), (11, 101), (12, 102)])
        self.assertEqual(remote.parse_target_snapshot(data, "127.0.0.1", 50051)["process_count"], 1)

    def test_denied_hidden_or_unmatched_namespace_ownership_is_unknown(self):
        for data in (protocol([row()], [], visibility="complete"),
                     protocol([row()], [(10, 100)], visibility="denied"),
                     protocol([row(), row(inode=101)], [(10, 100)])):
            result = remote.parse_target_snapshot(data, "127.0.0.1", 50051)
            self.assertIsNone(result["process_count"])
            self.assertTrue(result["listening"])
            self.assertFalse(result["complete"])

    def test_ipv6_wildcard_ipv4_ambiguity_preserved(self):
        result = remote.parse_target_snapshot(protocol([row("::")], [(10, 100)]), "127.0.0.1", 50051)
        self.assertIsNone(result["process_count"])
        self.assertIsNone(result["listening"])
        self.assertFalse(result["complete"])
        result = remote.parse_target_snapshot(protocol([row("::"), row(inode=101)], [(10, 100), (11, 101)]), "127.0.0.1", 50051)
        self.assertIs(result["listening"], True)
        self.assertIsNone(result["process_count"])

    def test_both_byteorders_and_families(self):
        for order in ("little", "big"):
            for address in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                data = protocol([row(address, byteorder=order)], [(10, 100)], address=address, order=order)
                result = remote.parse_target_snapshot(data, address, 50051)
                self.assertEqual(result["process_count"], 1, (order, address, result))

    def test_connection_snapshot_filters_state_endpoint_and_deduplicates(self):
        rows = [row(state="01"), row(state="01"), row(), row("127.0.0.2", state="01"), row(port=50052, state="01")]
        result = remote.parse_connection_snapshot(protocol(rows, mode="connections"), "127.0.0.1", 50051)
        self.assertEqual(result, {("127.0.0.2", 12345)})
        data = protocol([row("::1", state="01", peer="2001:db8::2", byteorder="big")], mode="connections", address="::1", order="big")
        self.assertEqual(remote.parse_connection_snapshot(data, "::1", 50051), {("2001:db8::2", 12345)})

    def test_remote_hostname_addresses_used_without_local_dns_resolution(self):
        data = protocol([row()], [(10, 100)])
        self.assertEqual(remote.parse_target_snapshot(data, "remote-only.example", 50051)["process_count"], 1)

    def test_truncated_duplicate_malformed_or_explicit_error_never_claim_zero(self):
        valid = protocol()
        samples = ("", valid.replace(remote._PREFIX + "END 1", ""),
                   valid.replace("ORDER little", "ORDER sideways"),
                   valid.replace("TABLE tcp6", "TABLE tcp"),
                   valid.replace("TABLE tcp6", "ERROR tables"),
                   valid.replace("TABLE tcp6", "ERROR ownership"),
                   valid.replace("TABLE tcp6", "ROW tcp corrupt"),
                   valid.replace("VISIBILITY complete", "OWNER 1 nope"),
                   valid.replace("VISIBILITY complete", ""))
        for data in samples:
            with self.subTest(data=data):
                result = remote.parse_target_snapshot(data, "127.0.0.1", 50051)
                self.assertIsNone(result["process_count"])
                self.assertFalse(result["complete"])
        with self.assertRaises(ValueError):
            remote.parse_connection_snapshot("", "127.0.0.1", 50051)

    def test_untrusted_parameters_are_quoted_and_ports_validated(self):
        address = "host';touch${IFS}/tmp/should-not-exist;'"
        script = remote.remote_target_process_script(address, 50051)
        self.assertEqual(script.splitlines()[0], "address=" + shlex.quote(address))
        for port in (True, 0, 65536, "22"):
            with self.assertRaises(ValueError):
                remote.remote_snapshot_script("127.0.0.1", port)
        for address in ("", "a\nb", "-option"):
            with self.assertRaises(ValueError):
                remote.remote_target_process_script(address, 22)
        self.assertNotIn("python", remote.remote_snapshot_script("127.0.0.1", 22).lower())


@unittest.skipUnless(sys.platform.startswith("linux"), "Requires Linux /proc and POSIX sh")
class ShellSamplerTests(unittest.TestCase):
    def run_script(self, script):
        completed = subprocess.run(["/bin/sh", "-s"], input=script, text=True, capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def test_real_listener_and_established_peer_without_python_on_remote_path(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            # Restrict helper PATH to its three basic utilities, excluding Python.
            with temp_directory() as directory:
                tools = Path(directory)
                for name in ("od", "stat", "getent"):
                    (tools / name).symlink_to("/usr/bin/" + name)
                prefix = "PATH=" + shlex.quote(str(tools)) + "\nexport PATH\n"
                output = self.run_script(prefix + remote.remote_target_process_script("127.0.0.1", port))
                result = remote.parse_target_snapshot(output, "127.0.0.1", port)
                self.assertTrue(result["listening"], result)
                if result["complete"]:
                    self.assertEqual(result["process_count"], 1)
                else:
                    self.assertIsNone(result["process_count"])
                with socket.create_connection(("127.0.0.1", port)) as client:
                    accepted, _ = listener.accept()
                    with accepted:
                        output = self.run_script(prefix + remote.remote_snapshot_script("127.0.0.1", port))
                        self.assertEqual(remote.parse_connection_snapshot(output, "127.0.0.1", port), {client.getsockname()})
            output = self.run_script(remote.remote_target_process_script("127.0.0.2", port))
            self.assertEqual(remote.parse_target_snapshot(output, "127.0.0.2", port)["process_count"], 0)

    def test_proc_fixture_counts_shared_owners_and_honors_hidepid(self):
        with temp_directory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            inode = os.fstat(listener.fileno()).st_ino
            root = Path(directory)
            (root / "net").mkdir()
            header = "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            (root / "net/tcp").write_text(header + row(inode=inode, byteorder=sys.byteorder) + "\n")
            (root / "net/tcp6").write_text(header)
            (root / "mounts").write_text("proc /proc proc rw 0 0\n")
            for pid in (10, 11):
                fd = root / str(pid) / "fd"
                fd.mkdir(parents=True)
                (fd / "1").symlink_to(f"/proc/{os.getpid()}/fd/{listener.fileno()}")
                (fd / "2").symlink_to(f"/proc/{os.getpid()}/fd/{listener.fileno()}")
            # An ordinary file can contain socket-looking text in its name.
            # This process owns only that file and must not inflate the count.
            misleading = root / f"ordinary file\nsocket:[{inode}]"
            misleading.write_text("ordinary file content")
            with misleading.open() as stream:
                fd = root / "12/fd"
                fd.mkdir(parents=True)
                (fd / "1").symlink_to(f"/proc/{os.getpid()}/fd/{stream.fileno()}")
                script = remote.remote_target_process_script("127.0.0.1", 50051).replace("proc_root=/proc", "proc_root=" + shlex.quote(str(root)))
                result = remote.parse_target_snapshot(self.run_script(script), "127.0.0.1", 50051)
                self.assertEqual(result["process_count"], 2, result)
            (fd / "1").unlink()
            (root / "mounts").write_text("proc /proc proc rw,hidepid=2 0 0\n")
            result = remote.parse_target_snapshot(self.run_script(script), "127.0.0.1", 50051)
            self.assertIsNone(result["process_count"])
            self.assertTrue(result["listening"])


if __name__ == "__main__":
    unittest.main()
