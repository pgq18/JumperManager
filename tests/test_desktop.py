"""Desktop entry-point regressions with fake tray, HTTP server and SSH manager."""

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from support import temp_directory


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def loaded_app(*, frozen=False, executable=None, windows=True):
    """Load an independent entry point without launching its main function."""
    spec = importlib.util.spec_from_file_location("jumper_desktop_test_app", PROJECT_ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "frozen", frozen, create=True), patch.object(sys, "executable", str(executable or sys.executable)):
        spec.loader.exec_module(module)
        module.WINDOWS = windows
        # These entry-point tests do not change the test runner's DLL search
        # path or require the optional desktop dependencies to be installed.
        with patch.object(module, "prepare_frozen_runtime"):
            yield module


class DesktopPathTests(unittest.TestCase):
    def test_source_paths_remain_relative_to_app(self):
        with loaded_app() as app:
            self.assertEqual(app.BUNDLE_ROOT, PROJECT_ROOT)
            self.assertEqual(app.ROOT, PROJECT_ROOT)
            self.assertEqual(app.DATA, PROJECT_ROOT / "data")
            self.assertEqual(app.RUNTIME, PROJECT_ROOT / "data" / "server.json")

    def test_frozen_resources_and_writable_data_have_separate_roots(self):
        executable = PROJECT_ROOT / "portable folder" / "JumperManager.exe"
        with loaded_app(frozen=True, executable=executable) as app:
            self.assertEqual(app.BUNDLE_ROOT, PROJECT_ROOT)
            self.assertEqual(app.ROOT, executable.parent)
            self.assertEqual(app.DATA, executable.parent / "data")
            self.assertEqual(app.RUNTIME, executable.parent / "data" / "server.json")
            self.assertNotEqual(app.DATA.parent, app.BUNDLE_ROOT)


class AutostartCommandTests(unittest.TestCase):
    def test_frozen_command_keeps_installed_exe_port_and_absolute_ssh_config(self):
        executable = PROJECT_ROOT / "portable folder" / "JumperManager.exe"
        extraction = PROJECT_ROOT / "temporary extraction" / "_MEI12345"
        args = SimpleNamespace(port=12345, ssh_config="custom config/ssh.conf")
        with loaded_app(frozen=True, executable=executable) as app, patch.object(app, "BUNDLE_ROOT", extraction), patch.object(sys, "_MEIPASS", str(extraction), create=True):
            command = app.autostart_command(args)
        self.assertEqual(command[0], str(executable.resolve()))
        self.assertEqual(command[1:], ["--tray", "--no-browser", "--port", "12345", "--ssh-config", str(Path(args.ssh_config).resolve())])
        self.assertNotIn(str(extraction), " ".join(command))
        self.assertNotIn("--serve", command)
        self.assertNotIn("--open", command)

    def test_source_command_prefers_pythonw_and_preserves_source_entry_point(self):
        executable = PROJECT_ROOT / "Python runtime" / "python.exe"
        args = SimpleNamespace(port=9876, ssh_config=None)
        with loaded_app(executable=executable) as app, patch.object(Path, "exists", return_value=True):
            command = app.autostart_command(args)
        self.assertEqual(command, [str(executable.with_name("pythonw.exe")), str(PROJECT_ROOT / "app.py"), "--serve", "--tray", "--no-browser", "--port", "9876"])

    def test_source_command_retains_interpreter_when_pythonw_is_absent(self):
        executable = PROJECT_ROOT / "Python runtime" / "python.exe"
        args = SimpleNamespace(port=8765, ssh_config=None)
        with loaded_app(executable=executable) as app, patch.object(Path, "exists", return_value=False):
            command = app.autostart_command(args)
        self.assertEqual(command[:3], [str(executable), str(PROJECT_ROOT / "app.py"), "--serve"])
        self.assertIn("--no-browser", command)
        self.assertNotIn("--ssh-config", command)


