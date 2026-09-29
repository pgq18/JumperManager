"""Per-mapping wait persistence, jump policy isolation and deadline regressions."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from support import temp_directory
from test_engine import fake_refresh, payload
from jumper_manager.engine import Manager, safe_options
from jumper_manager.ssh_config import parse_effective_config, simple_proxy


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = temp_directory()
        self.root = Path(self.temp.__enter__())
        self.refresh = patch.object(Manager, "refresh_hosts", fake_refresh)
        self.refresh.start()
        self.manager = Manager(self.root, str(self.root / "config"))

    def tearDown(self):
        self.manager.close()
        self.refresh.stop()
        self.temp.__exit__(None, None, None)

    def test_default_validation_persistence_and_old_client_edit(self):
        first = self.manager.create(payload())
        second = self.manager.create(payload(name="slow", ssh_timeout=120))
        self.assertEqual(first["ssh_timeout"], 30)
        for value in [None, True, False, "120", 4, 601, 30.0, [], {}]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SSH.*5.*600"):
                self.manager.create(payload(ssh_timeout=value))
        for value in [5, 600]:
            self.assertEqual(self.manager.preview(payload(ssh_timeout=value))["route"][0]["host"], "server-a")
        updated = self.manager.update(second["id"], payload(name="edited by older client"))
        self.assertEqual(updated["ssh_timeout"], 120)
        saved = json.loads((self.manager.data / "mappings.json").read_text(encoding="utf-8"))
        self.assertEqual([item["ssh_timeout"] for item in saved["mappings"]], [30, 120])
        # Simulate upgrading an older configuration that has no timeout field.
        del saved["mappings"][0]["ssh_timeout"]
        (self.manager.data / "mappings.json").write_text(json.dumps(saved), encoding="utf-8")
        self.manager.close()
        self.manager = Manager(self.root, str(self.root / "config"))
        self.assertEqual([item["ssh_timeout"] for item in self.manager.state()["mappings"]], [30, 120])

    def test_concurrent_timeout_policies_do_not_overwrite_each_other(self):
        self.manager._effective = {"server-b": {"proxycommand": ["ssh -o ConnectTimeout=2 -W %h:%p jump"]}}
        with ThreadPoolExecutor(max_workers=2) as executor:
            short, long = list(executor.map(lambda wait: self.manager._ssh_args("server-b", ssh_timeout=wait), [5, 120]))
        short_path, long_path = Path(short[2]), Path(long[2])
        self.assertNotEqual(short_path, long_path)
        original = short_path.read_bytes()
        long_content = long_path.read_text(encoding="utf-8")
        self.assertIn("ConnectTimeout 120", long_content)
        self.assertIn("ConnectTimeout=120", long_content)
        self.assertIn(str(long_path), long_content)
        self.assertLess(long_content.index("ConnectTimeout=120"), long_content.index("ConnectTimeout=2"))
        self.manager._ssh_args("server-b", ssh_timeout=600)
        self.assertEqual(short_path.read_bytes(), original)
        alive_count = int(next(option.split("=")[1] for option in safe_options(120) if option.startswith("ServerAliveCountMax=")))
        self.assertGreater(15 * alive_count, 120)

    @unittest.skipIf(os.name == "nt", "Windows OpenSSH rejects sandbox-inherited ACLs; real SSH integration covers alternate configs on Linux")
    def test_effective_nested_proxy_policies_override_original_ssh_defaults(self):
        alternate = self.root / "alternate config"
        alternate.write_text("Host hop\n HostName 127.0.0.1\n ProxyJump final-hop\nHost final-hop\n HostName 127.0.0.1\nHost *\n ConnectTimeout 2\n", encoding="utf-8")
        proxy = subprocess.list2cmdline([self.manager.ssh, "-F", str(alternate), "-W", "%h:%p", "hop"])
        self.manager.config_path.write_text("Host server-b\n HostName 127.0.0.1\n ProxyCommand " + proxy + "\n", encoding="utf-8")
        self.manager._effective = {"server-b": {"proxycommand": [proxy]}}
        args = self.manager._ssh_args("server-b", ssh_timeout=120)
        result = self.manager._run([*args, "-G", "server-b"], timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = parse_effective_config(result.stdout)
        self.assertEqual(config["connecttimeout"], ["120"])
        nested = simple_proxy(config["proxycommand"][0])
        self.assertIsNotNone(nested)
        options = nested["options"]
        nested_path = options[options.index("-F") + 1]
        self.assertNotEqual(Path(nested_path), alternate)
        for alias in ("hop", "final-hop"):
            child = self.manager._run([self.manager.ssh, "-F", nested_path, "-G", alias], timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            resolved = parse_effective_config(child.stdout)
            self.assertEqual(resolved["connecttimeout"], ["120"])
        self.assertIn("ProxyJump final-hop", alternate.read_text(encoding="utf-8"))

    def test_all_connection_diagnostics_use_mapping_timeout(self):
        mapping = self.manager.create(payload(ssh_timeout=120))
        with patch.object(self.manager, "_connection_snapshot", return_value=set()) as connections, \
             patch.object(self.manager, "_target_process_snapshot", return_value={"complete": True, "process_count": 0, "listening": False}) as target:
            self.manager._sample_usage(mapping["id"], mapping)
            self.manager._sample_target_usage(mapping)
        self.assertEqual(connections.call_args.kwargs["ssh_timeout"], 120)
        self.assertEqual(target.call_args.kwargs["ssh_timeout"], 120)
        with patch.object(self.manager, "_run", return_value=subprocess.CompletedProcess([], 0, "sample", "")) as run, \
             patch("jumper_manager.engine.parse_connection_snapshot", return_value=set()), \
             patch("jumper_manager.engine.parse_target_snapshot", return_value={}):
            self.manager._connection_snapshot("server-a", "127.0.0.1", 80, ssh_timeout=120)
            self.manager._target_process_snapshot("server-b", "127.0.0.1", 80, ssh_timeout=120)
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [130, 130])
        self.assertTrue(all("ssh_runtime_120.conf" in call.args[0][2] for call in run.call_args_list))
        with patch("jumper_manager.engine.ssh_tcp_check", return_value={"ok": True}) as probe:
            self.manager._endpoint_check("server-b", "connect", "127.0.0.1", 80, ssh_timeout=120)
        self.assertEqual(probe.call_args.kwargs["timeout"], 120)
        self.assertIs(probe.call_args.kwargs["cancel_event"], self.manager._closed)

    def test_readiness_uses_entry_wait_and_times_out_cleanly(self):
        process = MagicMock()
        process.poll.return_value = None
        log = self.root / "ssh.log"
        log.write_text("", encoding="utf-8")
        entry = {"process": process, "kind": "-R", "log": log, "offset": 0, "ssh_timeout": 120}
        with patch("jumper_manager.engine.time.monotonic", side_effect=[0, 31]), \
             patch.object(self.manager, "_read_process_log"):
            log.write_text("remote forward success for: test", encoding="utf-8")
            self.manager._wait_ready("test", entry)
        entry["ssh_timeout"] = 5
        with patch("jumper_manager.engine.time.monotonic", side_effect=[0, 5]), \
             self.assertRaisesRegex(RuntimeError, "5 秒"):
            self.manager._wait_ready("test", entry)

    def test_shutdown_interrupts_long_diagnostic_and_cleans_process(self):
        process = MagicMock()
        process.poll.return_value = 0
        process.communicate.return_value = ("", "")
        self.manager._closed.set()
        try:
            with patch("jumper_manager.engine.subprocess.Popen", return_value=process), \
                 patch("jumper_manager.engine.os.killpg", create=True), \
                 patch("jumper_manager.engine.WindowsJob") as job, \
                 self.assertRaisesRegex(RuntimeError, "关闭"):
                self.manager._run(["ssh"], timeout=600)
            self.assertTrue(job.return_value.close.called)
        finally:
            self.manager._closed.clear()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-group regression")
    def test_exited_parent_with_live_jump_child_cannot_hold_diagnostic_open(self):
        pid_file = self.root / "jump-child.pid"
        child_code = "import time; time.sleep(30)"
        parent_code = ("import subprocess, sys; from pathlib import Path; "
                       f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                       f"Path({str(pid_file)!r}).write_text(str(child.pid))")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "SSH.*超过"):
            self.manager._run([sys.executable, "-c", parent_code], timeout=1)
        self.assertLess(time.monotonic() - started, 5, "An exited parent must not leave communicate waiting for its jump child")
        child_pid = int(pid_file.read_text())
        # A killed orphan may briefly remain a zombie until init reaps it.
        stat = Path(f"/proc/{child_pid}/stat")
        try:
            child_stat = stat.read_text()
        except FileNotFoundError:
            return
        self.assertIn(child_stat.rsplit(")", 1)[1].split()[0], {"Z", "X"})


if __name__ == "__main__":
    unittest.main()
