"""Install a verified update with a detached, installation-scoped helper.

The helper runs a copy of the current executable, so Windows can replace the
installed EXE after its tray has shut down. Persistent application data is never
replaced. A failed startup restores the previous executable and running tunnels.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener

from jumper_manager.linux_service import LinuxUserService

OPENER = build_opener(ProxyHandler({}))
FINAL = {"success", "error"}
STOP_TIMEOUT = 90
START_TIMEOUT = 90
READY_TIMEOUT = 90


class InstallError(RuntimeError):
    pass


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise InstallError("更新状态文件过大。")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InstallError("更新状态文件无效。")
    return value


def _write(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.chmod(0o600)
    _move_with_retry(temp, path, timeout=5)


class _Lock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+b")
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
            self.close()
            raise InstallError("此安装正在运行另一个操作，请稍后再试。") from None
        return self

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


def _instance_free(root):
    lock = _Lock(root / "data" / "instance.lock")
    try:
        lock.acquire()
        return True
    except InstallError:
        return False
    finally:
        lock.close()


def _request(url, body=None, token=None, timeout=4):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Jumper-Token"] = token
    req = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with OPENER.open(req, timeout=timeout) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise InstallError("管理器返回了无效状态。")
    return value


def _running(root):
    try:
        runtime = _read(root / "data" / "server.json")
    except FileNotFoundError:
        return None
    port = runtime.get("port")
    if type(port) is not int or not 1 <= port <= 65535 or not runtime.get("instance_id"):
        raise InstallError("管理器运行状态无效，未改动程序。")
    url = f"http://127.0.0.1:{port}"
    try:
        ping = _request(url + "/api/ping")
    except (OSError, URLError):
        return None
    if ping.get("app") != "JumperManager" or ping.get("instance_id") != runtime["instance_id"]:
        raise InstallError("管理器实例已改变，未停止其他程序。")
    return {**runtime, **ping, "port": port, "url": url}


def _service(root):
    return LinuxUserService(root) if sys.platform.startswith("linux") else None


def _snapshot(root):
    active = _running(root)
    service = _service(root)
    service_active = bool(service and service.is_active())
    if not active:
        if not _instance_free(root) or service_active:
            raise InstallError("管理器正在启动或退出，请稍后再更新。")
        return {"runtime": None, "active": [], "service": False}
    state = _request(active["url"] + "/api/state")
    mappings = state.get("mappings", [])
    if any(item.get("status") in {"starting", "stopping"} for item in mappings):
        raise InstallError("有映射正在启动或停止，请完成后再更新。")
    current = _running(root)
    if not current or current["instance_id"] != active["instance_id"]:
        raise InstallError("管理器实例已改变，请重新检查更新。")
    return {"runtime": active, "service": service_active,
            "active": [{"id": item["id"], "name": item.get("name", item["id"]),
                        "ssh_timeout": item.get("ssh_timeout", 30)} for item in mappings
                       if item.get("status") in {"running", "degraded"}]}


def _within(path, root):
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise InstallError("更新路径不属于此安装。") from None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise InstallError("更新路径不能包含符号链接。")
    if path.resolve() != path:
        raise InstallError("更新路径不是规范路径。")
    return path


def _spawn_options(root):
    options = {"cwd": str(root), "stdin": subprocess.DEVNULL,
               "env": {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return options


def prepare_install(root: Path, executable: Path, version: str) -> Path:
    """Stage an already checksum-verified executable without stopping the app."""
    if not getattr(sys, "frozen", False):
        raise InstallError("源码运行请更新源码；自动安装仅适用于打包版本。")
    root = Path(root).resolve()
    target = Path(sys.executable).resolve()
    if target.parent != root:
        raise InstallError("当前可执行文件不属于此安装目录。")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise InstallError("更新版本无效。")
    executable = Path(executable).resolve(strict=True)
    updates = _within(root / "data" / "updates", root)
    updates.mkdir(parents=True, exist_ok=True)
    folder = updates / secrets.token_hex(16)
    folder.mkdir(mode=0o700)
    suffix = ".exe" if os.name == "nt" else ""
    candidate, helper = folder / ("candidate" + suffix), folder / ("helper" + suffix)
    try:
        shutil.copy2(executable, candidate)
        shutil.copy2(target, helper)
        candidate.chmod(0o700)
        helper.chmod(0o700)
        from jumper_manager.update_release import is_package_file
        files = []
        for source in sorted(executable.parent.rglob("*")):
            relative = source.relative_to(executable.parent).as_posix()
            if source == executable or not is_package_file(relative, "windows" if os.name == "nt" else "linux"):
                continue
            if source.is_symlink() or not source.is_file():
                raise InstallError("安装包包含非普通文件。")
            destination = _within(root / relative, root)
            staged = folder / "package" / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staged)
            files.append({"path": relative, "sha256": _hash(staged),
                          "previous_sha256": _hash(destination) if destination.exists() else None})
        snapshot = _snapshot(root)
        from jumper_manager import __version__
        plan = {"schema": 1, "root": str(root), "target": str(target), "candidate": str(candidate),
                "helper": str(helper), "backup": str(folder / ("previous" + suffix)),
                "sha256": _hash(candidate), "previous_sha256": _hash(target),
                "previous_version": __version__, "version": version, "snapshot": snapshot,
                "files": files, "created": time.time()}
        path = folder / "plan.json"
        _write(path, plan)
        _status(path, "prepared", "更新已准备。", ready=False)
        return path
    except Exception:
        # Only remove this exact newly-created staging directory, never user data.
        shutil.rmtree(folder)
        raise


def _validate(path, *, helper=False):
    path = Path(path).resolve(strict=True)
    plan = _read(path)
    root = Path(plan["root"])
    if root != root.resolve() or plan.get("schema") != 1:
        raise InstallError("更新计划无效。")
    folder = path.parent
    if (path.name != "plan.json" or folder.parent != root / "data" / "updates"
            or not re.fullmatch(r"[0-9a-f]{32}", folder.name)):
        raise InstallError("更新计划位置无效。")
    _within(path, root)
    suffix = ".exe" if os.name == "nt" else ""
    for field, name in (("candidate", "candidate" + suffix), ("helper", "helper" + suffix),
                        ("backup", "previous" + suffix)):
        if Path(plan[field]) != folder / name:
            raise InstallError("更新计划路径无效。")
        _within(Path(plan[field]), root)
    target = _within(Path(plan["target"]), root)
    if target.parent != root:
        raise InstallError("更新目标不属于此安装。")
    if _hash(plan["candidate"]) != plan["sha256"] or _hash(plan["helper"]) != plan["previous_sha256"]:
        raise InstallError("更新文件校验失败，原程序未改动。")
    if helper and Path(sys.executable).resolve() != Path(plan["helper"]):
        raise InstallError("更新助手位置无效。")
    from jumper_manager.update_release import is_package_file
    seen = set()
    for item in plan.get("files", []):
        relative = item["path"]
        if (relative in seen or not is_package_file(relative, "windows" if os.name == "nt" else "linux")
                or relative in {target.name, "JumperManager.exe", "jumper-manager"}):
            raise InstallError("安装包文件清单无效。")
        seen.add(relative)
        _within(root / relative, root)
        staged = _within(folder / "package" / relative, root)
        if _hash(staged) != item["sha256"]:
            raise InstallError("安装包文件校验失败。")
    return plan


def _validate_current_files(data):
    root = Path(data["root"])
    for item in data.get("files", []):
        destination = _within(root / item["path"], root)
        actual = _hash(destination) if destination.exists() else None
        if actual != item["previous_sha256"]:
            raise InstallError(f"文件 {item['path']} 已改变，请重新检查更新。")


def _install_files(path, data):
    root = Path(data["root"])
    data["_files_replaced"] = []
    for item in data.get("files", []):
        destination = _within(root / item["path"], root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = path.parent / "original" / item["path"]
        if item["previous_sha256"] is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        temporary = destination.with_name(destination.name + ".update-" + path.parent.name)
        _within(temporary, root)
        try:
            shutil.copy2(path.parent / "package" / item["path"], temporary)
            if _hash(temporary) != item["sha256"]:
                raise InstallError("安装包文件复制校验失败。")
            data["_files_replaced"].append(item)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def _rollback_files(path, data):
    root = Path(data["root"])
    for item in reversed(data.get("_files_replaced", [])):
        destination = _within(root / item["path"], root)
        actual = _hash(destination) if destination.exists() else None
        if actual == item["previous_sha256"]:
            continue
        if actual != item["sha256"]:
            raise InstallError(f"文件 {item['path']} 被其他操作改变，未覆盖它。")
        if item["previous_sha256"] is None:
            destination.unlink()
        else:
            backup = path.parent / "original" / item["path"]
            if _hash(backup) != item["previous_sha256"]:
                raise InstallError("旧版附带文件备份校验失败。")
            backup.replace(destination)


def _status(path, status, message, **extra):
    value = {"status": status, "message": message, "ready": True,
             "plan_path": str(path), "status_path": str(path.parent / "status.json"),
             "updated": time.time(), **extra}
    _write(path.parent / "status.json", value)
    return value


def launch_install(plan: Path, *, wait=False) -> dict:
    plan = Path(plan).resolve(strict=True)
    data = _validate(plan)
    if _hash(data["target"]) != data["previous_sha256"]:
        raise InstallError("安装文件已改变，请重新检查更新。")
    with (plan.parent / "helper.log").open("ab") as log:
        process = subprocess.Popen([data["helper"], "--apply-update", str(plan)],
                                   stdout=log, stderr=log, **_spawn_options(Path(data["root"])))
    active = data["snapshot"]["active"]
    restore_time = sum(max(300, 8 * int(item.get("ssh_timeout", 30)) + 60) for item in active)
    deadline = time.monotonic() + (2 * (STOP_TIMEOUT + START_TIMEOUT + restore_time) + 180 if wait else READY_TIMEOUT)
    while time.monotonic() < deadline:
        state = _read(plan.parent / "status.json")
        if state["status"] in FINAL or (not wait and state.get("ready")):
            return state
        if process.poll() is not None:
            # The helper can publish its final status between our first read
            # and poll; a completed installation must not be reported failed.
            state = _read(plan.parent / "status.json")
            if state.get("status") in FINAL:
                return state
            raise InstallError("更新助手意外退出。详情：" + str(plan.parent / "helper.log"))
        time.sleep(0.2)
    raise InstallError("更新尚未结束，请查看：" + str(plan.parent / "status.json"))


def _probe(path, data):
    result_path = path.parent / "probe.json"
    result_path.unlink(missing_ok=True)
    with (path.parent / "helper.log").open("ab") as log:
        result = subprocess.run([data["candidate"], "--update-probe", str(result_path)],
                                stdout=log, stderr=log, timeout=60, **_spawn_options(Path(data["root"])))
    if result.returncode or not result_path.exists():
        raise InstallError("新版本无法在此系统运行，原程序未改动。")
    metadata = _read(result_path)
    if metadata.get("app") != "JumperManager" or metadata.get("version") != data["version"]:
        raise InstallError("新版本身份或版本号不匹配，原程序未改动。")


def _same_instance(root, expected):
    current = _running(root)
    if current and (not expected or current["instance_id"] != expected["instance_id"]):
        raise InstallError("管理器实例已改变，未停止其他实例。")
    if not current and not _instance_free(root):
        raise InstallError("管理器正在切换状态，请稍后重试。")
    return current


def _stop(root, expected, service_active, timeout=STOP_TIMEOUT):
    current = _same_instance(root, expected)
    if current:
        if service_active:
            _service(root).stop()
        else:
            token = _request(current["url"] + "/api/session")["token"]
            _request(current["url"] + "/api/shutdown", {}, token)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = _running(root)
        if active and expected and active["instance_id"] != expected["instance_id"]:
            raise InstallError("另一实例已启动，更新已停止。")
        if not active and _instance_free(root):
            return
        time.sleep(0.2)
    raise InstallError("程序仍在清理映射，未替换程序文件。请稍后重试。")


def resume_identity(root: Path) -> str | None:
    """Return the update's restart nonce without consuming its instruction."""
    marker = Path(root) / "data" / "update-resume.json"
    if not marker.exists():
        return None
    try:
        value = _read(marker)
        path = Path(value["plan"])
        data = _validate(path)
        if (Path(data["root"]) != Path(root).resolve() or time.time() - value["created"] > 300
                or _hash(data["target"]) != value["sha256"]
                or value["sha256"] not in {data["sha256"], data["previous_sha256"]}
                or _read(path.parent / "status.json").get("status") in FINAL):
            return None
        return path.parent.name
    except (OSError, ValueError, KeyError, InstallError):
        return None


