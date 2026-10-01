"""Public update actions must not install on check, cancellation or errors."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from jumper_manager import updates


class UpdateActionsTests(unittest.TestCase):
    def info(self, available=True):
        info = Mock(available=available, version="9.0.0")
        info.to_dict.return_value = {"version": "9.0.0", "current_version": "1.2.3-dev", "available": available}
        return info

    def invoke(self, args, info, installed=None):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(updates, "check_release", return_value=info), \
             patch.object(updates, "install_release", return_value=installed) as install, \
             redirect_stdout(output), redirect_stderr(error):
            code = updates.run(args, root=Path("app"))
        return code, output.getvalue(), error.getvalue(), install

    def test_check_is_read_only_with_new_version_and_stopped_manager(self):
        code, output, _, install = self.invoke(["--check", "--json"], self.info())
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["available"])
        self.assertFalse(json.loads(output)["updated"])
        install.assert_not_called()

    def test_current_version_never_installs(self):
        code, output, _, install = self.invoke([], self.info(False))
        self.assertEqual(code, 0)
        self.assertIn("没有可用的新版本", output)
        install.assert_not_called()

    def test_explicit_update_waits_and_reports_success(self):
        code, output, _, install = self.invoke(["--json"], self.info(), {"status": "success", "warnings": []})
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["updated"])
        self.assertTrue(install.call_args.kwargs["wait"])
        self.assertIsNone(install.call_args.kwargs["progress"])

    def test_install_failure_is_nonzero_json(self):
        code, output, _, _ = self.invoke(["--json"], self.info(), {"status": "error", "message": "rollback"})
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["updated"])

    def test_network_failure_does_not_say_current(self):
        out = io.StringIO()
        with patch.object(updates, "check_release", side_effect=OSError("network down")), redirect_stdout(out):
            self.assertEqual(updates.run(["--json"], root=Path("app")), 1)
        self.assertIn("network down", json.loads(out.getvalue())["error"])

    def test_source_install_is_rejected_before_download(self):
        with patch.object(updates.sys, "frozen", False, create=True), patch.object(updates, "stage_release") as stage:
            with self.assertRaisesRegex(updates.UpdateError, "源码"):
                updates.install_release(self.info(), Path("app"))
            stage.assert_not_called()

    def test_tray_cancel_does_not_download_or_install(self):
        with patch.object(updates, "check_release", return_value=self.info()), \
             patch.object(updates, "_message", return_value=False), patch.object(updates, "install_release") as install:
            self.assertFalse(updates.windows_update(Path("app"), progress=Mock()))
            install.assert_not_called()

    def test_tray_current_version_only_displays_version(self):
        with patch.object(updates, "check_release", return_value=self.info(False)), \
             patch.object(updates, "_message") as message, patch.object(updates, "install_release") as install:
            self.assertFalse(updates.windows_update(Path("app"), progress=Mock()))
            self.assertIn("没有可用的新版本", message.call_args.args[0])
            install.assert_not_called()

    def test_tray_confirm_hands_off_in_background(self):
        with patch.object(updates, "check_release", return_value=self.info()), \
             patch.object(updates, "_message", return_value=True), \
             patch.object(updates, "install_release", return_value={"status": "ready"}) as install:
            self.assertTrue(updates.windows_update(Path("app"), progress=Mock()))
            self.assertFalse(install.call_args.kwargs["wait"])


if __name__ == "__main__":
    unittest.main()
