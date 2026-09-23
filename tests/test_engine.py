"""Focused route, validation and lifecycle regression tests (no real SSH needed)."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

from support import temp_directory
from jumper_manager.engine import Manager, address, forward_spec, port
from jumper_manager.connections import local_connections
from jumper_manager.target_processes import local_target_processes
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
        # These lifecycle tests never launch diagnostics on real SSH aliases.
        self.snapshot = patch.object(self.manager, "_connection_snapshot", side_effect=lambda host, address, port:
                                     local_connections(address, port) if host == "local" else set())
        self.snapshot.start()
        self.target_snapshot = patch.object(self.manager, "_target_process_snapshot", side_effect=lambda host, address, port:
                                            local_target_processes(address, port) if host == "local" else
                                            {"process_count": 0, "listening": False, "complete": True, "message": "没有进程在用"})
        self.target_snapshot.start()

    def tearDown(self):
        self.manager.close()
        self.snapshot.stop()
        self.target_snapshot.stop()
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
        with patch.object(self.manager, "_endpoint_check", return_value={"ok": False, "message": "Address already in use"}) as probe, patch.object(self.manager, "_spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "监听端口"):
                self.manager.start(mapping["id"])
            spawn.assert_not_called()
            probe.assert_called_once_with("server-a", "available", "127.0.0.1", 51051)
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
        self.assertEqual(result["status"], "running")
        self.assertFalse(result["health"]["ok"])
        self.assertTrue(result["health"]["tunnel_ok"])
        self.assertFalse(result["health"]["target_ok"])
        self.assertIsNone(result["error"])
        self.manager._processes.clear()

    def test_failed_source_listener_is_unavailable_even_when_target_responds(self):
        mapping = self.manager.create(payload())
        self.manager._get(mapping["id"])["status"] = "running"
        process = MagicMock()
        process.poll.return_value = None
        entry = {"process": process, "alias": "server-a", "log": self.root / "absent", "offset": 0}
        self.manager._processes[mapping["id"]] = [entry]
        try:
            with patch.object(self.manager, "_endpoint_check", side_effect=[{"ok": True}, {"ok": False, "message": "refused"}]):
                result = self.manager.check(mapping["id"])
            self.assertEqual(result["status"], "degraded")
            self.assertTrue(result["health"]["target_ok"])
            self.assertFalse(result["health"]["tunnel_ok"])
            self.assertEqual(result["error"], "不可用：连接检查失败。")
            self.assertEqual(self.manager._processes[mapping["id"]], [entry])
        finally:
            self.manager._processes.clear()

    def test_target_starts_after_tunnel_without_restart_and_process_count_updates(self):
        # Reserve the target address without listening: connect must fail until
        # the service below starts, while no other test can steal its port.
        target_listener = socket.socket()
        target_listener.bind(("127.0.0.1", 0))
        target_port = target_listener.getsockname()[1]
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            source_port = reservation.getsockname()[1]
        mapping = self.manager.create(payload(source_host="local", source_port=source_port,
                                              target_host="local", target_port=target_port))
        finished = threading.Event()
        thread = None
        try:
            result = self.manager.start(mapping["id"])
            self.assertEqual(result["status"], "running")
            self.assertNotIn("failure_stage", result)
            self.assertTrue(result["health"]["tunnel_ok"])
            self.assertFalse(result["health"]["target_ok"])
            self.assertFalse(result["health"]["ok"])
            self.assertIsNone(result["error"])
            self.assertEqual(result["target_usage"]["process_count"], 0)
            self.assertFalse(result["target_usage"]["listening"])
            relay = self.manager._relays[mapping["id"]]
            self.assertFalse(relay.closed.is_set())

            target_listener.listen(8)
            target_listener.settimeout(0.2)

            def echo():
                while not finished.is_set():
                    try:
                        client, _ = target_listener.accept()
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
            # Starting the target makes the existing tunnel usable immediately;
            # a manual check only refreshes its process/TCP diagnostics.
            healthy = self.manager.check(mapping["id"])
            self.assertIs(self.manager._relays[mapping["id"]], relay)
            # Linux may expose the listener but not every process holding its
            # socket (other UIDs, inherited descriptors, PID namespaces).
            self.assertTrue(healthy["target_usage"]["listening"])
            if healthy["target_usage"]["complete"]:
                self.assertEqual(healthy["target_usage"]["process_count"], 1)
            else:
                self.assertIsNone(healthy["target_usage"]["process_count"])
            self.assertNotIn("failure_stage", healthy)
            with socket.create_connection(("127.0.0.1", source_port), timeout=3) as client:
                client.sendall(b"service-started-later")
                self.assertEqual(client.recv(100), b"service-started-later")
            self.assertEqual(healthy["status"], "running")
            self.assertTrue(healthy["health"]["tunnel_ok"])
            self.assertTrue(healthy["health"]["target_ok"])
            self.assertTrue(healthy["health"]["ok"])
            self.assertEqual(healthy["health"]["summary"], "隧道已建立；连接检查通过。")
            self.assertIsNone(healthy["error"])
        finally:
            self.manager.stop(mapping["id"])
            finished.set()
            target_listener.close()
            if thread:
                thread.join(timeout=3)

    def test_remote_target_down_up_keeps_both_ready_ssh_legs(self):
        mapping = self.manager.create(payload())
        entries = []
        target_state = {"ready": True, "failure": None}

        def spawn(mapping_id, alias, kind, spec):
            process = MagicMock()
            process.poll.return_value = None
            entry = {"process": process, "job": MagicMock(),
                     "record": {"pid": 100 + len(entries), "marker": "test-only"},
                     "log": self.root / f"missing-{kind}", "offset": 0, "alias": alias, "kind": kind}
            entries.append(entry)
            self.manager._processes[mapping_id] = list(entries)
            return entry

        def probe(host, operation, address, port):
            if host == "server-b" and operation == "connect":
                self.assertEqual(len(entries), 2, "Target health is checked only after both SSH legs exist")
                if target_state["failure"]:
                    raise target_state["failure"]
                return {"ok": target_state["ready"], "message": "Connection refused"}
            return {"ok": True}

        with patch.object(self.manager, "_spawn", side_effect=spawn) as spawned, \
             patch.object(self.manager, "_wait_ready") as ready, \
             patch.object(self.manager, "_endpoint_check", side_effect=probe), \
             patch("jumper_manager.engine.terminate_owned", return_value=True) as terminate:
            try:
                result = self.manager.start(mapping["id"])
                self.assertEqual(result["status"], "running")
                self.assertTrue(result["health"]["tunnel_ok"])
                self.assertTrue(result["health"]["target_ok"])
                self.assertIsNone(result["error"])
                self.assertEqual(ready.call_count, 2)
                self.assertEqual([entry["kind"] for entry in entries], ["-L", "-R"])
                for reachable in (True, False, True):
                    target_state["ready"] = reachable
                    checked = self.manager.check(mapping["id"])
                    self.assertEqual(checked["status"], "running")
                    self.assertTrue(checked["health"]["tunnel_ok"])
                    self.assertEqual(checked["health"]["target_ok"], reachable)
                    self.assertIsNone(checked["error"])
                    self.assertEqual(self.manager._processes[mapping["id"]], entries)
                target_state["failure"] = OSError("Could not launch the diagnostic SSH process")
                checked = self.manager.check(mapping["id"])
                self.assertEqual(checked["status"], "running")
                self.assertTrue(checked["health"]["tunnel_ok"])
                self.assertIsNone(checked["health"]["target_ok"])
                self.assertFalse(checked["health"]["ok"])
                self.assertIn("未确认", checked["health"]["summary"])
                self.assertIsNone(checked["error"])
                self.assertIn("diagnostic SSH", " ".join(checked["health"]["details"]))
                self.assertEqual(spawned.call_count, 2)
                terminate.assert_not_called()
            finally:
                self.manager.stop(mapping["id"])
            self.assertEqual(terminate.call_count, 2)

    def test_target_offline_or_unchecked_keeps_both_remote_legs_running(self):
        for target in ({"ok": False, "message": "Connection refused"},
                       {"ok": False, "checked": False, "message": "Diagnostic SSH failed"}):
            with self.subTest(target=target):
                mapping = self.manager.create(payload())
                entries = []
                def spawn(mapping_id, alias, kind, spec):
                    process = MagicMock()
                    process.poll.return_value = None
                    entry = {"process": process, "job": MagicMock(), "record": {"pid": 100 + len(entries)},
                             "log": self.root / "missing", "offset": 0, "alias": alias, "kind": kind}
                    entries.append(entry)
                    self.manager._processes[mapping_id] = list(entries)
                    return entry
                def probe(host, operation, address, port):
                    if host == "server-b" and operation == "connect":
                        return target
                    return {"ok": True, "probe_peer": ["127.0.0.1", 40234]}
                with patch.object(self.manager, "_endpoint_check", side_effect=probe), \
                     patch.object(self.manager, "_spawn", side_effect=spawn), \
                     patch.object(self.manager, "_wait_ready"), \
                     patch("jumper_manager.engine.terminate_owned", return_value=True) as terminate:
                    result = self.manager.start(mapping["id"])
                    terminate.assert_not_called()
                    self.assertEqual(result["status"], "running")
                    self.assertTrue(result["health"]["tunnel_ok"])
                    self.assertFalse(result["health"]["ok"])
                    self.assertNotIn("failure_stage", result)
                    self.assertIn("relay_port", result)
                    self.assertEqual(self.manager._processes[mapping["id"]], entries)
                    self.manager.stop(mapping["id"])
                    self.assertEqual(terminate.call_count, 2)


    def test_failed_start_cleanup_keeps_owned_records_and_reports_incomplete_cleanup(self):
        mapping = self.manager.create(payload(source_host="local"))
        process = MagicMock()
        process.poll.return_value = None
        entry = {"process": process, "job": MagicMock(), "record": {"mapping_id": mapping["id"], "pid": 101},
                 "log": self.root / "missing", "offset": 0, "alias": "server-b", "kind": "-L"}
        def spawn(*args):
            self.manager._processes[mapping["id"]] = [entry]
            return entry
        with patch.object(self.manager, "_endpoint_check", return_value={"ok": True}), \
             patch.object(self.manager, "_spawn", side_effect=spawn), \
             patch.object(self.manager, "_wait_ready"), \
             patch.object(self.manager, "check", side_effect=RuntimeError("diagnostic failed")), \
             patch("jumper_manager.engine.terminate_owned", side_effect=OSError("access denied")):
            with self.assertRaisesRegex(RuntimeError, "清理未完成"):
                self.manager.start(mapping["id"])
        result = self.manager._get(mapping["id"])
        self.assertEqual(result["failure_stage"], "start")
        self.assertEqual(self.manager._processes[mapping["id"]], [entry])
        self.assertEqual(json.loads((self.manager.data / "runtime.json").read_text())["processes"], [entry["record"]])
        self.assertIn("清理未完成", result["logs"][-1]["message"])
        self.assertNotIn("已清理本次创建", result["logs"][-1]["message"])
        with patch("jumper_manager.engine.terminate_owned", return_value=True):
            stopped = self.manager.stop(mapping["id"])
        self.assertNotIn("failure_stage", stopped)
        self.assertNotIn(mapping["id"], self.manager._processes)

    def test_start_retries_owned_cleanup_before_building_new_resources(self):
        mapping = self.manager.create(payload(source_host="local"))
        live = self.manager._get(mapping["id"])
        live.update(status="error", failure_stage="start")
        process = MagicMock()
        process.poll.return_value = None
        old = {"process": process, "job": MagicMock(), "record": {"pid": 101},
               "log": self.root / "missing", "offset": 0, "alias": "server-b", "kind": "-L"}
        self.manager._processes[mapping["id"]] = [old]
        with patch("jumper_manager.engine.terminate_owned", side_effect=OSError("still denied")), \
             patch.object(self.manager, "_endpoint_check") as probe, patch.object(self.manager, "_spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "清理未完成"):
                self.manager.start(mapping["id"])
            spawn.assert_not_called()
            probe.assert_not_called()
        self.assertEqual(self.manager._processes[mapping["id"]], [old])
        events = []
        def terminate(*args):
            events.append("clean")
            return True
        new = {**old, "record": {"pid": 102}}
        def spawn(*args):
            self.assertNotIn(mapping["id"], self.manager._processes)
            events.append("spawn")
            self.manager._processes[mapping["id"]] = [new]
            return new
        with patch("jumper_manager.engine.terminate_owned", side_effect=terminate), \
             patch.object(self.manager, "_endpoint_check", return_value={"ok": True}), \
             patch.object(self.manager, "_spawn", side_effect=spawn), patch.object(self.manager, "_wait_ready"):
            result = self.manager.start(mapping["id"])
            self.assertEqual(events, ["clean", "spawn"])
            self.assertEqual(result["status"], "running")
            self.assertNotIn("failure_stage", result)
            self.assertEqual(self.manager._processes[mapping["id"]], [new])
            self.manager.stop(mapping["id"])

    def test_explicit_start_of_unavailable_existing_mapping_does_not_return_success(self):
        mapping = self.manager.create(payload())
        live = self.manager._get(mapping["id"])
        live["status"] = "degraded"
        process = MagicMock()
        process.poll.return_value = None
        entry = {"process": process, "job": MagicMock(), "record": {"pid": 101},
                 "log": self.root / "missing", "offset": 0, "alias": "server-a", "kind": "-R"}
        self.manager._processes[mapping["id"]] = [entry]
        with patch.object(self.manager, "_endpoint_check", side_effect=[{"ok": True}, {"ok": False, "message": "refused"}]), \
             patch("jumper_manager.engine.terminate_owned", return_value=True) as terminate, \
             patch.object(self.manager, "_spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "连接检查失败"):
                self.manager.start(mapping["id"])
            terminate.assert_called_once()
            spawn.assert_not_called()
        self.assertEqual(live["status"], "error")
        self.assertEqual(live["failure_stage"], "start")
        self.assertNotIn(mapping["id"], self.manager._processes)

    def test_invalid_start_configuration_is_marked_as_start_failure(self):
        mapping = self.manager.create(payload())
        live = self.manager._get(mapping["id"])
        live["target_host"] = "removed-host"
        with self.assertRaises(RuntimeError):
            self.manager.start(mapping["id"])
        self.assertEqual(live["status"], "error")
        self.assertEqual(live["failure_stage"], "start")

    def test_remote_probe_distinguishes_refused_connection_from_failed_diagnostic(self):
        for result in [
                {"ok": False, "checked": True, "message": "Connection refused"},
                {"ok": False, "checked": False, "message": "SSH forwarding prohibited"}]:
            with patch("jumper_manager.engine.ssh_tcp_check", return_value=result) as probe:
                value = self.manager._endpoint_check("server-b", "connect", "127.0.0.1", 50051)
            self.assertEqual(value, result)
            self.assertEqual(probe.call_args.args[1:], ("server-b", "127.0.0.1", 50051))

    def test_ready_failure_still_cleans_every_started_ssh_leg(self):
        mapping = self.manager.create(payload())
        entries = []

        def spawn(mapping_id, alias, kind, spec):
            process = MagicMock()
            process.poll.return_value = None
            entry = {"process": process, "job": MagicMock(), "record": {"pid": 100 + len(entries)},
                     "log": self.root / "missing", "offset": 0, "alias": alias, "kind": kind}
            entries.append(entry)
            self.manager._processes[mapping_id] = list(entries)
            return entry

        with patch.object(self.manager, "_endpoint_check", return_value={"ok": True}), \
             patch.object(self.manager, "_spawn", side_effect=spawn), \
             patch.object(self.manager, "_wait_ready", side_effect=[None, RuntimeError("remote port forwarding failed")]), \
             patch("jumper_manager.engine.terminate_owned", return_value=True) as terminate:
            with self.assertRaisesRegex(RuntimeError, "remote port forwarding failed"):
                self.manager.start(mapping["id"])
            self.assertEqual(terminate.call_count, 2)
        self.assertFalse(self.manager._processes)
        self.assertEqual(self.manager._get(mapping["id"])["status"], "error")
        self.assertNotIn("relay_port", self.manager._get(mapping["id"]))

    def test_unexpected_start_diagnostic_error_removes_listener_and_marks_start_failure(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            source_port = reservation.getsockname()[1]
        mapping = self.manager.create(payload(source_host="local", source_port=source_port,
                                              target_host="local", target_port=source_port + 1 if source_port < 65535 else source_port - 1))
        with patch.object(self.manager, "check", side_effect=TypeError("invalid diagnostic response")):
            with self.assertRaisesRegex(RuntimeError, "invalid diagnostic response"):
                self.manager.start(mapping["id"])
        result = self.manager._get(mapping["id"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_stage"], "start")
        self.assertNotIn(mapping["id"], self.manager._relays)
        self.assertNotIn(mapping["id"], self.manager._probe_peers)
        with socket.socket() as available:
            available.bind(("127.0.0.1", source_port))
            available.listen(1)
        self.assertNotIn("failure_stage", self.manager.stop(mapping["id"]))

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

    def test_uncertain_cleanup_retains_record_even_if_leader_already_exited(self):
        mapping = self.manager.create(payload())
        process = MagicMock()
        process.poll.return_value = 0
        entry = {"process": process, "job": MagicMock(), "record": {"pid": 101},
                 "alias": "server-a", "log": self.root / "missing", "offset": 0}
        self.manager._processes[mapping["id"]] = [entry]
        with patch("jumper_manager.engine.terminate_owned", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "无法停止"):
                self.manager._cleanup(mapping["id"])
        self.assertEqual(self.manager._processes[mapping["id"]], [entry])
        self.assertEqual(json.loads((self.manager.data / "runtime.json").read_text())["processes"], [entry["record"]])
        with patch("jumper_manager.engine.terminate_owned", return_value=True):
            self.manager._cleanup(mapping["id"])

    def test_recovery_failure_keeps_ownership_and_continues_other_records(self):
        records = [{"pid": 101}, {"pid": 102}]
        runtime = self.manager.data / "runtime.json"
        runtime.write_text(json.dumps({"processes": records}))
        with patch("jumper_manager.engine.terminate_owned", side_effect=[OSError("permission denied"), True]) as terminate:
            self.manager._recover()
        self.assertEqual(terminate.call_count, 2)
        self.assertEqual(json.loads(runtime.read_text())["processes"], records[:1])
        self.assertEqual(self.manager._unrecovered_records, records[:1])

    def test_malformed_recovery_file_is_not_overwritten_as_empty(self):
        runtime = self.manager.data / "runtime.json"
        runtime.write_text("invalid JSON that needs manual inspection")
        with patch("jumper_manager.engine.terminate_owned") as terminate:
            self.manager._recover()
            terminate.assert_not_called()
        self.assertEqual(runtime.read_text(), "invalid JSON that needs manual inspection")

    def test_stopped_manager_rejects_create(self):
        self.manager.close()
        with self.assertRaisesRegex(RuntimeError, "关闭"):
            self.manager.create(payload())


if __name__ == "__main__":
    unittest.main()
