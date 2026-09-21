"""Opt-in Windows logon startup for the current JumperManager installation.

Only one HKCU value is managed. Importing or constructing this module never
changes Windows startup settings. The registry module is imported on demand.
"""
from __future__ import annotations

import subprocess


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAX_COMMAND_UTF16_UNITS = 260


def _text(value, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串。")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label}不能包含 NUL 或换行。")
    if nonempty and not value.strip():
        raise ValueError(f"{label}不能为空。")
    return value


class WindowsAutostart:
    def __init__(self, command: list[str], value_name: str = "JumperManager", registry_path: str = RUN_KEY):
        if not isinstance(command, (list, tuple)) or not command:
            raise ValueError("启动命令必须是非空的参数列表。")
        arguments = [_text(argument, "启动参数", nonempty=index == 0) for index, argument in enumerate(command)]
        self._command_line = subprocess.list2cmdline(arguments)
        identity_arguments = arguments[:2] if len(arguments) > 1 and arguments[1].lower().endswith(".py") else arguments[:1]
        self._identity_prefix = subprocess.list2cmdline(identity_arguments)
        try:
            length = len(self._command_line.encode("utf-16-le")) // 2
        except UnicodeEncodeError as exc:
            raise ValueError("启动命令包含无效的 Unicode 字符。") from exc
        if length > MAX_COMMAND_UTF16_UNITS:
            # Run/RunOnce command limit documented by Microsoft:
            # https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys
            raise ValueError("启动命令超过 Windows Run 项的 260 字符限制，请缩短程序路径或参数。")
        self.value_name = _text(value_name, "注册表值名称")
        self.registry_path = _text(registry_path, "注册表路径")
        components = self.registry_path.replace("/", "\\").casefold().split("\\")
        if components[0] in {"", "hkey_current_user", "hkcu", "hkey_local_machine", "hklm"}:
            raise ValueError("注册表路径必须相对于 HKEY_CURRENT_USER。")
        if "startupapproved" in components:
            raise ValueError("本程序不会修改 Windows StartupApproved 设置。")

    @property
    def command_line(self) -> str:
        return self._command_line

    def _owns_command(self, value) -> bool:
        if not isinstance(value, str):
            return False
        # Ports, SSH config paths and launch flags may change between runs.
        # The executable (plus script for source runs) identifies this install.
        length = len(self._identity_prefix)
        return (value[:length].casefold() == self._identity_prefix.casefold()
                and (len(value) == length or value[length:length + 1] == " "))

    def is_enabled(self) -> bool:
        """Return whether this installation is registered, without creating keys."""
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.registry_path, 0, winreg.KEY_QUERY_VALUE) as key:
                value, kind = winreg.QueryValueEx(key, self.value_name)
                return kind == winreg.REG_SZ and self._owns_command(value)
        except FileNotFoundError:
            return False

    def set_enabled(self, enabled: bool) -> None:
        """Write current args on opt-in; remove only this installation on opt-out."""
        if not isinstance(enabled, bool):
            raise ValueError("开机自启开关必须是布尔值。")
        import winreg

        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.registry_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, self.command_line)
            return

        try:
            access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.registry_path, 0, access) as key:
                value, kind = winreg.QueryValueEx(key, self.value_name)
                if kind == winreg.REG_SZ and self._owns_command(value):
                    winreg.DeleteValue(key, self.value_name)
        except FileNotFoundError:
            # Missing key/value and another process already deleting it are
            # equivalent to disabled. Access-denied errors deliberately escape.
            return
