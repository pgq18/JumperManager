"""Focused route, validation and lifecycle regression tests (no real SSH needed)."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import MagicMock, patch

from support import temp_directory
from jumper_manager.engine import Manager, address, forward_spec, port
from jumper_manager.processes import terminate_owned
from jumper_manager.ssh_config import discover_aliases, parse_effective_config, route_for, simple_proxy


def fake_refresh(manager):
    manager._hosts = [{"id": alias, "alias": alias, "label": alias, "hostname": alias,
                       "user": "tester", "port": 22, "route": route}
                      for alias, route in [("local", ["local"]), ("server-a", ["server-a"]),
                                           ("server-b", ["jump", "server-b"]), ("jump", ["jump"])]]
    manager._effective = {}
    return manager._hosts


def payload(**changes):
    result = {"name": "测试映射", "source_host": "server-a", "source_port": 51051,
              "target_host": "server-b", "target_port": 50051, "bind_address": "127.0.0.1",
              "target_address": "127.0.0.1", "auto_start": False}
    result.update(changes)
    return result


class ConfigTests(unittest.TestCase):
    def test_include_wildcard_cycle_and_alias_filter(self):
        with temp_directory() as directory:
            root = Path(directory)
            ssh = root / ".ssh"
            ssh.mkdir()
            (ssh / "parts").mkdir()
            (ssh / "config").write_text("Host primary *.internal !excluded\nInclude parts/*.conf\n", encoding="utf-8")
            (ssh / "parts" / "a.conf").write_text('Host "server-b" jump\nInclude config\nHost server-a\n', encoding="utf-8")
            with patch("pathlib.Path.home", return_value=root):
                self.assertEqual(discover_aliases(ssh / "config"), ["primary", "server-b", "jump", "server-a"])

    def test_effective_config_repeated_directives(self):
        parsed = parse_effective_config("hostname 10.0.0.1\nuser alice\nidentityfile ~/.ssh/a\nidentityfile ~/.ssh/b\n")
        self.assertEqual(parsed["identityfile"], ["~/.ssh/a", "~/.ssh/b"])

    def test_proxycommand_and_proxyjump_routes(self):
        configs = {"target": {"proxycommand": ["ssh -W %h:%p jump"]}, "jump": {"proxyjump": ["alice@entry:2222"]}, "entry": {}}
        route, warnings = route_for("target", configs.__getitem__)
        self.assertEqual(route, ["entry", "jump", "target"])
        self.assertEqual(warnings, [])

    def test_custom_proxy_uncertainty_and_cycles(self):
        route, warnings = route_for("target", lambda _: {"proxycommand": ["custom-proxy %h %p"]})
        self.assertEqual(route, ["target"])
        self.assertIn("无法完整解析", warnings[0])
        _, warnings = route_for("a", lambda _: {"proxyjump": ["a"]})
        self.assertIn("循环", warnings[0])

    def test_proxy_parser_does_not_accept_shell_pipeline(self):
        self.assertIsNone(simple_proxy("ssh -W %h:%p jump | another-command"))
        self.assertEqual(simple_proxy("ssh -p 2222 -W %h:%p alice@jump")["alias"], "jump")


class ValidationTests(unittest.TestCase):
    def test_ports_reject_bool_float_out_of_range(self):
        for value in [True, False, 1.5, "1.5", "22 -R x", 0, 65536]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                port(value)
        self.assertEqual(port("2222"), 2222)

    def test_addresses_and_ipv6_forward(self):
        self.assertEqual(address("localhost", bind=True), "127.0.0.1")
        self.assertEqual(forward_spec("::1", 12, "::1", 34), "[::1]:12:[::1]:34")
        for value in ["0.0.0.0", "192.168.1.1", "example.com"]:
            with self.assertRaises(ValueError):
                address(value, bind=True)
        for value in ["x;shutdown", "-oProxyCommand=id", "host:22", "a\nb"]:
            with self.assertRaises(ValueError):
                address(value)


class EngineTests(unittest.TestCase):
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

    def test_remote_route_explicitly_passes_local_pc(self):
        plan = self.manager.preview(payload())
        self.assertEqual([node["host"] for node in plan["route"]], ["server-a", "local", "jump", "server-b"])
        self.assertEqual(len(plan["steps"]), 2)
        self.assertIn("应用协议", " ".join(plan["warnings"]))

    def test_reject_self_loop_unknown_alias_and_truthy_autostart(self):
        for data in [payload(source_host="missing"), payload(auto_start="yes"),
                     payload(source_host="server-b", source_port=50051)]:
            with self.assertRaises(ValueError):
                self.manager.create(data)

    def test_persist_defaults_and_reload_stopped(self):
        mapping = self.manager.create(payload())
        self.assertEqual(mapping["status"], "stopped")
        saved = json.loads((self.root / "data" / "mappings.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["mappings"][0]["auto_start"])
        self.assertNotIn("logs", saved["mappings"][0])
        self.manager.close()
        self.manager = Manager(self.root, str(self.root / "config"))
        self.assertEqual(self.manager.state()["mappings"][0]["id"], mapping["id"])
        self.assertEqual(self.manager.state()["mappings"][0]["status"], "stopped")

    def test_conflicting_source_fails_without_spawning(self):
        mapping = self.manager.create(payload())
        with patch.object(self.manager, "_endpoint_check", side_effect=[{"ok": True}, {"ok": False, "message": "Address already in use"}]), patch.object(self.manager, "_spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "监听端口"):
                self.manager.start(mapping["id"])
            spawn.assert_not_called()
        self.assertEqual(self.manager.state()["mappings"][0]["status"], "error")

    def test_partial_remote_start_cleans_first_leg(self):
        mapping = self.manager.create(payload())
        process = MagicMock()
        process.poll.return_value = None
        entry = {"process": process, "job": MagicMock(), "record": {"pid": 123, "marker": "owned.log"},
                 "log": self.root / "owned.log", "offset": 0, "alias": "server-b", "kind": "-L"}
        def spawn(mapping_id, alias, kind, spec):
            if kind == "-R":
                raise RuntimeError("remote port forwarding failed")
            self.manager._processes[mapping_id] = [entry]
            return entry
        with patch.object(self.manager, "_endpoint_check", return_value={"ok": True}), patch.object(self.manager, "_spawn", side_effect=spawn), patch.object(self.manager, "_wait_ready"), patch("jumper_manager.engine.terminate_owned", return_value=True) as terminate:
            with self.assertRaisesRegex(RuntimeError, "remote port"):
                self.manager.start(mapping["id"])
            terminate.assert_called_once_with(entry["record"], process, entry["job"])
        self.assertFalse(self.manager._processes)
        self.assertNotIn("relay_port", self.manager.state()["mappings"][0])

    def test_closing_during_probe_prevents_spawn(self):
        mapping = self.manager.create(payload())
        def probe(*args):
            self.manager._closed.set()
            return {"ok": True}
        with patch.object(self.manager, "_endpoint_check", side_effect=probe), patch.object(self.manager, "_spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "关闭"):
                self.manager.start(mapping["id"])
            spawn.assert_not_called()
        self.manager._closed.clear()

    def test_false_positive_listener_not_counted_as_target_health(self):
        mapping = self.manager.create(payload())
        self.manager._mappings[mapping["id"]]["status"] = "running"
        process = MagicMock()
        process.poll.return_value = None
        self.manager._processes[mapping["id"]] = [{"process": process, "alias": "server-a", "log": self.root / "absent", "offset": 0}]
        with patch.object(self.manager, "_endpoint_check", side_effect=[{"ok": False, "message": "refused"}, {"ok": True}]):
            result = self.manager.check(mapping["id"])
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["health"]["ok"])
        self.manager._processes.clear()

    def test_local_relay_roundtrip_and_stop(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.3)
        target_port = listener.getsockname()[1]
        finished = threading.Event()
        def echo():
            while not finished.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with client:
                    client.settimeout(2)
                    try:
                        data = client.recv(1024)
                        if data:
                            client.sendall(data)
                    except OSError:
                        pass
        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            source_port = available.getsockname()[1]
        mapping = self.manager.create(payload(source_host="local", target_host="local", source_port=source_port, target_port=target_port))
        try:
            result = self.manager.start(mapping["id"])
            self.assertEqual(result["status"], "running")
            with socket.create_connection(("127.0.0.1", source_port), timeout=3) as connection:
                connection.sendall(b"jumper-byte-for-byte")
                self.assertEqual(connection.recv(100), b"jumper-byte-for-byte")
            with self.assertRaises(ValueError):
                self.manager.update(mapping["id"], payload())
            self.manager.stop(mapping["id"])
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", source_port), timeout=0.5)
        finally:
            finished.set()
            listener.close()
            thread.join(timeout=2)

    def test_pid_reuse_never_terminates_unowned_process(self):
        record = {"pid": 123, "identity": {"created": "OLD", "image": "ssh.exe"}, "marker": "our-unique-log"}
        with patch("jumper_manager.processes.identity", return_value={"created": "NEW", "image": "ssh.exe", "command": "ssh our-unique-log"}), patch("jumper_manager.processes.subprocess.run") as run:
            self.assertFalse(terminate_owned(record))
            run.assert_not_called()

    def test_stopped_manager_rejects_create(self):
        self.manager.close()
        with self.assertRaisesRegex(RuntimeError, "关闭"):
            self.manager.create(payload())


if __name__ == "__main__":
    unittest.main()