class DesktopModeTests(unittest.TestCase):
    def test_source_default_preserves_background_launcher(self):
        with loaded_app() as app, patch.object(sys, "argv", ["app.py", "--no-browser"]), patch.object(app, "launch", return_value=0) as launch, patch.object(app, "serve") as serve:
            self.assertEqual(app.main(), 0)
            launch.assert_called_once()
            serve.assert_not_called()

    def test_source_serve_does_not_require_a_tray(self):
        with loaded_app() as app, patch.object(sys, "argv", ["app.py", "--serve", "--no-browser"]), patch.object(app, "launch") as launch, patch.object(app, "serve") as serve:
            self.assertEqual(app.main(), 0)
            serve.assert_called_once()
            self.assertFalse(serve.call_args.args[0].tray)
            launch.assert_not_called()

    def test_source_can_explicitly_enable_tray(self):
        with loaded_app() as app, patch.object(sys, "argv", ["app.py", "--serve", "--tray", "--no-browser"]), patch.object(app, "running", return_value=None), patch.object(app, "launch") as launch, patch.object(app, "serve") as serve:
            self.assertEqual(app.main(), 0)
            serve.assert_called_once()
            self.assertTrue(serve.call_args.args[0].tray)
            launch.assert_not_called()

    def test_frozen_default_runs_server_with_tray_without_spawning_app_py(self):
        with loaded_app(frozen=True) as app, patch.object(sys, "argv", ["JumperManager.exe", "--no-browser"]), patch.object(app, "running", return_value=None), patch.object(app.subprocess, "Popen") as popen, patch.object(app, "serve") as serve:
            self.assertEqual(app.main(), 0)
            serve.assert_called_once()
            self.assertTrue(serve.call_args.args[0].tray)
            popen.assert_not_called()

    def test_frozen_headless_test_mode_remains_available(self):
        with loaded_app(frozen=True) as app, patch.object(sys, "argv", ["JumperManager.exe", "--no-tray", "--no-browser"]), patch.object(app, "running", return_value=None), patch.object(app.subprocess, "Popen") as popen, patch.object(app, "serve") as serve:
            self.assertEqual(app.main(), 0)
            serve.assert_called_once()
            self.assertFalse(serve.call_args.args[0].tray)
            self.assertFalse(serve.call_args.args[0].open_browser)
            popen.assert_not_called()

    def test_frozen_first_launch_requests_browser_after_server_start(self):
        with loaded_app(frozen=True) as app, patch.object(sys, "argv", ["JumperManager.exe"]), patch.object(app, "running", return_value=None), patch.object(app, "serve") as serve, patch.object(app.webbrowser, "open") as browser:
            self.assertEqual(app.main(), 0)
            serve.assert_called_once()
            self.assertTrue(serve.call_args.args[0].open_browser)
            browser.assert_not_called()

    def test_frozen_launch_flag_does_not_recurse_through_source_launcher(self):
        with loaded_app(frozen=True) as app, patch.object(sys, "argv", ["JumperManager.exe", "--launch", "--no-browser"]), patch.object(app, "running", return_value=None), patch.object(app.subprocess, "Popen") as popen, patch.object(app, "serve") as serve:
            self.assertEqual(app.main(), 0)
            serve.assert_called_once()
            popen.assert_not_called()

    def test_second_frozen_launch_opens_existing_ui_without_new_server(self):
        existing = {"url": "http://127.0.0.1:9999", "instance_id": "existing"}
        with loaded_app(frozen=True) as app, patch.object(sys, "argv", ["JumperManager.exe"]), patch.object(app, "running", return_value=existing), patch.object(app.subprocess, "Popen") as popen, patch.object(app, "serve") as serve, patch.object(app.webbrowser, "open") as browser:
            self.assertEqual(app.main(), 0)
            serve.assert_not_called()
            popen.assert_not_called()
            browser.assert_called_once_with(existing["url"])

    def test_second_frozen_launch_honors_no_browser(self):
        existing = {"url": "http://127.0.0.1:9999", "instance_id": "existing"}
        with loaded_app(frozen=True) as app, patch.object(sys, "argv", ["JumperManager.exe", "--no-browser"]), patch.object(app, "running", return_value=existing), patch.object(app, "serve") as serve, patch.object(app.webbrowser, "open") as browser:
            self.assertEqual(app.main(), 0)
            serve.assert_not_called()
            browser.assert_not_called()

    def test_concurrent_second_launch_waits_for_original_instance(self):
        existing = {"url": "http://127.0.0.1:9999", "instance_id": "first"}
        with loaded_app(frozen=True) as app, patch.object(sys, "argv", ["JumperManager.exe"]), patch.object(app, "running", side_effect=[None, None, existing]), patch.object(app, "serve", side_effect=app.InstanceAlreadyRunning("already starting")) as serve, patch.object(app.subprocess, "Popen") as popen, patch.object(app.time, "sleep") as sleep, patch.object(app.webbrowser, "open") as browser:
            self.assertEqual(app.main(), 0)
            serve.assert_called_once()
            popen.assert_not_called()
            sleep.assert_called_once()
            browser.assert_called_once_with(existing["url"])


