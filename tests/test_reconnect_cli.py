"""Auto-reconnect CLI configuration, cancellation controls and status contracts."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import unittest

from jumper_manager.cli import mapping_status, run
from test_cli import FakeAPI


class ReconnectCLITests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run(args, running=self.api.running, request=self.api.request, data=Path("unused"))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_live_toggle_only_changes_reconnect_and_keeps_session_auth(self):
        self.api.mappings[0].update(status="running", daily_check_enabled=True)
        for flag, enabled in (("--no-auto-reconnect", False), ("--auto-reconnect", True)):
            with self.subTest(enabled=enabled):
                before = copy.deepcopy(self.api.mappings[0])
                code, output, error = self.invoke(["mappings", "reconnect", "demo", flag, "--json"])
                self.assertEqual(code, 0, error)
                self.assertEqual(json.loads(output), {**before, "auto_reconnect_enabled": enabled})
                self.assertEqual(self.api.calls[-1][:4], ("POST", "/api/mappings/abc111/reconnect",
                    {"auto_reconnect_enabled": enabled}, "session-token"))

    def test_read_only_and_lookup(self):
        self.api.mappings[0]["auto_reconnect"] = {"state": "exhausted", "attempts": 3}
        for ref in ("demo", "abc1", "abc111"):
            code, output, error = self.invoke(["mappings", "reconnect", ref, "--json"])
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output), self.api.mappings[0])
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))
        code, _, _ = self.invoke(["mappings", "reconnect", "abc", "--auto-reconnect"])
        self.assertEqual(code, 2)
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))

    def test_add_and_unrelated_edit_preserve_false(self):
        code, output, error = self.invoke(["mappings", "add", "--name", "created", "--source-host", "local", "--source-port", "50051",
            "--target-host", "device-a", "--target-port", "50052", "--no-auto-reconnect", "--json"])
        self.assertEqual(code, 0, error)
        self.assertIs(json.loads(output)["auto_reconnect_enabled"], False)
        self.assertEqual(json.loads(output)["status"], "stopped")
        self.api.mappings[0]["auto_reconnect_enabled"] = False
        code, output, error = self.invoke(["mappings", "edit", "demo", "--name", "renamed", "--json"])
        self.assertEqual(code, 0, error)
        self.assertIs(json.loads(output)["auto_reconnect_enabled"], False)

    def test_conflicting_switches_never_call_service(self):
        code, _, _ = self.invoke(["mappings", "reconnect", "demo", "--auto-reconnect", "--no-auto-reconnect"])
        self.assertEqual(code, 2)
        self.assertEqual(self.api.calls, [])

    def test_recovery_statuses_are_not_green_started_or_start_failure(self):
        for state in ("waiting", "reconnecting"):
            for status in ("error", "starting"):
                self.assertEqual(mapping_status({"status": status, "failure_stage": "start", "auto_reconnect": {"state": state}}), "重连中")
        self.assertEqual(mapping_status({"status": "error", "failure_stage": "start", "auto_reconnect": {"state": "exhausted"}}), "已断开")
        self.assertEqual(mapping_status({"status": "stopped", "auto_reconnect": {"state": "exhausted"}}), "已停止")

    def test_list_and_show_include_exhaustion(self):
        self.api.mappings[0].update(status="error", auto_reconnect_enabled=True,
            auto_reconnect={"state": "exhausted", "attempts": 3, "message": "已失败 3 次，请手动启动。"})
        for args in (["mappings", "list"], ["mappings", "show", "demo"]):
            code, output, error = self.invoke(args)
            self.assertEqual(code, 0, error)
            self.assertIn("已断开", output)
            self.assertIn("已停止重试（3 次失败）", output)


if __name__ == "__main__":
    unittest.main()
