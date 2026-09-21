"""Windows-compatible SSH log ownership and inherited stderr regressions.

No SSH processes are started. A duplicated file descriptor models the child
holding stderr open, so readers exercise the actual platform's sharing rules.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

from support import temp_directory
from jumper_manager.engine import Manager


class ProcessLoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = temp_directory()
        self.root = Path(self.directory.__enter__())
        self.manager = Manager.__new__(Manager)
        self.manager._closed = threading.Event()
        self.manager._lock = threading.RLock()
        self.manager._effective = {}
        self.manager._processes = {}
        self.manager.logs_dir = self.root
        self.manager._ssh_args = MagicMock(return_value=["ssh", "-T"])
        self.manager._save_runtime = MagicMock()
        self.manager._log = MagicMock()
        self.child_handles = []
        self.mapping_id = "f" * 32

    def tearDown(self):
        for descriptor in self.child_handles:
            os.close(descriptor)
        self.directory.__exit__(None, None, None)

    def process_factory(self, captured):
        def create(args, **kwargs):
            captured.update(args=args, **kwargs)
            stderr = kwargs["stderr"]
            # Unlike OpenSSH -E, the Python-created inherited handle permits a
            # second reader while the child continues to write to this file.
            child_descriptor = os.dup(stderr.fileno())
            self.child_handles.append(child_descriptor)
            os.write(child_descriptor, b"debug1: remote forward success for: listen 127.0.0.1:55271, connect 127.0.0.1:7897\n")
            process = MagicMock()
            process.pid = 43210
            process.poll.return_value = None
            captured["process"] = process
            return process
        return create

    def test_running_child_log_is_readable_and_readiness_marker_is_detected(self):
        captured = {}
        with patch("jumper_manager.engine.subprocess.Popen", side_effect=self.process_factory(captured)), \
             patch("jumper_manager.engine.WindowsJob"), \
             patch("jumper_manager.engine.identity", return_value={"created": "test", "image": "ssh.exe"}):
            entry = self.manager._spawn(self.mapping_id, "test-device", "-R", "127.0.0.1:55271:127.0.0.1:7897")
        self.assertNotIn("-E", captured["args"], "OpenSSH must not reopen the log with its own sharing policy")
        self.assertIn("-N", captured["args"], "An ownership marker must never cause a remote session")
        marker = entry["record"]["marker"]
        self.assertIn(marker, subprocess.list2cmdline(captured["args"]), "Crash recovery still needs a unique command-line ownership marker")
        self.assertTrue(any(argument == f"SetEnv=JUMPER_MANAGER_ID={marker}" for argument in captured["args"]))
        self.assertTrue(captured["stderr"].closed, "The parent should release its writer after Popen duplicates it")
        self.assertIn("remote forward success for:", entry["log"].read_text(encoding="utf-8"))
        # Child descriptor is deliberately still open during readiness parsing.
        self.manager._wait_ready(self.mapping_id, entry)
        self.assertIsNone(entry["process"].poll())
        self.manager._save_runtime.assert_called_once()

    def test_process_creation_failure_closes_log_writer(self):
        captured = {}
        def fail(args, **kwargs):
            captured.update(args=args, **kwargs)
            self.assertFalse(kwargs["stderr"].closed)
            raise OSError("simulated CreateProcess failure")
        with patch("jumper_manager.engine.subprocess.Popen", side_effect=fail):
            with self.assertRaisesRegex(OSError, "CreateProcess"):
                self.manager._spawn(self.mapping_id, "test-device", "-R", "127.0.0.1:55271:127.0.0.1:7897")
        self.assertTrue(captured["stderr"].closed)
        self.assertFalse(self.manager._processes)
        self.manager._save_runtime.assert_not_called()

    def test_runtime_save_failure_cleans_created_process_and_closes_writer(self):
        captured = {}
        job = MagicMock()
        self.manager._save_runtime.side_effect = OSError("simulated runtime write failure")
        with patch("jumper_manager.engine.subprocess.Popen", side_effect=self.process_factory(captured)), \
             patch("jumper_manager.engine.WindowsJob", return_value=job), \
             patch("jumper_manager.engine.identity", return_value={"created": "test", "image": "ssh.exe"}), \
             patch("jumper_manager.engine.terminate_owned", return_value=True) as terminate:
            with self.assertRaisesRegex(OSError, "runtime write"):
                self.manager._spawn(self.mapping_id, "test-device", "-R", "127.0.0.1:55271:127.0.0.1:7897")
        self.assertTrue(captured["stderr"].closed)
        terminate.assert_called_once()
        record, process, used_job = terminate.call_args.args
        self.assertIs(process, captured["process"])
        self.assertIs(used_job, job)
        self.assertIn(record["marker"], subprocess.list2cmdline(captured["args"]))


if __name__ == "__main__":
    unittest.main()