@contextmanager
def fake_desktop_service(app, *, tray_enabled=True):
    """Exercise real serve/finally logic without a socket, tray or SSH process."""
    with temp_directory() as temporary:
        root = Path(temporary)
        data = root / "data"
        data.mkdir()
        bundle = root / "extracted bundle"
        manager = MagicMock()
        manager.state.return_value = {"hosts": [], "mappings": []}
        server = MagicMock()
        server.server_address = ("127.0.0.1", 9876)
        server.shutting_down = False
        tray = MagicMock()
        tray_module = ModuleType("jumper_manager.tray")
        tray_module.TrayController = MagicMock(return_value=tray)
        autostart = MagicMock()
        autostart_module = ModuleType("jumper_manager.autostart")
        autostart_module.WindowsAutostart = MagicMock(return_value=autostart)
        instance_lock = MagicMock()
        args = SimpleNamespace(port=9876, ssh_config=None, open_browser=False, tray=tray_enabled)
        fixture = SimpleNamespace(root=root, data=data, bundle=bundle, runtime=data / "server.json", manager=manager, server=server, tray=tray, tray_factory=tray_module.TrayController, autostart=autostart, autostart_factory=autostart_module.WindowsAutostart, lock=instance_lock, args=args)
        with patch.object(app, "ROOT", root), patch.object(app, "DATA", data), patch.object(app, "RUNTIME", fixture.runtime), patch.object(app, "BUNDLE_ROOT", bundle), patch.object(app, "configure_logging"), patch.object(app, "InstanceLock", return_value=instance_lock), patch.object(app.signal, "signal"), patch("jumper_manager.engine.Manager", return_value=manager) as manager_factory, patch("jumper_manager.server.AppServer", return_value=server) as server_factory, patch.dict(sys.modules, {"jumper_manager.tray": tray_module, "jumper_manager.autostart": autostart_module}):
            fixture.manager_factory = manager_factory
            fixture.server_factory = server_factory
            yield fixture


