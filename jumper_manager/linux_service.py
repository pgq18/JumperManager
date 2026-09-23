"""User-scoped systemd integration. No sudo and no changes to login lingering."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def unit_quote(value: str, *, environment: bool = True) -> str:
    """Quote one systemd argument, not a shell command."""
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("服务路径和参数不能包含换行或控制字符。")
    if environment:
        value = value.replace('$', '$$')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


class LinuxUserService:
    def __init__(self, root: Path, command: list[str] | None = None, *, config_home: Path | None = None):
        self.root = root.resolve()
        fingerprint = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        self.name = f"jumper-manager-{fingerprint}.service"
        config = config_home or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not config.is_absolute():
            raise ValueError("XDG_CONFIG_HOME 必须是绝对路径。")
        self.path = config / "systemd" / "user" / self.name
        self.command = command
        self.owner = "# JumperManager installation: " + str(self.root)

    def _run(self, *arguments, check=True):
        binary = shutil.which("systemctl")
        if not sys.platform.startswith("linux") or binary is None:
            raise RuntimeError("此功能需要 Linux systemd；仍可使用 start/stop 手动后台运行。")
        result = subprocess.run([binary, "--user", *arguments], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=50)
        if check and result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "无法连接当前用户的 systemd。")
        return result

    def owned(self):
        try:
            return self.owner in self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return False

    def exists(self):
        if self.path.exists() and not self.owned():
            raise RuntimeError(f"服务文件不是此安装创建的，未改动：{self.path}")
        return self.owned()

    def is_enabled(self):
        return self.exists() and self._run("is-enabled", self.name, check=False).stdout.strip() == "enabled"

    def is_active(self):
        return self.exists() and self._run("is-active", self.name, check=False).stdout.strip() in {"active", "activating", "deactivating"}

    def settings(self):
        if not self.exists():
            return None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# Launch arguments: "):
                return json.loads(line.removeprefix("# Launch arguments: "))
        raise RuntimeError("自启服务配置不完整，请重新执行 autostart enable。")

    def enable(self):
        if not self.command:
            raise ValueError("缺少服务启动命令。")
        self.exists()
        unit_quote(str(self.root))  # Reject controls; WorkingDirectory is not an argv field.
        content = "\n".join([
            self.owner, "# Launch arguments: " + json.dumps(self.command, ensure_ascii=True),
            "[Unit]", "Description=JumperManager port mapping manager", "After=network.target", "",
            "[Service]", "Type=simple", "WorkingDirectory=" + str(self.root).replace("%", "%%"),
            "ExecStart=:" + " ".join(unit_quote(value, environment=False) for value in self.command),
            "Environment=PYTHONUNBUFFERED=1", "UMask=0077", "Restart=on-failure", "RestartSec=3",
            "KillMode=control-group", "TimeoutStopSec=45", "", "[Install]", "WantedBy=default.target", "",
        ])
        # Confirm the user's service manager is reachable before writing.
        self._run("show-environment")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            temporary.chmod(0o600)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._run("daemon-reload")
        self._run("enable", self.name)

    def disable(self):
        if self.exists():
            self._run("disable", self.name)

    def start(self):
        if not self.exists():
            raise RuntimeError("自启服务尚未配置。")
        self._run("start", self.name)

    def stop(self):
        if self.exists():
            self._run("stop", self.name)

    def status(self):
        present = self.exists()
        result = {"installed": present, "enabled": False, "active": False, "unit": self.name, "path": str(self.path), "linger": None}
        if present:
            enabled = self._run("is-enabled", self.name, check=False)
            active = self._run("is-active", self.name, check=False)
            if enabled.returncode not in {0, 1} or active.returncode not in {0, 3}:
                result["error"] = (enabled.stderr or active.stderr).strip()
            result.update(enabled=enabled.stdout.strip() == "enabled", active=active.stdout.strip() in {"active", "activating"})
        binary = shutil.which("loginctl")
        if binary and hasattr(os, "getuid"):
            linger = subprocess.run([binary, "show-user", str(os.getuid()), "-p", "Linger", "--value"],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
            if linger.returncode == 0 and linger.stdout.strip() in {"yes", "no"}:
                result["linger"] = linger.stdout.strip() == "yes"
        return result


def run(argv, *, root, command_builder):
    parser = argparse.ArgumentParser(prog="jumper-manager autostart", description="设置 Linux 用户级自动启动")
    parser.add_argument("action", choices=["enable", "disable", "status"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ssh-config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间。")
    if not sys.platform.startswith("linux"):
        parser.error("Linux 自启设置请在 Linux 上运行；Windows 可使用托盘菜单。")
    try:
        service = LinuxUserService(root, command_builder(args))
        if args.action == "enable":
            service.enable()
        elif args.action == "disable":
            service.disable()
        result = service.status()
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("自动启动：" + ("已开启" if result["enabled"] else "已关闭"))
            print("服务：" + result["unit"])
            if args.action == "enable":
                print("已保存，下次启动应用；当前运行的映射不受影响。")
            if result["enabled"]:
                print("触发时机：" + ("系统开机（已开启用户驻留）" if result["linger"] else "用户登录后"))
            if result.get("error"):
                print(result["error"], file=sys.stderr)
        return 1 if result.get("error") else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if args.json:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
        else:
            print(str(error), file=sys.stderr)
        return 1
