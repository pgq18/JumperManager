"""Linux ownership, graceful group cleanup and restart recovery regressions."""
from pathlib import Path
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

from jumper_manager import processes


def info(pid=321, *, boot="boot-a", image="/usr/bin/ssh", created="100"):
    return {"created": created, "boot_id": boot, "image": image,
            "command": "ssh owned-marker", "pgid": pid, "session": pid}


class LinuxOwnershipTests(unittest.TestCase):
    def test_reused_pid_boot_or_case_distinct_path_never_receives_signal(self):
        expected = info()
        record = {"pid": 321, "identity": expected, "marker": "owned-marker"}
        for current in (info(boot="boot-b"), info(created="200"), info(image="/usr/bin/SSH")):
            with self.subTest(current=current), patch.object(processes, "identity", return_value=current), \
                 patch.object(processes.os, "killpg", create=True) as killpg:
                self.assertFalse(processes._terminate_linux(record))
                killpg.assert_not_called()

    def test_unreadable_proc_does_not_mean_the_process_exited(self):
        record = {"pid": 321, "identity": info(), "marker": "owned-marker"}
        with patch.object(processes, "identity", return_value=None), \
             patch.object(processes, "_linux_group", side_effect=PermissionError("proc denied")), \
             patch.object(processes.os, "kill", side_effect=PermissionError("signal denied")), \
             patch.object(processes.os, "killpg", create=True) as killpg:
            self.assertFalse(processes._terminate_linux(record))
            killpg.assert_not_called()

    def test_nonexistent_process_and_group_can_be_forgotten(self):
        with patch.object(processes, "_linux_group", side_effect=FileNotFoundError()), \
             patch.object(processes.os, "kill", side_effect=ProcessLookupError()), \
             patch.object(processes.os, "killpg", side_effect=ProcessLookupError(), create=True):
            self.assertTrue(processes._linux_resources_gone(321))

    def test_shared_session_is_not_signalled_as_a_group(self):
        current = {**info(), "pgid": 77, "session": 77}
        record = {"pid": 321, "identity": current, "marker": "owned-marker"}
        with patch.object(processes, "identity", return_value=current), \
             patch.object(processes.os, "killpg", create=True) as killpg:
            self.assertFalse(processes._terminate_linux(record))
            killpg.assert_not_called()

    def test_recovery_waits_for_the_group_instead_of_returning_after_sigterm(self):
        current = info()
        member = {"created": "100", "pgid": 321, "session": 321, "state": "S"}
        record = {"pid": 321, "identity": current, "marker": "owned-marker"}
        with patch.object(processes, "identity", return_value=current), \
             patch.object(processes, "_linux_group", side_effect=[({321: member}, True), ({321: member}, True), ({}, True)]), \
             patch.object(processes.time, "sleep") as sleep, \
             patch.object(processes.os, "killpg", create=True) as killpg:
            self.assertTrue(processes._terminate_linux(record))
            sleep.assert_called_once()
            killpg.assert_called_once_with(321, signal.SIGTERM)

    def test_sigkill_includes_known_proxy_after_leader_exits(self):
        current = info()
        leader = {"created": "100", "pgid": 321, "session": 321, "state": "S"}
        child = {"created": "101", "pgid": 321, "session": 321, "state": "S"}
        record = {"pid": 321, "identity": current, "marker": "owned-marker"}
        with patch.object(processes, "identity", return_value=current), \
             patch.object(processes.signal, "SIGKILL", 9, create=True), \
             patch.object(processes, "_linux_group", side_effect=[({321: leader, 322: child}, True), ({322: child}, True), ({}, True)]), \
             patch.object(processes.os, "killpg", create=True) as killpg:
            self.assertTrue(processes._terminate_linux(record, grace=0))
            self.assertEqual([call.args for call in killpg.call_args_list], [(321, signal.SIGTERM), (321, signal.SIGKILL)])


@unittest.skipUnless(sys.platform.startswith("linux"), "real /proc and Unix process groups")
class RealLinuxProcessesTests(unittest.TestCase):
    def launch(self, script):
        process = subprocess.Popen([sys.executable, "-u", "-c", script, "owned-marker"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, **processes.hidden_options())
        self.addCleanup(self.emergency_cleanup, process)
        return process

    @staticmethod
    def emergency_cleanup(process):
        # Only test children started with start_new_session=True are targeted.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=3)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()

    def record(self, process):
        return {"pid": process.pid, "identity": processes.identity(process.pid), "marker": "owned-marker"}

    def test_normal_stop_kills_proxy_ignoring_sigterm_after_leader_exits(self):
        script = '''import subprocess,sys,time
child=subprocess.Popen([sys.executable,'-u','-c',"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"],stdout=subprocess.PIPE,text=True)
child.stdout.readline()
print(child.pid,flush=True)
time.sleep(60)
'''
        process = self.launch(script)
        child_pid = int(process.stdout.readline())
        record = self.record(process)
        self.assertEqual(record["identity"]["pgid"], process.pid)
        self.assertEqual(record["identity"]["session"], process.pid)
        self.assertTrue(record["identity"]["boot_id"])
        self.assertTrue(processes._terminate_linux(record, process, grace=0.2))
        process.wait(timeout=2)
        try:
            self.assertIn(processes._linux_stat(child_pid)["state"], {"Z", "X", "x"})
        except FileNotFoundError:
            pass

    def test_recovered_pid_is_stopped_and_listener_port_released(self):
        script = '''import signal,socket,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1)
print(s.getsockname()[1],flush=True)
time.sleep(60)
'''
        process = self.launch(script)
        port = int(process.stdout.readline())
        self.assertTrue(processes._terminate_linux(self.record(process), grace=0.2))
        process.wait(timeout=2)
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))
            check.listen(1)


if __name__ == "__main__":
    unittest.main()
