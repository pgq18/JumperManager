"""Automatic SSH discovery using file edits, without connecting to any device."""

from pathlib import Path
import re
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from support import temp_directory
from jumper_manager.engine import Manager


class ConfigWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = temp_directory()
        self.root = Path(self.temp.__enter__())
        self.config = self.root / "config"
        self.config.write_text("Host alpha\n HostName 10.0.0.1\n", encoding="utf-8")
        self.block = threading.Event()
        self.entered = threading.Event()
        self.patch_run = patch.object(Manager, "_run", self.fake_run)
        self.patch_interval = patch("jumper_manager.engine.CONFIG_POLL_INTERVAL", 0.05)
        self.patch_run.start()
        self.patch_interval.start()
        self.manager = Manager(self.root, str(self.config))

    def tearDown(self):
        self.block.set()
        self.manager.close()
        self.patch_run.stop()
        self.patch_interval.stop()
        self.temp.__exit__(None, None, None)

    def fake_run(self, args, **kwargs):
        source = self.config.read_text(encoding="utf-8")
        if "# slow" in source:
            self.entered.set()
            self.block.wait(3)
        if "BrokenOption" in source:
            return subprocess.CompletedProcess(args, 255, "", "Bad configuration option: BrokenOption")
        alias = args[-1]
        match = re.search(r"^Host " + re.escape(alias) + r"\s*\n(.*?)(?=^Host |\Z)", source, re.M | re.S)
        block = match.group(1) if match else ""
        def option(name, default):
            found = re.search(r"^\s*" + name + r"\s+(\S+)", block, re.M | re.I)
            return found.group(1) if found else default
        output = f"hostname {option('HostName', alias)}\nuser tester\nport {option('Port', '22')}\nproxyjump {option('ProxyJump', 'none')}\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.manager.state()
            if predicate(state):
                return state
            time.sleep(0.025)
        self.fail("Automatic discovery did not reach the expected state: " + repr(self.manager.state()))

    def create_mapping(self):
        return self.manager.create({"name": "watch test", "source_host": "local", "source_port": 54321,
                                    "target_host": "alpha", "target_port": 22, "auto_start": False})

    def test_add_and_remove_devices_without_refresh_call(self):
        self.config.write_text("Host alpha\n HostName 10.0.0.1\nHost beta\n HostName 10.0.0.2\n", encoding="utf-8")
        self.wait_for(lambda s: any(h["id"] == "beta" for h in s["hosts"]))
        self.config.write_text("Host beta\n HostName 10.0.0.2\n", encoding="utf-8")
        state = self.wait_for(lambda s: all(h["id"] != "alpha" for h in s["hosts"]))
        self.assertGreaterEqual(state["ssh_discovery"]["revision"], 3)

    def test_changed_jump_replans_stopped_mapping(self):
        mapping = self.create_mapping()
        self.config.write_text("Host jump\n HostName 10.0.0.9\nHost alpha\n HostName 10.0.0.2\n ProxyJump jump\n Port 2200\n", encoding="utf-8")
        state = self.wait_for(lambda s: next(h for h in s["hosts"] if h["id"] == "alpha")["port"] == 2200)
        saved = next(m for m in state["mappings"] if m["id"] == mapping["id"])
        self.assertEqual([node["host"] for node in saved["route"]], ["local", "jump", "alpha"])
        self.assertEqual(saved["status"], "stopped")

    def test_bad_save_preserves_last_good_hosts_then_recovers(self):
        self.config.write_text("Host broken\n BrokenOption yes\n", encoding="utf-8")
        state = self.wait_for(lambda s: bool(s["ssh_discovery"]["error"]))
        self.assertEqual([h["id"] for h in state["hosts"]], ["local", "alpha"])
        self.config.write_text("Host recovered\n HostName 10.0.0.4\n", encoding="utf-8")
        state = self.wait_for(lambda s: any(h["id"] == "recovered" for h in s["hosts"]))
        self.assertIsNone(state["ssh_discovery"]["error"])

    def test_removed_mapping_endpoint_is_retained_and_restored(self):
        mapping = self.create_mapping()
        self.config.write_text("Host beta\n HostName 10.0.0.2\n", encoding="utf-8")
        self.wait_for(lambda s: s["mappings"][0].get("config_error"))
        self.assertEqual(self.manager.state()["mappings"][0]["id"], mapping["id"])
        self.config.write_text("Host alpha\n HostName 10.0.0.3\n", encoding="utf-8")
        state = self.wait_for(lambda s: s["mappings"][0]["status"] == "stopped")
        self.assertFalse(state["mappings"][0].get("config_error"))

    def test_live_mapping_keeps_its_old_route_until_restart(self):
        mapping = self.create_mapping()
        self.manager._mappings[mapping["id"]]["status"] = "running"
        self.config.write_text("Host jump\n HostName 10.0.0.9\nHost alpha\n HostName 10.0.0.2\n ProxyJump jump\n", encoding="utf-8")
        state = self.wait_for(lambda s: s["mappings"][0].get("config_changed"))
        self.assertEqual([node["host"] for node in state["mappings"][0]["route"]], ["local", "alpha"])
        self.assertEqual(state["mappings"][0]["status"], "running")
        checked = self.manager.check(mapping["id"])
        self.assertEqual(checked["status"], "degraded")
        self.assertIn("未改道", checked["health"]["summary"])

    def test_slow_refresh_does_not_block_state(self):
        self.config.write_text("# slow\nHost beta\n HostName 10.0.0.2\n", encoding="utf-8")
        self.assertTrue(self.entered.wait(2))
        started = time.monotonic()
        state = self.manager.state()
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertTrue(state["ssh_discovery"]["refreshing"])
        self.block.set()
        self.wait_for(lambda s: any(h["id"] == "beta" for h in s["hosts"]))


if __name__ == "__main__":
    unittest.main()
