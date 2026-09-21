"""Tray lifecycle tests use a fake backend and never display native UI."""

from __future__ import annotations

import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jumper_manager import tray
from support import temp_directory


class FakeItem:
    def __init__(self, text, action, **kwargs):
        self.text_value = text
        self.action = action
        self.default = kwargs.get("default", False)
        self.enabled_value = kwargs.get("enabled", True)
        self.checked_value = kwargs.get("checked")

    @property
    def text(self):
        return self.text_value(self) if callable(self.text_value) else self.text_value

    @property
    def enabled(self):
        return self.enabled_value(self) if callable(self.enabled_value) else self.enabled_value

    @property
    def checked(self):
        return self.checked_value(self) if callable(self.checked_value) else self.checked_value


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeIcon:
    HAS_NOTIFICATION = True

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.visible = False
        self.finished = threading.Event()
        self.entered = threading.Event()
        self.run_thread = None
        self.stop_calls = 0
        self.updates = 0
        self.notifications = []

    def run(self, setup):
        self.run_thread = threading.get_ident()
        self.entered.set()
        setup(self)
        self.finished.wait(3)

    def stop(self):
        self.stop_calls += 1
        self.finished.set()

    def update_menu(self):
        self.updates += 1

    def notify(self, message, title=None):
        self.notifications.append((message, title))


class FakeAutostart:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.read_error = None
        self.write_error = None
        self.writes = []
        self.write_threads = []
        self.reads = 0

    def is_enabled(self):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return self.enabled

    def set_enabled(self, value):
        self.writes.append(value)
        self.write_threads.append(threading.get_ident())
        if self.write_error:
            raise self.write_error
        self.enabled = value


