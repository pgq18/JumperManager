"""Daily check CLI: partial edits, live scheduling, lookup and output contracts."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import unittest

from jumper_manager.cli import run
from test_cli import FakeAPI


class DailyCheckCLITests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.api.mappings[0].update(daily_check_enabled=True, daily_check_time="08:15")

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run(args, running=self.api.running, request=self.api.request, data=Path("unused-test-data"))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_unrelated_edit_keeps_daily_check_configuration(self):
        before = copy.deepcopy(self.api.mappings[0])
        code, output, error = self.invoke(["mappings", "edit", "demo", "--name", "renamed", "--json"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output), {**before, "name": "renamed"})
        body = self.api.calls[-1][2]
        self.assertIs(body["daily_check_enabled"], True)
        self.assertEqual(body["daily_check_time"], "08:15")
        self.assertNotIn("status", body)

    def test_edit_time_alone_preserves_enabled_or_disabled_setting(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.api.mappings[0]["daily_check_enabled"] = enabled
                code, output, error = self.invoke(["mappings", "edit", "demo", "--daily-check-time", "23:59", "--json"])
                self.assertEqual(code, 0, error)
                result = json.loads(output)
                self.assertIs(result["daily_check_enabled"], enabled)
                self.assertEqual(result["daily_check_time"], "23:59")

    def test_add_sends_explicit_schedule_options_without_starting(self):
        code, output, error = self.invoke(["mappings", "add", "--source-host", "local", "--source-port", "50051",
                                          "--target-host", "device-a", "--target-port", "50052",
                                          "--daily-check", "--daily-check-time", "00:00", "--json"])
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual(result["status"], "stopped")
        self.assertIs(result["daily_check_enabled"], True)
        self.assertEqual(result["daily_check_time"], "00:00")
        self.assertEqual(self.api.calls[-1][1], "/api/mappings")

    def test_schedule_changes_live_mapping_without_stop_or_general_edit(self):
        self.api.mappings[0]["status"] = "running"
        before = copy.deepcopy(self.api.mappings[0])
        code, output, error = self.invoke(["mappings", "schedule", "demo", "--daily-check-time", "12:30", "--json"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output), {**before, "daily_check_time": "12:30"})
        writes = [call for call in self.api.calls if call[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][:4], ("POST", "/api/mappings/abc111/schedule",
                                        {"daily_check_enabled": True, "daily_check_time": "12:30"}, "session-token"))

    def test_schedule_switch_preserves_time_and_disabled_time_stays_disabled(self):
        code, _, error = self.invoke(["mappings", "schedule", "demo", "--no-daily-check"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.api.calls[-1][2], {"daily_check_enabled": False, "daily_check_time": "08:15"})
        code, _, error = self.invoke(["mappings", "schedule", "demo", "--daily-check-time", "10:45"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.api.calls[-1][2], {"daily_check_enabled": False, "daily_check_time": "10:45"})

    def test_schedule_defaults_missing_legacy_fields(self):
        code, _, error = self.invoke(["mappings", "schedule", "secondary", "--daily-check"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.api.calls[-1][2], {"daily_check_enabled": True, "daily_check_time": "09:00"})

    def test_schedule_without_options_is_read_only_and_retains_json_snapshot(self):
        runtime = {"next_run": "2026-10-26T08:15:00+08:00", "last_run": "2026-10-25T08:15:00+08:00",
                   "result": "reconnected", "message": "连接已恢复", "timezone": "UTC+08:00", "eligible": True}
        self.api.mappings[0]["scheduled_check"] = runtime
        for ref in ("demo", "abc1", "abc111"):
            with self.subTest(ref=ref):
                code, output, error = self.invoke(["mappings", "schedule", ref, "--json"])
                self.assertEqual(code, 0, error)
                self.assertEqual(json.loads(output), self.api.mappings[0])
        self.assertTrue(all(call[0] == "GET" and call[1] == "/api/state" for call in self.api.calls))

    def test_schedule_ambiguous_reference_does_not_mutate(self):
        code, output, _ = self.invoke(["mappings", "schedule", "abc", "--daily-check", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("不唯一", json.loads(output)["error"])
        self.assertFalse(any(call[0] != "GET" for call in self.api.calls))

    def test_invalid_time_or_conflicting_switches_never_contacts_service(self):
        for action in ("edit", "schedule"):
            for time in ("9:00", "24:00", "12:60", "12:00:00", " 09:00", "09:00\n", "０９:００", ""):
                with self.subTest(action=action, time=time):
                    code, output, _ = self.invoke(["mappings", action, "demo", "--daily-check-time", time, "--json"])
                    self.assertEqual(code, 2)
                    self.assertIn("HH:MM", json.loads(output)["error"])
        code, _, _ = self.invoke(["mappings", "schedule", "demo", "--daily-check", "--no-daily-check"])
        self.assertEqual(code, 2)
        self.assertEqual(self.api.calls, [])

    def test_show_and_list_include_schedule_and_recent_result(self):
        self.api.mappings[0]["scheduled_check"] = {
            "next_run": "2026-10-26T08:15:00+08:00", "last_run": "2026-10-25T08:15:00+08:00",
            "result": "reconnected", "message": "连接已恢复", "timezone": "UTC+08:00", "eligible": True}
        code, output, error = self.invoke(["mappings", "show", "demo"])
        self.assertEqual(code, 0, error)
        for text in ("每日检查：每天 08:15", "管理器本地时间，UTC+08:00", "下次检查：2026-10-26",
                     "最近定时检查：2026-10-25", "已重新连接", "连接已恢复"):
            self.assertIn(text, output)
        code, output, error = self.invoke(["mappings", "list"])
        self.assertEqual(code, 0, error)
        self.assertIn("每日检查", output)
        self.assertIn("每天 08:15", output)
        self.assertIn("已关闭", output)

    def test_show_stopped_schedule_does_not_promise_a_restart(self):
        self.api.mappings[0]["scheduled_check"] = {"eligible": False, "next_run": None}
        code, output, error = self.invoke(["mappings", "show", "demo"])
        self.assertEqual(code, 0, error)
        self.assertIn("手动停止的映射不会自动启动", output)


if __name__ == "__main__":
    unittest.main()
