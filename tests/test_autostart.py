"""Opt-in startup tests using a fake registry: no actual HKCU/HKLM writes."""
from __future__ import annotations

import builtins
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from jumper_manager.autostart import RUN_KEY, WindowsAutostart


class FakeKey:
    def __init__(self, path, access):
        self.path = path
        self.access = access
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class FakeWinreg(types.ModuleType):
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self):
        super().__init__("winreg")
        self.keys = {}
        self.calls = []
        self.handles = []
        self.failures = {}

    def fail_if_requested(self, operation):
        if operation in self.failures:
            raise self.failures[operation]

    def new_handle(self, path, access):
        result = FakeKey(path, access)
        self.handles.append(result)
        return result

    def OpenKey(self, hive, path, reserved=0, access=KEY_QUERY_VALUE):
        self.calls.append(("OpenKey", hive, path, reserved, access))
        assert hive is self.HKEY_CURRENT_USER
        self.fail_if_requested("OpenKey")
        if path not in self.keys:
            raise FileNotFoundError(path)
        return self.new_handle(path, access)

    def CreateKeyEx(self, hive, path, reserved=0, access=KEY_SET_VALUE):
        self.calls.append(("CreateKeyEx", hive, path, reserved, access))
        assert hive is self.HKEY_CURRENT_USER
        self.fail_if_requested("CreateKeyEx")
        self.keys.setdefault(path, {})
        return self.new_handle(path, access)

    def QueryValueEx(self, key, name):
        self.calls.append(("QueryValueEx", key.path, name))
        assert key.access & self.KEY_QUERY_VALUE
        self.fail_if_requested("QueryValueEx")
        try:
            return self.keys[key.path][name]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc

    def SetValueEx(self, key, name, reserved, kind, value):
        self.calls.append(("SetValueEx", key.path, name, reserved, kind, value))
        assert key.access & self.KEY_SET_VALUE
        self.fail_if_requested("SetValueEx")
        self.keys[key.path][name] = (value, kind)

    def DeleteValue(self, key, name):
        self.calls.append(("DeleteValue", key.path, name))
        assert key.access & self.KEY_SET_VALUE
        self.fail_if_requested("DeleteValue")
        try:
            del self.keys[key.path][name]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeWinreg()
        self.patch = patch.dict(sys.modules, {"winreg": self.registry})
        self.patch.start()
        self.command = [r"C:\Program Files\Jumper Manager\JumperManager.exe", "--no-browser", "--port", "8765"]
        self.setting = WindowsAutostart(self.command)

    def tearDown(self):
        self.patch.stop()
        self.assertTrue(all(handle.closed for handle in self.registry.handles), "Registry handles must be closed even after errors")

    def test_construction_is_opt_in_and_quotes_executable(self):
        self.assertEqual(self.registry.calls, [])
        self.assertEqual(self.setting.command_line, r'"C:\Program Files\Jumper Manager\JumperManager.exe" --no-browser --port 8765')
        self.assertEqual(self.registry.keys, {})
        # Caller changes to the original argument list cannot alter registration.
        self.command.append("different")
        self.assertNotIn("different", self.setting.command_line)

    def test_missing_key_or_value_reads_false_without_creating_key(self):
        self.assertFalse(self.setting.is_enabled())
        self.assertEqual(self.registry.keys, {})
        self.registry.keys[RUN_KEY] = {}
        self.assertFalse(self.setting.is_enabled())
        self.assertFalse(any(call[0] == "CreateKeyEx" for call in self.registry.calls))
        self.assertEqual(self.registry.calls[0][-1], self.registry.KEY_QUERY_VALUE)

    def test_enable_sets_only_named_hkcu_reg_sz_value(self):
        unrelated = ("existing command", self.registry.REG_SZ)
        self.registry.keys[RUN_KEY] = {"OtherApplication": unrelated}
        self.setting.set_enabled(True)
        self.assertEqual(self.registry.keys[RUN_KEY], {
            "OtherApplication": unrelated,
            "JumperManager": (self.setting.command_line, self.registry.REG_SZ),
        })
        self.assertTrue(self.setting.is_enabled())

    def test_disable_removes_only_owned_installation(self):
        self.setting.set_enabled(True)
        self.registry.keys[RUN_KEY]["OtherApplication"] = ("untouched", self.registry.REG_SZ)
        self.setting.set_enabled(False)
        self.assertEqual(self.registry.keys[RUN_KEY], {"OtherApplication": ("untouched", self.registry.REG_SZ)})
        self.assertFalse(self.setting.is_enabled())

    def test_same_executable_changed_port_and_ssh_config_can_be_disabled(self):
        old_command = subprocess.list2cmdline([
            self.command[0].upper(), "--no-browser", "--port", "8877",
            "--ssh-config", r"C:\Old Config\ssh.conf",
        ])
        self.registry.keys[RUN_KEY] = {"JumperManager": (old_command, self.registry.REG_SZ)}
        self.assertNotEqual(old_command, self.setting.command_line)
        self.assertTrue(self.setting.is_enabled(), "Existing startup registration must stay visible after argument changes")
        self.setting.set_enabled(False)
        self.assertNotIn("JumperManager", self.registry.keys[RUN_KEY])

    def test_enable_replaces_old_arguments_with_current_complete_command(self):
        old_command = subprocess.list2cmdline([self.command[0], "--port", "8877"])
        self.registry.keys[RUN_KEY] = {"JumperManager": (old_command, self.registry.REG_SZ)}
        self.setting.set_enabled(True)
        self.assertEqual(self.registry.keys[RUN_KEY]["JumperManager"], (self.setting.command_line, self.registry.REG_SZ))

    def test_source_identity_includes_interpreter_and_script_but_not_port(self):
        interpreter = r"C:\Python 314\pythonw.exe"
        script = r"C:\Apps\Jumper Manager\app.py"
        setting = WindowsAutostart([interpreter, script, "--no-browser", "--port", "8765"])
        old_command = subprocess.list2cmdline([interpreter.upper(), script.upper(), "--port", "9999"])
        self.registry.keys[RUN_KEY] = {"JumperManager": (old_command, self.registry.REG_SZ)}
        self.assertTrue(setting.is_enabled())
        setting.set_enabled(False)
        self.assertNotIn("JumperManager", self.registry.keys[RUN_KEY])
        other_script = subprocess.list2cmdline([interpreter, r"D:\Other Installation\app.py", "--port", "8765"])
        self.registry.keys[RUN_KEY]["JumperManager"] = (other_script, self.registry.REG_SZ)
        self.assertFalse(setting.is_enabled())
        setting.set_enabled(False)
        self.assertEqual(self.registry.keys[RUN_KEY]["JumperManager"][0], other_script)

    def test_identity_prefix_requires_end_or_space_argument_boundary(self):
        setting = WindowsAutostart([r"C:\Apps\JumperManager.exe", "--port", "8765"])
        for foreign_command in (r"C:\Apps\JumperManager.exe.other --port 8765", r"C:\Apps\JumperManager.exe2", r"C:\Elsewhere\JumperManager.exe"):
            with self.subTest(command=foreign_command):
                self.registry.keys[RUN_KEY] = {"JumperManager": (foreign_command, self.registry.REG_SZ)}
                self.assertFalse(setting.is_enabled())
                setting.set_enabled(False)
                self.assertEqual(self.registry.keys[RUN_KEY]["JumperManager"][0], foreign_command)
        self.registry.keys[RUN_KEY] = {"JumperManager": (r"C:\Apps\JumperManager.exe", self.registry.REG_SZ)}
        self.assertTrue(setting.is_enabled())

    def test_different_installation_or_registry_type_is_not_deleted(self):
        for value in [(r"D:\Another\JumperManager.exe --no-browser", self.registry.REG_SZ),
                      (self.setting.command_line, self.registry.REG_EXPAND_SZ)]:
            with self.subTest(value=value):
                self.registry.keys[RUN_KEY] = {"JumperManager": value}
                self.assertFalse(self.setting.is_enabled())
                self.setting.set_enabled(False)
                self.assertEqual(self.registry.keys[RUN_KEY]["JumperManager"], value)
        self.assertFalse(any(call[0] == "DeleteValue" for call in self.registry.calls))

    def test_disable_missing_key_and_value_is_idempotent(self):
        self.setting.set_enabled(False)
        self.assertEqual(self.registry.keys, {})
        self.registry.keys[RUN_KEY] = {}
        self.setting.set_enabled(False)
        self.setting.set_enabled(False)
        self.assertFalse(any(call[0] == "CreateKeyEx" for call in self.registry.calls))

    def test_permission_failures_are_not_reported_as_disabled(self):
        for operation in ("OpenKey", "QueryValueEx"):
            with self.subTest(operation=operation):
                self.registry.keys[RUN_KEY] = {"JumperManager": (self.setting.command_line, self.registry.REG_SZ)}
                self.registry.failures = {operation: PermissionError("access denied")}
                with self.assertRaises(PermissionError):
                    self.setting.is_enabled()
                with self.assertRaises(PermissionError):
                    self.setting.set_enabled(False)
        self.registry.failures = {"SetValueEx": PermissionError("access denied")}
        with self.assertRaises(PermissionError):
            self.setting.set_enabled(True)
        self.registry.failures = {"DeleteValue": PermissionError("access denied")}
        with self.assertRaises(PermissionError):
            self.setting.set_enabled(False)

    def test_custom_test_key_and_value_stay_under_hkcu(self):
        path = r"Software\JumperManagerTests\Autostart"
        setting = WindowsAutostart([r"C:\Apps\jm.exe"], value_name="TestOnly", registry_path=path)
        setting.set_enabled(True)
        self.assertEqual(set(self.registry.keys), {path})
        self.assertEqual(set(self.registry.keys[path]), {"TestOnly"})
        self.assertTrue(setting.is_enabled())

    def test_command_rejects_empty_invalid_and_control_arguments(self):
        for command in ([], None, "app.exe", [123], [""], ["  "], ["app.exe", "a\x00b"], ["app.exe", "a\nb"], ["app.exe", "a\rb"]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                WindowsAutostart(command)
        for invalid in (None, "true", 1, 0):
            with self.subTest(enabled=invalid), self.assertRaises(ValueError):
                self.setting.set_enabled(invalid)
        self.assertEqual(self.registry.calls, [])

    def test_command_length_limit_uses_utf16_after_quoting(self):
        self.assertEqual(len(WindowsAutostart(["a" * 260]).command_line), 260)
        with self.assertRaisesRegex(ValueError, "260"):
            WindowsAutostart(["a" * 261])
        # Non-BMP characters count as two Windows WCHAR units each.
        self.assertEqual(len(WindowsAutostart(["\U0001f680" * 130]).command_line.encode("utf-16-le")) // 2, 260)
        with self.assertRaisesRegex(ValueError, "260"):
            WindowsAutostart(["\U0001f680" * 131])
        # Whitespace requires quotes, which are part of the Run value limit.
        with self.assertRaisesRegex(ValueError, "260"):
            WindowsAutostart(["a " + "b" * 258])

    def test_registry_names_reject_empty_controls_and_unowned_hives(self):
        for changes in ({"value_name": ""}, {"value_name": "name\x00bad"},
                        {"registry_path": ""}, {"registry_path": "Software\nbad"},
                        {"registry_path": r"HKLM\Software\Run"},
                        {"registry_path": r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                WindowsAutostart(["app.exe"], **changes)
        self.assertEqual(self.registry.calls, [])

    def test_constructing_does_not_require_winreg_module(self):
        original_import = builtins.__import__
        def import_without_winreg(name, *args, **kwargs):
            if name == "winreg":
                raise ModuleNotFoundError("winreg unavailable on this platform")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=import_without_winreg):
            setting = WindowsAutostart(["app.exe"])
        self.assertEqual(setting.command_line, "app.exe")
        self.assertEqual(self.registry.calls, [])


if __name__ == "__main__":
    unittest.main()