def wait_for(predicate, timeout=1.5):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("Condition did not become true before timeout")
        time.sleep(0.005)


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.status = {"mappings": []}
        self.exit_calls = []
        self.backend = SimpleNamespace(MenuItem=FakeItem, Menu=FakeMenu, Icon=FakeIcon)
        self.patches = [patch.object(tray.sys, "platform", "win32"),
                        patch.object(tray, "_load_pystray", return_value=self.backend),
                        patch.object(tray, "create_icon", return_value=object())]
        for item in self.patches:
            item.start()
        self.controller = tray.TrayController("http://127.0.0.1:8765", Path("data"),
                                              lambda: self.status,
                                              lambda: self.exit_calls.append(threading.get_ident()))
        self.controller.POLL_INTERVAL = 0.02

    def tearDown(self):
        self.controller.stop(timeout=1)
        for item in reversed(self.patches):
            item.stop()

    def test_start_waits_for_setup_and_stop_is_idempotent(self):
        self.assertIs(self.controller.start(), self.controller)
        icon = self.controller._icon
        self.assertTrue(icon.visible)
        self.assertTrue(self.controller.is_running)
        self.assertNotEqual(icon.run_thread, threading.get_ident())
        self.assertIs(self.controller.start(), self.controller)
        self.controller.stop()
        self.controller.stop()
        self.assertFalse(icon.visible)
        self.assertFalse(self.controller._thread.is_alive())
        self.assertFalse(self.controller._status_thread.is_alive())
        self.assertFalse(self.controller.is_running)

    def test_counts_menu_and_tooltip_follow_state_changes(self):
        self.status["mappings"] = [{"status": value} for value in ("running", "running", "degraded", "error", "stopped")]
        self.controller.start()
        icon = self.controller._icon
        wait_for(lambda: "运行 2/5" in icon.title)
        self.assertIn("降级 1", icon.title)
        self.assertIn("异常 1", icon.title)
        status_item = icon.menu.items[1]
        self.assertFalse(status_item.enabled)
        self.assertIn("2 运行中 / 5 总数", status_item.text)
        self.assertIn("2 需关注", status_item.text)
        previous_updates = icon.updates
        self.status["mappings"] = [{"status": "stopped"}]
        wait_for(lambda: "运行 0/1" in icon.title)
        self.assertGreater(icon.updates, previous_updates)

    def test_waiting_target_counts_as_running_tunnel_and_recovers(self):
        waiting = {"status": "degraded", "error": None,
                   "health": {"tunnel_ok": True, "target_ok": False}}
        self.status["mappings"] = [waiting, {"status": "running"},
                                   {"status": "degraded"}, {"status": "error"}]
        self.controller.start()
        icon = self.controller._icon
        wait_for(lambda: "等待服务 1" in icon.title)
        self.assertIn("运行 2/4", icon.title)
        self.assertIn("降级 1", icon.title)
        self.assertIn("异常 1", icon.title)
        self.assertIn("1 等待服务", self.controller._status_text())
        self.assertIn("2 需关注", self.controller._status_text())
        self.status["mappings"] = [{"status": "running", "health": {"tunnel_ok": True, "target_ok": True}}]
        wait_for(lambda: "运行 1/1" in icon.title)
        self.assertIn("等待服务 0", icon.title)
        self.assertIn("0 需关注", self.controller._status_text())

    def test_waiting_target_requires_explicit_health_and_unchanged_config(self):
        cases = [
            {"status": "degraded", "health": {"tunnel_ok": True, "target_ok": False}, "config_changed": True},
            {"status": "degraded", "health": {"tunnel_ok": False, "target_ok": False}},
            {"status": "degraded", "health": {"tunnel_ok": 1, "target_ok": False}},
            {"status": "degraded", "health": {"tunnel_ok": True, "target_ok": 0}},
            {"status": "degraded", "health": {"tunnel_ok": True}},
            {"status": "degraded", "health": None},
            {"status": "degraded", "health": []},
            {"status": "error", "health": {"tunnel_ok": True, "target_ok": False}},
            {"status": "stopped", "health": {"tunnel_ok": True, "target_ok": False}},
        ]
        self.status["mappings"] = cases
        self.controller.start()
        icon = self.controller._icon
        wait_for(lambda: "运行 0/9" in icon.title)
        self.assertIn("等待服务 0", icon.title)
        self.assertIn("降级 7", icon.title)
        self.assertIn("异常 1", icon.title)
        self.assertIn("8 需关注", self.controller._status_text())

    def test_exit_runs_off_callback_thread_and_only_once(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def on_exit():
            self.exit_calls.append(threading.get_ident())
            entered.set()
            release.wait(2)
            finished.set()

        self.controller.on_exit = on_exit
        self.controller.start()
        exit_item = self.controller._icon.menu.items[-1]
        try:
            exit_item.action(None, None)
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.is_set())
            self.assertNotEqual(self.exit_calls[0], threading.get_ident())
            self.assertFalse(exit_item.enabled)
            exit_item.action(None, None)
            self.assertEqual(len(self.exit_calls), 1)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_failed_exit_can_be_retried(self):
        called = threading.Event()

        def on_exit():
            self.exit_calls.append(1)
            called.set()
            raise RuntimeError("shutdown failed")

        self.controller.on_exit = on_exit
        self.controller.start()
        with self.assertLogs(tray.LOG, level="ERROR"):
            self.controller._request_exit()
            self.assertTrue(called.wait(1))
            wait_for(lambda: not self.controller._exit_requested.is_set())
        self.assertTrue(self.controller._icon.menu.items[-1].enabled)

    def test_default_action_opens_browser_and_coalesces_double_click(self):
        opened = threading.Event()
        with patch.object(tray.webbrowser, "open", side_effect=lambda *args, **kwargs: opened.set()) as browser:
            self.controller.start()
            default = self.controller._icon.menu.items[0]
            self.assertTrue(default.default)
            default.action(None, None)
            default.action(None, None)
            self.assertTrue(opened.wait(1))
            browser.assert_called_once_with("http://127.0.0.1:8765", new=2)

    def test_open_logs_uses_data_directory_without_shell_command(self):
        opened = threading.Event()
        with temp_directory() as directory, patch.object(tray.os, "startfile", create=True, side_effect=lambda path: opened.set()) as startfile:
            self.controller.data_dir = Path(directory) / "logs"
            self.controller.start()
            self.controller._open_logs()
            self.assertTrue(opened.wait(1))
            self.assertTrue(self.controller.data_dir.is_dir())
            startfile.assert_called_once_with(str(self.controller.data_dir))

    def test_run_failure_reaches_start_without_waiting_for_timeout(self):
        class BrokenIcon(FakeIcon):
            def run(self, setup):
                raise OSError("native window creation failed")

        self.backend.Icon = BrokenIcon
        started = time.monotonic()
        with self.assertLogs(tray.LOG, level="ERROR"), self.assertRaisesRegex(RuntimeError, "native window creation failed"):
            self.controller.start(timeout=1)
        self.assertLess(time.monotonic() - started, 0.8)

    def test_setup_failure_reaches_start_and_cleans_thread(self):
        class HiddenIcon(FakeIcon):
            @property
            def visible(self):
                return False

            @visible.setter
            def visible(self, value):
                if value:
                    raise OSError("notification area unavailable")

        self.backend.Icon = HiddenIcon
        with self.assertLogs(tray.LOG, level="ERROR"), self.assertRaisesRegex(RuntimeError, "notification area unavailable"):
            self.controller.start(timeout=1)
        self.assertFalse(self.controller._thread.is_alive())

    def test_start_timeout_stops_backend(self):
        class SlowIcon(FakeIcon):
            def run(self, setup):
                self.finished.wait(3)

        self.backend.Icon = SlowIcon
        with self.assertRaisesRegex(RuntimeError, "超时"):
            self.controller.start(timeout=0.02)
        self.assertFalse(self.controller._thread.is_alive())
        self.assertTrue(self.controller._icon.finished.is_set())

    def test_state_callback_failure_is_visible_and_recovers(self):
        problem = {"fail": True}

        def get_status():
            if problem["fail"]:
                raise ValueError("state failed")
            return {"mappings": [{"status": "running"}]}

        self.controller.get_status = get_status
        with self.assertLogs(tray.LOG, level="ERROR"):
            self.controller.start()
            wait_for(lambda: "状态读取失败" in self.controller._icon.title)
        problem["fail"] = False
        wait_for(lambda: "运行 1/1" in self.controller._icon.title)
        self.assertIn("1 运行中", self.controller._status_text())

    def test_stop_before_start_and_non_windows_error(self):
        self.controller.stop()
        with self.assertRaisesRegex(RuntimeError, "已经停止"):
            self.controller.start()
        with patch.object(tray.sys, "platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "仅支持 Windows"):
                self.controller.start()

    def test_autostart_without_provider_is_unchecked_and_disabled(self):
        self.controller.start()
        item = self.controller._icon.menu.items[4]
        self.assertEqual(item.text, "开机自启（登录后）")
        self.assertFalse(item.checked)
        self.assertFalse(item.enabled)
        item.action(None, None)
        self.assertIsNone(self.controller._autostart_thread)

    def test_autostart_initial_and_external_states_are_read_without_writes(self):
        provider = FakeAutostart(True)
        self.controller.autostart = provider
        self.controller.start()
        item = self.controller._icon.menu.items[4]
        wait_for(lambda: item.checked)
        self.assertTrue(item.enabled)
        self.assertEqual(provider.writes, [])
        updates = self.controller._icon.updates
        provider.enabled = False
        wait_for(lambda: not item.checked)
        self.assertGreater(self.controller._icon.updates, updates)
        self.assertEqual(provider.writes, [])

    def test_autostart_toggle_runs_in_background_and_confirms_real_state(self):
        provider = FakeAutostart(False)
        self.controller.autostart = provider
        self.controller.start()
        item = self.controller._icon.menu.items[4]
        item.action(None, None)
        wait_for(lambda: bool(self.controller._icon.notifications))
        self.assertTrue(item.checked)
        self.assertEqual(provider.writes, [True])
        self.assertNotEqual(provider.write_threads[0], threading.get_ident())
        self.assertIn("已启用", self.controller._icon.notifications[-1][0])
        item.action(None, None)
        wait_for(lambda: len(self.controller._icon.notifications) == 2)
        self.assertFalse(item.checked)
        self.assertEqual(provider.writes, [True, False])
        self.assertIn("已关闭", self.controller._icon.notifications[-1][0])

    def test_autostart_duplicate_clicks_are_ignored_until_write_finishes(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingAutostart(FakeAutostart):
            def set_enabled(inner, value):
                entered.set()
                release.wait(1)
                super().set_enabled(value)

        provider = BlockingAutostart(False)
        self.controller.autostart = provider
        self.controller.start()
        item = self.controller._icon.menu.items[4]
        try:
            item.action(None, None)
            self.assertTrue(entered.wait(1))
            self.assertFalse(item.enabled)
            self.assertFalse(item.checked)
            self.assertIn("正在设置", item.text)
            item.action(None, None)
        finally:
            release.set()
        wait_for(lambda: bool(self.controller._icon.notifications))
        self.assertEqual(provider.writes, [True])
        self.assertTrue(item.checked)

    def test_autostart_write_failure_does_not_optimistically_check_menu(self):
        provider = FakeAutostart(False)
        provider.write_error = PermissionError("registry access denied")
        self.controller.autostart = provider
        self.controller.start()
        item = self.controller._icon.menu.items[4]
        with self.assertLogs(tray.LOG, level="ERROR"):
            item.action(None, None)
            wait_for(lambda: bool(self.controller._icon.notifications))
        self.assertFalse(item.checked)
        self.assertTrue(item.enabled)
        self.assertIn("无法修改", self.controller._icon.notifications[-1][0])
        self.assertIn("registry access denied", self.controller._icon.notifications[-1][0])

    def test_autostart_read_failure_is_visible_unchecked_and_retryable(self):
        provider = FakeAutostart(True)
        provider.read_error = PermissionError("registry read denied")
        self.controller.autostart = provider
        with self.assertLogs(tray.LOG, level="ERROR"):
            self.controller.start()
            item = self.controller._icon.menu.items[4]
            wait_for(lambda: "读取失败" in item.text)
            self.assertFalse(item.checked)
            self.assertTrue(item.enabled)
            item.action(None, None)
            wait_for(lambda: bool(self.controller._icon.notifications))
        self.assertEqual(provider.writes, [])
        self.assertFalse(item.checked)
        self.assertTrue(item.enabled)
        provider.read_error = None
        wait_for(lambda: item.checked)
        self.assertEqual(item.text, "开机自启（登录后）")

    def test_autostart_readback_mismatch_reports_failure(self):
        class NoOpAutostart(FakeAutostart):
            def set_enabled(inner, value):
                inner.writes.append(value)

        self.controller.autostart = NoOpAutostart(False)
        self.controller.start()
        item = self.controller._icon.menu.items[4]
        with self.assertLogs(tray.LOG, level="ERROR"):
            item.action(None, None)
            wait_for(lambda: bool(self.controller._icon.notifications))
        self.assertFalse(item.checked)
        self.assertIn("不一致", self.controller._icon.notifications[-1][0])

    def test_autostart_toggle_disabled_during_exit(self):
        provider = FakeAutostart(False)
        self.controller.autostart = provider
        self.controller.start()
        self.controller._request_exit()
        item = self.controller._icon.menu.items[4]
        self.assertFalse(item.enabled)
        item.action(None, None)
        self.assertEqual(provider.writes, [])


class IconTests(unittest.TestCase):
    def test_icon_is_transparent_rgba_and_can_be_saved_as_ico(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is optional for non-GUI test environments")
        icon = tray.create_icon()
        self.assertEqual(icon.mode, "RGBA")
        self.assertEqual(icon.size, (256, 256))
        self.assertEqual(icon.getpixel((0, 0))[3], 0)
        output = io.BytesIO()
        icon.save(output, format="ICO", sizes=[(16, 16), (32, 32), (64, 64), (256, 256)])
        output.seek(0)
        with Image.open(output) as loaded:
            self.assertEqual(loaded.format, "ICO")
            self.assertEqual(loaded.size, (256, 256))

    def test_reject_invalid_icon_sizes_without_importing_gui(self):
        for size in (0, 15, 1025, "256", True):
            with self.subTest(size=size), self.assertRaises(ValueError):
                tray.create_icon(size)


if __name__ == "__main__":
    unittest.main()