def consume_resume(root: Path) -> bool:
    """Consume a verified one-shot instruction for a systemd-driven restart."""
    marker = Path(root) / "data" / "update-resume.json"
    try:
        return bool(resume_identity(root))
    finally:
        marker.unlink(missing_ok=True)


def _start(path, data, *, rollback=False):
    root, snapshot = Path(data["root"]), data["snapshot"]
    original = snapshot["runtime"]
    if not original:
        return None
    _same_instance(root, None)
    expected_version = data["previous_version"] if rollback else data["version"]
    _write(root / "data" / "update-resume.json", {"plan": str(path), "created": time.time(),
           "sha256": data["previous_sha256"] if rollback else data["sha256"]})
    if snapshot["service"]:
        _service(root).start()
        process = None
    else:
        command = [data["target"], "--serve", "--no-browser", "--update-resume", "--update-id", path.parent.name,
                   "--port", str(original["port"])]
        if original.get("ssh_config"):
            command += ["--ssh-config", original["ssh_config"]]
        if os.name == "nt":
            command += ["--tray" if original.get("tray_ready") else "--no-tray"]
        with (root / "data" / "launcher.log").open("ab") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, **_spawn_options(root))
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        current = _running(root)
        if current:
            if current.get("update_id") != path.parent.name:
                raise InstallError("出现其他运行实例，未接管它。")
            data["_started_runtime"] = current
            if current.get("version") != expected_version or current["port"] != original["port"]:
                raise InstallError("重启后的版本或端口与预期不一致。")
            if original.get("tray_ready") and not current.get("tray_ready"):
                time.sleep(0.2)
                continue
            return current
        if process and process.poll() is not None:
            raise InstallError("新程序未能启动，请查看 data/launcher.log。")
        time.sleep(0.2)
    raise InstallError("等待程序启动超时。")


