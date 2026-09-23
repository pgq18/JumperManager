"""JumperManager entry point: launch, serve, status and graceful shutdown."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler
import webbrowser

BUNDLE_ROOT = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))
WINDOWS = os.name == "nt"
# Bundled web assets live in PyInstaller's extraction directory. Persistent
# mappings stay beside the EXE and must never be written into that directory.
ROOT = Path(sys.executable).resolve().parent if FROZEN else BUNDLE_ROOT
DATA = ROOT / "data"
RUNTIME = DATA / "server.json"
OPENER = build_opener(ProxyHandler({}))


class InstanceAlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """OS-released lock across all WebUI ports for this installation."""

    def __init__(self):
        self.file = None

    def acquire(self):
        self.file = (DATA / "instance.lock").open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise InstanceAlreadyRunning("此目录已有 JumperManager 实例正在运行；请使用 --status 查看地址。") from None

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


def request(url, body=None, token=None, timeout=3, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Jumper-Token"] = token
    req = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def running():
    try:
        state = json.loads(RUNTIME.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("port"), int):
            return None
        port = int(state["port"])
        if not 1 <= port <= 65535:
            return None
        url = f"http://127.0.0.1:{port}"
        ping = request(url + "/api/ping")
        if isinstance(ping, dict) and ping.get("app") == "JumperManager" and ping.get("instance_id") == state.get("instance_id"):
            return {**state, "url": url}
    except (OSError, ValueError, KeyError, URLError):
        pass
    return None


def configure_logging():
    DATA.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(DATA / "server.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def show_message(message, *, error=False):
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(message, file=stream)
    elif os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "JumperManager", 0x10 if error else 0x40)


def prepare_frozen_runtime():
    if FROZEN and os.name == "nt":
        # Load bundled binary extensions before restoring the system DLL search
        # path, so external ssh.exe inherits its normal Windows environment.
        import ssl
        import pystray
        from PIL import Image
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)


def autostart_command(args):
    """Register the installed path, never the one-file extraction directory."""
    executable = Path(sys.executable).resolve()
    if FROZEN:
        command = [str(executable)]
    else:
        if WINDOWS and executable.with_name("pythonw.exe").exists():
            executable = executable.with_name("pythonw.exe")
        command = [str(executable), str(BUNDLE_ROOT / "app.py"), "--serve"]
    command += ["--tray", "--no-browser", "--port", str(args.port)]
    if args.ssh_config:
        command += ["--ssh-config", str(Path(args.ssh_config).expanduser().resolve())]
    return command


def serve(args):
    from jumper_manager.engine import Manager
    from jumper_manager.server import AppServer

    if not WINDOWS:
        os.umask(0o077)
    configure_logging()
    identity = {"app": "JumperManager", "instance_id": secrets.token_hex(16), "pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "desktop": FROZEN and WINDOWS, "tray_ready": False}
    # Lock and bind before constructing Manager; another launch must never
    # recover or clean up the first instance's tunnels.
    server = None
    manager = None
    tray = None
    instance_lock = InstanceLock()
    try:
        instance_lock.acquire()
        server = AppServer(("127.0.0.1", args.port), None, BUNDLE_ROOT / "web", identity)
        manager = Manager(ROOT, config_path=args.ssh_config)
        server.manager = manager

        def shutdown_signal(*_):
            server.shutting_down = True
            threading.Thread(target=server.shutdown, daemon=True).start()

        if getattr(args, "tray", False):
            from jumper_manager.tray import TrayController
            tray = TrayController(f"http://127.0.0.1:{server.server_address[1]}", DATA,
                                  manager.state, shutdown_signal)
            from jumper_manager.autostart import WindowsAutostart
            try:
                tray.autostart = WindowsAutostart(autostart_command(args))
            except ValueError:
                logging.exception("Automatic startup is unavailable for these launch arguments")
            tray.start()
            identity["tray_ready"] = True

        runtime = {**identity, "port": server.server_address[1], "ssh_config": str(Path(args.ssh_config).expanduser().resolve()) if args.ssh_config else None}
        temporary = RUNTIME.with_suffix(".tmp")
        temporary.write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(RUNTIME)
        logging.info("Serving http://127.0.0.1:%s (tray=%s)", runtime["port"], identity["tray_ready"])

        signal.signal(signal.SIGINT, shutdown_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, shutdown_signal)

        def auto_start():
            for mapping in manager.state()["mappings"]:
                if server.shutting_down:
                    break
                if mapping.get("auto_start"):
                    try:
                        manager.start(mapping["id"])
                    except Exception:
                        logging.exception("Auto start failed for %s", mapping["id"])

        threading.Thread(target=auto_start, daemon=True, name="mapping-autostart").start()
        if args.open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{runtime['port']}")).start()
        server.serve_forever(poll_interval=0.25)
    finally:
        if server is not None:
            server.shutting_down = True
            try:
                server.server_close()
            except Exception:
                logging.exception("HTTP cleanup failed")
        try:
            if manager is not None:
                manager.close()
        except Exception:
            logging.exception("Mapping cleanup failed")
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                logging.exception("Tray cleanup failed")
        try:
            saved = json.loads(RUNTIME.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("instance_id") == identity["instance_id"]:
                RUNTIME.unlink()
        except (OSError, ValueError):
            pass
        instance_lock.close()
        logging.info("JumperManager stopped")


def launch(args):
    existing = running()
    if existing:
        print("JumperManager is running: " + existing["url"])
        if not args.no_browser:
            webbrowser.open(existing["url"])
        return 0
    if FROZEN and WINDOWS:
        # A windowed EXE already has no terminal; keep one serving process with
        # a tray rather than spawning another copy as though it were Python.
        args.open_browser = not args.no_browser
        try:
            serve(args)
        except InstanceAlreadyRunning:
            # Two double-clicks can race before the first runtime file exists.
            for _ in range(100):
                existing = running()
                if existing:
                    if not args.no_browser:
                        webbrowser.open(existing["url"])
                    return 0
                time.sleep(0.2)
            raise
        return 0
    service = linux_service()
    if service is not None and service.is_enabled():
        saved = service.settings()
        if getattr(args, "port_explicit", False) and str(args.port) != saved[saved.index("--port") + 1]:
            raise RuntimeError("自启服务使用其他端口，请先用 autostart enable --port 更新设置。")
        if args.ssh_config:
            config = str(Path(args.ssh_config).expanduser().resolve())
            if "--ssh-config" not in saved or config != saved[saved.index("--ssh-config") + 1]:
                raise RuntimeError("请先用 autostart enable --ssh-config 更新自启服务配置。")
        service.start()
        for _ in range(100):
            active = running()
            if active:
                print("JumperManager 已启动：" + active["url"])
                if not args.no_browser:
                    webbrowser.open(active["url"])
                return 0
            time.sleep(0.2)
        raise RuntimeError("服务未就绪，请查看 data/server.log 或 journalctl --user -u " + service.name)
    DATA.mkdir(parents=True, exist_ok=True)
    executable = Path(sys.executable)
    if WINDOWS and not FROZEN and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    command = ([str(executable)] if FROZEN else [str(executable), str(BUNDLE_ROOT / "app.py")]) + ["--serve", "--port", str(args.port)]
    if args.ssh_config:
        command += ["--ssh-config", str(Path(args.ssh_config).expanduser().resolve())]
    if getattr(args, "tray", False):
        command.append("--tray")
    kwargs = {"cwd": str(ROOT), "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    if FROZEN and not WINDOWS:
        # The detached service must own its extraction directory after this
        # short-lived CLI invocation exits (PyInstaller onefile protocol).
        kwargs["env"] = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    with (DATA / "launcher.log").open("ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, **kwargs)
    for _ in range(100):
        active = running()
        if active:
            print("JumperManager started: " + active["url"])
            if not args.no_browser:
                webbrowser.open(active["url"])
            return 0
        if process.poll() is not None:
            print("Startup failed. See data/launcher.log and data/server.log.", file=sys.stderr)
            return 1
        time.sleep(0.2)
    print("Startup is taking longer than expected. Run --status or see data/server.log.", file=sys.stderr)
    return 1


def linux_service():
    if WINDOWS or not sys.platform.startswith("linux"):
        return None
    from jumper_manager.linux_service import LinuxUserService
    return LinuxUserService(ROOT)


def service_command(args):
    command = [str(Path(sys.executable).resolve())]
    if not FROZEN:
        command.append(str(BUNDLE_ROOT / "app.py"))
    command += ["--serve", "--no-tray", "--no-browser", "--port", str(args.port)]
    if args.ssh_config:
        command += ["--ssh-config", str(Path(args.ssh_config).expanduser().resolve())]
    return command


def stop_background():
    active = running()
    service = linux_service()
    if service is not None and service.is_active():
        service.stop()
    elif active:
        token = request(active["url"] + "/api/session")["token"]
        request(active["url"] + "/api/shutdown", {}, token)
    else:
        print("JumperManager 未启动。")
        return 0
    for _ in range(120):
        if not RUNTIME.exists():
            print("JumperManager 已停止。")
            return 0
        time.sleep(0.25)
    print("已请求停止，但资源仍在清理，请稍后运行 status 查看。", file=sys.stderr)
    return 1


def main():
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if FROZEN and not WINDOWS:
        # Restore the system loader path for ssh/systemctl child processes.
        # The current bundled interpreter is already loaded by the bootloader.
        original = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = original
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--json" and len(arguments) > 1:
        arguments = arguments[1:] + ["--json"]
    if arguments and arguments[0] == "list":
        arguments = ["mappings", "list", *arguments[1:]]
    if arguments and arguments[0] in {"hosts", "mappings", "logs"}:
        from jumper_manager.cli import run
        return run(arguments, running=running, request=request, data=DATA)
    if arguments and arguments[0] == "autostart":
        from jumper_manager.linux_service import run
        return run(arguments[1:], root=ROOT, command_builder=service_command)
    parser = argparse.ArgumentParser(description="JumperManager - SSH 端口映射管理器", allow_abbrev=False,
        epilog="list 查看所有映射；start/stop 名称或ID 控制单条映射，不带名称时控制管理器。更多命令：hosts / mappings / logs / autostart。")
    parser.add_argument("command", nargs="?", choices=["list", "start", "stop", "restart", "status", "serve", "open"], help="启动、停止、重启、状态、前台运行或打开 WebUI")
    parser.add_argument("tunnel", nargs="?", metavar="TUNNEL", help="start/stop 的映射名称、完整 ID 或唯一 ID 前缀；省略时控制管理器")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="Run server in foreground")
    mode.add_argument("--launch", "--start", action="store_true", help="Start server in background (default)")
    mode.add_argument("--status", action="store_true", help="Check background server")
    mode.add_argument("--stop", action="store_true", help="Stop server and its managed mappings")
    mode.add_argument("--restart", action="store_true", help="Restart background server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ssh-config", help="Use a different SSH config file")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--open", dest="open_browser", action="store_true")
    parser.add_argument("--json", action="store_true", help="Machine-readable status")
    from jumper_manager import __version__
    parser.add_argument("--version", action="version", version="JumperManager " + __version__)
    tray_mode = parser.add_mutually_exclusive_group()
    tray_mode.add_argument("--tray", action="store_true", help="Show a Windows notification-area icon")
    tray_mode.add_argument("--no-tray", dest="tray", action="store_false", help="Run without a tray icon")
    parser.set_defaults(tray=FROZEN and WINDOWS)
    args = parser.parse_args(arguments)
    if args.command and any((args.serve, args.launch, args.status, args.stop, args.restart)):
        parser.error("请只指定一种启动或停止操作。")
    if args.tunnel is not None:
        if args.command not in {"start", "stop"}:
            parser.error("只有 start/stop 支持指定单条映射。")
        before_literal = arguments[:arguments.index("--")] if "--" in arguments else arguments
        server_options = {"--port", "--ssh-config", "--no-browser", "--open", "--tray", "--no-tray"}
        if any(value.split("=", 1)[0] in server_options for value in before_literal):
            parser.error("单条映射的 start/stop 仅接受名称或 ID 和 --json；管理器启动参数请单独使用。")
        from jumper_manager.cli import run
        selection = (["--"] if args.tunnel.startswith("-") else []) + [args.tunnel]
        forwarded = (["--json"] if args.json else []) + ["mappings", args.command, *selection]
        return run(forwarded, running=running, request=request, data=DATA)
    if args.command:
        setattr(args, {"start": "launch", "open": "launch"}.get(args.command, args.command), True)
        if args.command == "open":
            args.open_browser = True
    args.port_explicit = any(value == "--port" or value.startswith("--port=") for value in arguments)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not WINDOWS:
        if args.tray:
            parser.error("Linux 使用命令行和 WebUI，无需 --tray。")
        args.no_browser = args.no_browser or not args.open_browser
        if args.open_browser and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            args.open_browser = False
            args.no_browser = True
            print("当前无图形桌面，请通过 SSH 端口转发在电脑浏览器中打开下方地址。")
    if args.status:
        active = running()
        if args.json:
            print(json.dumps({"running": bool(active), "url": active["url"] if active else None,
                              "pid": active["pid"] if active else None}, ensure_ascii=False))
        else:
            show_message("已启动：" + active["url"] if active else "已停止")
        return 0 if active else 1
    if args.stop:
        return stop_background()
    if args.restart:
        previous = running()
        if previous and not args.port_explicit:
            args.port = previous["port"]
        if previous and not args.ssh_config:
            args.ssh_config = previous.get("ssh_config")
        if stop_background():
            return 1
    if args.serve:
        prepare_frozen_runtime()
        serve(args)
        return 0
    prepare_frozen_runtime()
    return launch(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        logging.exception("Fatal application error")
        show_message(f"启动失败：{error}\n\n日志目录：{DATA}\n请将程序放在有写入权限的文件夹中。", error=True)
        raise SystemExit(1)