class DesktopLifecycleTests(unittest.TestCase):
    def assert_cleaned(self, fixture):
        fixture.server.server_close.assert_called_once()
        fixture.manager.close.assert_called_once()
        fixture.tray.stop.assert_called_once()
        fixture.lock.close.assert_called_once()
        self.assertFalse(fixture.runtime.exists())

    def test_serving_attaches_autostart_provider_without_changing_login_setting(self):
        with loaded_app(frozen=True) as app, fake_desktop_service(app) as fixture:
            expected_command = app.autostart_command(fixture.args)

            def starting_tray():
                self.assertIs(fixture.tray.autostart, fixture.autostart)

            fixture.tray.start.side_effect = starting_tray
            app.serve(fixture.args)
            fixture.autostart_factory.assert_called_once_with(expected_command)
            self.assertEqual(fixture.autostart.mock_calls, [])
            self.assert_cleaned(fixture)

    def test_tray_ready_and_resource_paths_are_published_before_serving(self):
        with loaded_app(frozen=True) as app, fake_desktop_service(app) as fixture:
            def serving(**_):
                runtime = json.loads(fixture.runtime.read_text(encoding="utf-8"))
                self.assertTrue(runtime["desktop"])
                self.assertTrue(runtime["tray_ready"])
                self.assertEqual(runtime["port"], 9876)
                fixture.tray.start.assert_called_once()
                identity = fixture.server_factory.call_args.args[3]
                self.assertTrue(identity["desktop"])
                self.assertTrue(identity["tray_ready"])

            fixture.server.serve_forever.side_effect = serving
            app.serve(fixture.args)
            fixture.manager_factory.assert_called_once_with(fixture.root, config_path=None)
            self.assertEqual(fixture.server_factory.call_args.args[2], fixture.bundle / "web")
            self.assertEqual(fixture.tray_factory.call_args.args[:2], ("http://127.0.0.1:9876", fixture.data))
            self.assert_cleaned(fixture)

    def test_legacy_source_arguments_without_tray_attribute_still_work(self):
        with loaded_app() as app, fake_desktop_service(app) as fixture:
            del fixture.args.tray

            def serving(**_):
                runtime = json.loads(fixture.runtime.read_text(encoding="utf-8"))
                self.assertFalse(runtime["desktop"])
                self.assertFalse(runtime["tray_ready"])

            fixture.server.serve_forever.side_effect = serving
            app.serve(fixture.args)
            fixture.tray_factory.assert_not_called()
            fixture.server.server_close.assert_called_once()
            fixture.manager.close.assert_called_once()
            fixture.lock.close.assert_called_once()
            self.assertFalse(fixture.runtime.exists())

    def test_tray_exit_marks_shutdown_and_asks_server_to_stop(self):
        with loaded_app(frozen=True) as app, fake_desktop_service(app) as fixture:
            stopped = threading.Event()
            fixture.server.shutdown.side_effect = stopped.set

            def serving(**_):
                exit_callback = fixture.tray_factory.call_args.args[3]
                exit_callback()
                self.assertTrue(fixture.server.shutting_down)
                self.assertTrue(stopped.wait(1), "tray exit did not request HTTP shutdown")

            fixture.server.serve_forever.side_effect = serving
            app.serve(fixture.args)
            fixture.server.shutdown.assert_called_once()
            self.assert_cleaned(fixture)

    def test_tray_start_failure_cleans_resources_and_leaves_no_runtime(self):
        with loaded_app(frozen=True) as app, fake_desktop_service(app) as fixture:
            fixture.tray.start.side_effect = RuntimeError("tray could not start")
            with self.assertRaisesRegex(RuntimeError, "tray could not start"):
                app.serve(fixture.args)
            fixture.server.serve_forever.assert_not_called()
            self.assert_cleaned(fixture)

    def test_serving_failure_still_stops_tray_and_manager(self):
        with loaded_app(frozen=True) as app, fake_desktop_service(app) as fixture:
            fixture.server.serve_forever.side_effect = RuntimeError("serve failed")
            with self.assertRaisesRegex(RuntimeError, "serve failed"):
                app.serve(fixture.args)
            self.assert_cleaned(fixture)

    def test_cleanup_errors_do_not_skip_other_resources(self):
        for failed_resource in ("server", "manager", "tray"):
            with self.subTest(failed_resource=failed_resource), loaded_app(frozen=True) as app, fake_desktop_service(app) as fixture:
                callback = {"server": fixture.server.server_close, "manager": fixture.manager.close, "tray": fixture.tray.stop}[failed_resource]
                callback.side_effect = RuntimeError("cleanup failed")
                try:
                    app.serve(fixture.args)
                except RuntimeError:
                    pass
                self.assert_cleaned(fixture)

    def test_lock_conflict_never_constructs_manager_or_tray(self):
        with loaded_app(frozen=True) as app, fake_desktop_service(app) as fixture:
            existing = {"instance_id": "other-instance", "port": 9876}
            fixture.runtime.write_text(json.dumps(existing), encoding="utf-8")
            fixture.lock.acquire.side_effect = app.InstanceAlreadyRunning("first instance owns lock")
            with self.assertRaises(app.InstanceAlreadyRunning):
                app.serve(fixture.args)
            fixture.server_factory.assert_not_called()
            fixture.manager_factory.assert_not_called()
            fixture.tray_factory.assert_not_called()
            fixture.lock.close.assert_called_once()
            self.assertEqual(json.loads(fixture.runtime.read_text(encoding="utf-8")), existing)


if __name__ == "__main__":
    unittest.main()