def _restore(data, runtime):
    if not runtime:
        return []
    warnings = []
    token = _request(runtime["url"] + "/api/session")["token"]
    for entry in data["snapshot"]["active"]:
        try:
            current = _running(Path(data["root"]))
            if not current or current["instance_id"] != runtime["instance_id"]:
                raise InstallError("恢复映射时管理器实例发生变化。")
            state = _request(runtime["url"] + "/api/state")
            mapping = next((item for item in state["mappings"] if item["id"] == entry["id"]), None)
            if mapping is None:
                raise InstallError("映射已被删除。")
            if mapping["status"] in {"running", "degraded", "starting"}:
                continue
            _request(runtime["url"] + "/api/mappings/" + quote(entry["id"], safe="") + "/start", {}, token,
                     timeout=max(300, int(entry.get("ssh_timeout", 30)) * 8 + 60))
        except (OSError, ValueError, KeyError, InstallError) as error:
            warnings.append(f"{entry['name']} 未能恢复：{error}")
    return warnings


def _move_with_retry(source, destination, *, timeout=STOP_TIMEOUT):
    deadline = time.monotonic() + timeout
    while True:
        try:
            Path(source).replace(destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _failure_notice(message):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "JumperManager 更新未完成", 0x10)


def apply_install(plan: Path) -> int:
    """Entry point used only by the copied frozen executable."""
    path = Path(plan).resolve()
    lock = None
    data = None
    replaced = False
    stopped = False
    shutdown_requested = False
    installed_runtime = None
    replacement = None
    owns_plan = False
    try:
        data = _validate(path, helper=True)
        root = Path(data["root"])
        lock = _Lock(root / "data" / "update.lock").acquire()
        prior = _read(path.parent / "status.json")
        if prior["status"] != "prepared":
            raise InstallError("此更新计划已执行，请重新检查更新。")
        owns_plan = True
        if _hash(data["target"]) != data["previous_sha256"]:
            raise InstallError("程序文件已改变，请重新检查更新。")
        _validate_current_files(data)
        _status(path, "running", "正在验证新程序。")
        _probe(path, data)
        # Revalidate after probing and immediately before touching the live app.
        _validate(path, helper=True)
        if _hash(data["target"]) != data["previous_sha256"]:
            raise InstallError("程序文件已改变，请重新检查更新。")
        _validate_current_files(data)
        original = data["snapshot"]["runtime"]
        _same_instance(root, original)
        # Preserve changes made while the candidate was being validated.
        fresh = _snapshot(root)
        if (bool(fresh["runtime"]) != bool(original)
                or original and fresh["runtime"]["instance_id"] != original["instance_id"]):
            raise InstallError("管理器运行状态已改变，请重新检查更新。")
        data["snapshot"] = fresh
        _write(path, data)
        _status(path, "running", "正在保存运行状态并退出旧版本。")
        shutdown_requested = bool(original)
        stop_timeout = max([STOP_TIMEOUT, *[int(item["ssh_timeout"]) + 60 for item in fresh["active"]]])
        _stop(root, original, data["snapshot"]["service"], timeout=stop_timeout)
        stopped = bool(original)
        replacement = root / (".jumper-update-" + path.parent.name)
        _within(replacement, root)
        shutil.copy2(data["candidate"], replacement)
        if _hash(replacement) != data["sha256"]:
            raise InstallError("更新文件复制校验失败。")
        _move_with_retry(data["target"], data["backup"])
        replaced = True
        _move_with_retry(replacement, data["target"])
        _install_files(path, data)
        _status(path, "running", "已替换程序，正在恢复原来的运行状态。")
        installed_runtime = _start(path, data)
        warnings = _restore(data, installed_runtime)
        _status(path, "success", f"已更新到 {data['version']}。" + ("部分映射恢复失败，请查看详情。" if warnings else ""),
                version=data["version"], warnings=warnings, running=bool(installed_runtime))
        if warnings and os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                f"已更新到 {data['version']}。\n\n以下映射未能恢复，请在管理界面查看或重试：\n" + "\n".join(warnings),
                "JumperManager · 部分映射未恢复", 0x30)
        return 0
    except Exception as error:
        rollback_error = None
        warnings = []
        if data and (stopped or replaced):
            try:
                root = Path(data["root"])
                current = _running(root)
                if current:
                    # Only touch the instance started by this transaction. A
                    # concurrent manual launch is not ours to shut down.
                    owned_runtime = installed_runtime or data.get("_started_runtime")
                    if not owned_runtime or current["instance_id"] != owned_runtime["instance_id"]:
                        raise InstallError("出现其他运行实例，未停止它；旧程序保留在备份目录。")
                    _stop(root, current, data["snapshot"]["service"])
                if replaced:
                    if Path(data["target"]).exists() and _hash(data["target"]) != data["sha256"]:
                        raise InstallError("安装文件被其他操作改变，未覆盖它。")
                    _move_with_retry(data["backup"], data["target"])
                    _rollback_files(path, data)
                previous_runtime = _start(path, data, rollback=True)
                warnings = _restore(data, previous_runtime)
            except Exception as recovery_error:
                rollback_error = str(recovery_error)
        message = str(error)
        if replaced and not rollback_error:
            message += " 已恢复原版本。"
        if rollback_error:
            message += " 自动恢复未完成：" + rollback_error
        elif shutdown_requested and not stopped:
            message += " 已请求退出旧程序；若它清理完成后退出，请手动重新打开。配置与程序文件均未替换。"
        if data and owns_plan:
            try:
                _status(path, "error", message, warnings=warnings, rollback_error=rollback_error)
                with (path.parent / "helper.log").open("a", encoding="utf-8") as log:
                    log.write(message + "\n")
            except OSError:
                pass
        if stopped or replaced:
            _failure_notice(message)
        return 1
    finally:
        if replacement is not None:
            replacement.unlink(missing_ok=True)
        if data and owns_plan:
            marker = Path(data["root"]) / "data" / "update-resume.json"
            try:
                if marker.exists() and _read(marker).get("plan") == str(path):
                    marker.unlink()
            except (OSError, ValueError):
                pass
        if lock:
            lock.close()
