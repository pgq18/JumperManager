"""JumperManager: config-driven, loopback-only, owned SSH port mappings."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import subprocess
import threading
import time
import uuid

from .processes import WindowsJob, hidden_options, identity, terminate_owned
from .ssh_config import ALIAS_RE, discover_aliases, parse_effective_config, route_for, simple_proxy, configuration_signature

VERSION = "1.0"
CONNECT_TIMEOUT = 8
CONFIG_POLL_INTERVAL = 2
SAFE_OPTIONS = ["BatchMode=yes", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                "StrictHostKeyChecking=yes", "UpdateHostKeys=no", "ConnectionAttempts=1",
                "ConnectTimeout=8", "ServerAliveInterval=15", "ServerAliveCountMax=3",
                "ControlMaster=no", "ControlPath=none", "RequestTTY=no",
                "ForkAfterAuthentication=no", "PermitLocalCommand=no", "RemoteCommand=none"]
FIELDS = ("name", "source_host", "source_port", "target_host", "target_port", "bind_address", "target_address", "auto_start")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def address(value, *, bind=False) -> str:
    if not isinstance(value, str):
        raise ValueError("地址必须是字符串。")
    value = value.strip()
    if value == "localhost":
        value = "127.0.0.1"
    if bind:
        if value not in {"127.0.0.1", "::1"}:
            raise ValueError("监听地址仅支持 127.0.0.1 或 ::1，避免把端口暴露到网络。")
        return value
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        if len(value) <= 253 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
            return value
    raise ValueError("目标地址必须是 IP 地址或主机名，不能包含命令或端口。")


def port(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError("端口必须是 1 到 65535 的整数。")
    result = int(value)
    if not 1 <= result <= 65535:
        raise ValueError("端口必须是 1 到 65535 的整数。")
    return result


def forward_spec(bind_address: str, source_port: int, target_address: str, target_port: int) -> str:
    def wrap(item):
        return f"[{item}]" if ":" in item else item
    return f"{wrap(bind_address)}:{source_port}:{wrap(target_address)}:{target_port}"


def local_socket_check(operation: str, host: str, target_port: int) -> dict:
    try:
        if operation == "available":
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            with socket.socket(family) as sock:
                if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                elif os.name != "nt":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, target_port))
                sock.listen(1)
        else:
            with socket.create_connection((host, target_port), timeout=4):
                pass
        return {"ok": True, "message": "端口可绑定" if operation == "available" else "TCP 连接成功"}
    except OSError as exc:
        return {"ok": False, "message": str(exc), "errno": exc.errno}


class LocalRelay:
    """Small protocol-neutral relay for two ports on this PC."""
    def __init__(self, bind_address, source_port, target_address, target_port):
        self.target = (target_address, target_port)
        self.closed = threading.Event()
        self.connections: set[socket.socket] = set()
        self.lock = threading.Lock()
        self.listener = socket.socket(socket.AF_INET6 if ":" in bind_address else socket.AF_INET)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name != "nt":
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((bind_address, source_port))
        self.listener.listen(64)
        self.listener.settimeout(0.5)
        self.thread = threading.Thread(target=self._accept, daemon=True, name="local-port-relay")
        self.thread.start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                incoming, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._copy, args=(incoming,), daemon=True).start()

    def _copy(self, incoming):
        outgoing = None
        try:
            outgoing = socket.create_connection(self.target, timeout=5)
            incoming.settimeout(5)
            outgoing.settimeout(5)
            with self.lock:
                self.connections.update((incoming, outgoing))
            read_sockets = [incoming, outgoing]
            while read_sockets and not self.closed.is_set():
                ready, _, _ = select.select(read_sockets, [], [], 0.5)
                for origin in ready:
                    destination = outgoing if origin is incoming else incoming
                    data = origin.recv(65536)
                    if data:
                        destination.sendall(data)
                    else:
                        read_sockets.remove(origin)
                        destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            with self.lock:
                self.connections.discard(incoming)
                if outgoing:
                    self.connections.discard(outgoing)
            incoming.close()
            if outgoing:
                outgoing.close()

    def close(self):
        self.closed.set()
        self.listener.close()
        with self.lock:
            for sock in self.connections:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        self.thread.join(timeout=1)


class Manager:
    def __init__(self, root: Path, config_path: str | None = None):
        self.root = Path(root).resolve()
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.data / "logs"
        self.logs_dir.mkdir(exist_ok=True)
        self.config_path = Path(config_path).expanduser().resolve() if config_path else Path.home() / ".ssh" / "config"
        self.safe_config = self.data / "ssh_runtime.conf"
        self.ssh = shutil.which("ssh")
        if not self.ssh:
            raise RuntimeError("未找到 OpenSSH 客户端 ssh，请先安装 Windows OpenSSH Client。")
        self._lock = threading.RLock()
        self._refresh_lock = threading.RLock()
        self._mapping_locks: dict[str, threading.RLock] = {}
        self._mappings: dict[str, dict] = {}
        self._processes: dict[str, list[dict]] = {}
        self._relays: dict[str, LocalRelay] = {}
        self._hosts: list[dict] = []
        self._effective: dict[str, dict] = {}
        self._closed = threading.Event()
        self._recovery_warnings: list[str] = []
        self._unrecovered_records: list[dict] = []
        self._config_signature = configuration_signature(self.config_path)
        self._discovery = {"automatic": True, "interval_seconds": CONFIG_POLL_INTERVAL,
                           "revision": 0, "last_refresh": None, "refreshing": False, "error": None}
        self._recover()
        try:
            self.refresh_hosts()
        except RuntimeError:
            # Keep the UI available to explain a bad config and recover as soon
            # as the user saves a valid one, even on the first application start.
            self._hosts = [{"id": "local", "alias": "local", "label": "本机", "hostname": "127.0.0.1", "user": "", "port": None, "route": ["local"], "warning": self._discovery["error"]}]
            self._write_safe_config()
        self._load()
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True, name="ssh-monitor")
        self._monitor_thread.start()
        self._config_thread = threading.Thread(target=self._watch_config, daemon=True, name="ssh-config-watch")
        self._config_thread.start()

    def _run(self, args: list[str], *, input: str | None = None, timeout=20) -> subprocess.CompletedProcess:
        process = subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   errors="replace", **hidden_options())
        job = WindowsJob(process)
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            job.close()
            if process.poll() is None:
                if os.name != "nt":
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
            process.communicate()
            raise RuntimeError(f"SSH 操作超过 {timeout} 秒，请检查设备路由、密钥及 known_hosts。")
        finally:
            job.close()

    def _resolve(self, alias: str) -> dict:
        if alias in self._effective:
            return self._effective[alias]
        result = self._run([self.ssh, "-F", str(self.config_path), "-G", alias], timeout=10)
        if result.returncode:
            raise RuntimeError(f"无法解析 SSH 配置 {alias}：{result.stderr.strip()[-1000:]}")
        value = parse_effective_config(result.stdout)
        self._effective[alias] = value
        return value

    def refresh_hosts(self) -> list[dict]:
        # Resolve potentially slow ssh -G calls off the state lock so the WebUI
        # and stop controls remain responsive with large or broken SSH configs.
        with self._refresh_lock:
            if self._closed.is_set():
                raise RuntimeError("程序正在关闭。")
            with self._lock:
                self._discovery["refreshing"] = True
            signature = configuration_signature(self.config_path)
            effective = {}
            def resolve(alias):
                if alias not in effective:
                    result = self._run([self.ssh, "-F", str(self.config_path), "-G", alias], timeout=10)
                    if result.returncode:
                        raise RuntimeError(f"无法解析 SSH 配置 {alias}：{result.stderr.strip()[-1000:]}")
                    effective[alias] = parse_effective_config(result.stdout)
                return effective[alias]
            try:
                hosts = [{"id": "local", "alias": "local", "label": "本机", "hostname": "127.0.0.1", "user": "", "port": None, "route": ["local"]}]
                if not self.config_path.exists():
                    hosts[0]["warning"] = f"SSH 配置不存在：{self.config_path}。创建 SSH Host 别名后将自动识别。"
                aliases = discover_aliases(self.config_path)
                # Validate a config even when it contains no enumerable aliases.
                if self.config_path.exists() and not aliases:
                    resolve("jumper-manager-config-validation")
                    effective.clear()
                for alias in aliases:
                    if self._closed.is_set():
                        raise RuntimeError("程序正在关闭。")
                    config = resolve(alias)
                    route, warnings = route_for(alias, resolve)
                    host = {"id": alias, "alias": alias, "label": alias, "hostname": config.get("hostname", [alias])[0],
                            "user": config.get("user", [""])[0], "port": int(config.get("port", ["22"])[0]), "route": route}
                    if config.get("localforward") or config.get("remoteforward"):
                        warnings.append("该别名预设了 LocalForward / RemoteForward；为避免附带启动其他映射，本程序不会启动此别名的隧道。请使用独立的 SSH Host 别名。")
                    if warnings:
                        host["warning"] = " ".join(warnings)
                    hosts.append(host)
                if configuration_signature(self.config_path) != signature:
                    raise RuntimeError("SSH 配置仍在修改中，稍后将自动重试。")
                with self._lock:
                    if self._closed.is_set():
                        raise RuntimeError("程序正在关闭。")
                    previous = self._effective
                    self._write_safe_config(effective)
                    self._effective = effective
                    self._hosts = hosts
                    self._config_signature = signature
                    self._discovery.update(revision=self._discovery["revision"] + 1, last_refresh=now(), error=None)
                    self._refresh_mapping_plans(previous)
                    return copy.deepcopy(hosts)
            except (OSError, RuntimeError, ValueError) as exc:
                with self._lock:
                    self._config_signature = signature
                    self._discovery["error"] = "SSH 配置读取失败，暂时保留上次识别结果：" + str(exc)
                raise RuntimeError(self._discovery["error"]) from exc
            finally:
                with self._lock:
                    self._discovery["refreshing"] = False

    def _refresh_mapping_plans(self, previous):
        for mapping in self._mappings.values():
            active = mapping["status"] in {"starting", "running", "degraded", "stopping"}
            if active:
                aliases = {node["host"] for node in mapping.get("route", []) if node["host"] != "local"}
                if any(previous.get(alias) != self._effective.get(alias) for alias in aliases):
                    mapping["config_changed"] = True
                    warning = "相关 SSH 配置已变化；当前连接保留原路径，停止后重新启动才会使用新配置。"
                    if warning not in mapping.get("plan", {}).get("warnings", []):
                        mapping.setdefault("plan", {}).setdefault("warnings", []).append(warning)
                    self._log(mapping["id"], "warning", warning)
                continue
            try:
                plan = self.preview(mapping)
                mapping.update(plan=plan, route=plan["route"], config_changed=False)
                if mapping.pop("config_error", False):
                    mapping.update(status="stopped", error=None, health=None)
            except ValueError as exc:
                mapping.update(config_error=True, status="error", error=str(exc), route=[],
                               plan={"route": [], "description": "SSH 设备配置需要更新", "warnings": [str(exc)], "steps": []})

    def _watch_config(self):
        while not self._closed.wait(CONFIG_POLL_INTERVAL):
            try:
                signature = configuration_signature(self.config_path)
                if signature != self._config_signature:
                    self.refresh_hosts()
            except Exception as exc:
                with self._lock:
                    self._discovery["error"] = str(exc)

    def _write_safe_config(self, effective=None):
        lines = ["# Generated non-secret execution policy; do not edit.", "Host *"]
        lines += ["    " + option.replace("=", " ", 1) for option in SAFE_OPTIONS]
        for alias, config in (self._effective if effective is None else effective).items():
            parsed = simple_proxy(config.get("proxycommand", ["none"])[0])
            if parsed and ALIAS_RE.fullmatch(alias):
                # Force safe noninteractive policy on common ssh -W jump processes too.
                options = parsed["options"]
                args = [self.ssh, *sum((["-o", option] for option in SAFE_OPTIONS), [])]
                if "-F" not in options:
                    args += ["-F", str(self.safe_config)]
                args += [*options, "-W", parsed["forward"], parsed["destination"]]
                command = subprocess.list2cmdline(args) if os.name == "nt" else __import__("shlex").join(args)
                lines += [f"Host {alias}", "    ProxyCommand " + command]
        if self.config_path.exists():
            escaped = str(self.config_path).replace("\\", "/").replace('"', '\\"')
            lines += ["Host *", f'    Include "{escaped}"']
        temporary = self.safe_config.with_suffix(".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, self.safe_config)

    def _ssh_args(self, alias: str, *, probe=False) -> list[str]:
        args = [self.ssh, "-F", str(self.safe_config), "-T"]
        if probe:
            args += ["-o", "ClearAllForwardings=yes"]
        return args

    def _validate(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("映射配置必须是 JSON 对象。")
        name = payload.get("name", "").strip() if isinstance(payload.get("name", ""), str) else ""
        if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise ValueError("请输入 1 到 100 字符的映射名称。")
        result = {"name": name}
        hosts = {host["id"] for host in self._hosts}
        for key in ("source_host", "target_host"):
            value = payload.get(key)
            if not isinstance(value, str) or value not in hosts:
                raise ValueError(f"设备 {value!r} 不在当前 SSH 配置中，请先刷新设备列表。")
            result[key] = value
        result["source_port"] = port(payload.get("source_port"))
        result["target_port"] = port(payload.get("target_port"))
        result["bind_address"] = address(payload.get("bind_address", "127.0.0.1"), bind=True)
        result["target_address"] = address(payload.get("target_address", "127.0.0.1"))
        auto = payload.get("auto_start", False)
        if not isinstance(auto, bool):
            raise ValueError("auto_start 必须是布尔值。")
        result["auto_start"] = auto
        if result["source_host"] == result["target_host"] and result["source_port"] == result["target_port"] and result["bind_address"] == result["target_address"]:
            raise ValueError("监听端点和目标端点相同会形成回路，请修改设备、地址或端口。")
        return result

    def preview(self, payload: dict) -> dict:
        with self._lock:
            value = self._validate(payload)
            hosts = {host["id"]: host for host in self._hosts}
            source, target = value["source_host"], value["target_host"]
            route = [{"host": source, "label": hosts[source]["label"], "role": "source", "port": value["source_port"]}]
            warnings = []
            for endpoint in (source, target):
                if hosts[endpoint].get("warning"):
                    warnings.append(hosts[endpoint]["warning"])
            if source != "local":
                for gateway in reversed(hosts[source]["route"][:-1]):
                    route.append({"host": gateway, "label": gateway, "role": "gateway"})
                route.append({"host": "local", "label": "本机中转", "role": "relay"})
            if target != "local":
                for gateway in hosts[target]["route"][:-1]:
                    route.append({"host": gateway, "label": gateway, "role": "gateway"})
            route.append({"host": target, "label": hosts[target]["label"], "role": "target", "port": value["target_port"]})
            start_endpoint = f"{hosts[source]['label']} {value['bind_address']}:{value['source_port']}"
            target_endpoint = f"{hosts[target]['label']} {value['target_address']}:{value['target_port']}"
            if source == target == "local":
                steps = [f"本机 TCP 转发：{start_endpoint} → {target_endpoint}。"]
            elif source == "local":
                steps = [f"本机通过 SSH 登录 {target}，建立 -L 本地端口转发。"]
            elif target == "local":
                steps = [f"本机通过 SSH 登录 {source}，建立 -R 远端监听，并回传至本机目标端口。"]
            else:
                steps = [f"本机通过 SSH 登录 {target}，建立 -L 到随机本机回环中转端口。",
                         f"本机通过 SSH 登录 {source}，建立 -R，将远端监听连接至该中转端口。"]
            warnings.append("状态检查验证 SSH、监听端口及目标 TCP 可达性；不等同于应用协议健康检查。")
            if source != "local" or target != "local":
                warnings.append("映射依赖本机持续开机、联网；远端端口检查需要 python3。")
            return {"route": route, "description": f"{start_endpoint} → {'本机中转 → ' if source != 'local' else ''}{target_endpoint}",
                    "warnings": list(dict.fromkeys(warnings)), "steps": steps}

    def _new_mapping(self, value: dict, mapping_id: str) -> dict:
        plan = self.preview(value)
        return {**value, "id": mapping_id, "status": "stopped", "route": plan["route"], "plan": plan, "error": None,
                "health": None, "last_checked": None, "logs": []}

    def _load(self):
        path = self.data / "mappings.json"
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("mappings"), list):
                raise ValueError("格式不正确")
            for entry in raw["mappings"]:
                mapping_id = entry.get("id")
                if not isinstance(mapping_id, str) or not re.fullmatch(r"[0-9a-f]{32}", mapping_id) or mapping_id in self._mappings:
                    raise ValueError("映射 ID 不正确或重复")
                try:
                    value = self._validate(entry)
                    mapping = self._new_mapping(value, mapping_id)
                except ValueError as exc:
                    # Keep removed SSH aliases visible so a refresh/edit can fix them.
                    mapping = {key: entry.get(key) for key in FIELDS}
                    mapping.update({"id": mapping_id, "status": "error", "route": [], "plan": {"route": [], "description": "配置需要修正", "warnings": [str(exc)], "steps": []},
                                    "error": str(exc), "health": None, "last_checked": None, "logs": []})
                self._mappings[mapping_id] = mapping
                self._mapping_locks[mapping_id] = threading.RLock()
                if self._recovery_warnings:
                    self._log(mapping_id, "warning", " ".join(self._recovery_warnings))
        except (ValueError, OSError, TypeError) as exc:
            raise RuntimeError(f"读取 {path} 失败；为防止覆盖旧配置已停止启动：{exc}") from exc

    def _save(self):
        atomic_json(self.data / "mappings.json", {"version": VERSION, "mappings": [{key: value.get(key) for key in ("id", *FIELDS)} for value in self._mappings.values()]})

    def state(self) -> dict:
        with self._lock:
            return {"hosts": copy.deepcopy(self._hosts), "mappings": copy.deepcopy(list(self._mappings.values())), "ssh_config": str(self.config_path), "version": VERSION, "ssh_discovery": copy.deepcopy(self._discovery)}

    def _get(self, mapping_id):
        with self._lock:
            if mapping_id not in self._mappings:
                raise KeyError("映射不存在。")
            return self._mappings[mapping_id]

    def create(self, payload):
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("程序正在关闭。")
            value = self._validate(payload)
            mapping_id = uuid.uuid4().hex
            mapping = self._new_mapping(value, mapping_id)
            self._mappings[mapping_id] = mapping
            self._mapping_locks[mapping_id] = threading.RLock()
            try:
                self._save()
            except Exception:
                del self._mappings[mapping_id]
                del self._mapping_locks[mapping_id]
                raise
            self._log(mapping_id, "info", "映射已保存，尚未启动。")
            return copy.deepcopy(mapping)

    def update(self, mapping_id, payload):
        self._get(mapping_id)
        with self._mapping_locks[mapping_id], self._lock:
            if self._closed.is_set():
                raise RuntimeError("程序正在关闭。")
            old = self._get(mapping_id)
            if old["status"] not in {"stopped", "error"} or self._processes.get(mapping_id) or mapping_id in self._relays:
                raise ValueError("请先停止映射再修改。")
            value = self._validate(payload)
            mapping = self._new_mapping(value, mapping_id)
            self._mappings[mapping_id] = mapping
            try:
                self._save()
            except Exception:
                self._mappings[mapping_id] = old
                raise
            self._log(mapping_id, "info", "映射配置已更新。")
            return copy.deepcopy(mapping)

    def delete(self, mapping_id):
        self._get(mapping_id)
        with self._mapping_locks[mapping_id]:
            self.stop(mapping_id)
            with self._lock:
                old = self._mappings.pop(mapping_id)
                try:
                    self._save()
                except Exception:
                    self._mappings[mapping_id] = old
                    raise

    def _log(self, mapping_id, level, message):
        with self._lock:
            mapping = self._mappings.get(mapping_id)
            if mapping is not None:
                mapping["logs"].append({"time": now(), "level": level, "message": str(message)[-2500:]})
                del mapping["logs"][:-150]

    def _recover(self):
        runtime = self.data / "runtime.json"
        if not runtime.exists():
            return
        try:
            records = json.loads(runtime.read_text(encoding="utf-8")).get("processes", [])
            for record in records:
                if not terminate_owned(record):
                    self._unrecovered_records.append(record)
                    self._recovery_warnings.append(f"旧进程 {record.get('pid')} 身份不匹配，未操作；如端口冲突请人工检查。")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._recovery_warnings.append(f"旧运行记录无法恢复：{exc}；未按进程名终止任何程序。")
        atomic_json(runtime, {"processes": self._unrecovered_records})

    def _save_runtime(self):
        with self._lock:
            atomic_json(self.data / "runtime.json", {"processes": [*self._unrecovered_records, *(entry["record"] for group in self._processes.values() for entry in group)]})

    def probe_host(self, alias: str) -> dict:
        hosts = {host["id"]: host for host in self._hosts}
        if alias not in hosts:
            raise ValueError("设备不在当前 SSH 配置中。")
        if alias == "local":
            return {"ok": True, "alias": alias, "message": "本机可用，无需 SSH。", "route": ["local"]}
        try:
            result = self._run([*self._ssh_args(alias, probe=True), alias, "echo JUMPER_MANAGER_SSH_OK"], timeout=25)
            ok = result.returncode == 0 and "JUMPER_MANAGER_SSH_OK" in result.stdout
            return {"ok": ok, "alias": alias, "message": "SSH 登录成功（已验证主机密钥）。" if ok else self._diagnostic(result.stderr or result.stdout), "route": hosts[alias]["route"]}
        except RuntimeError as exc:
            return {"ok": False, "alias": alias, "message": str(exc), "route": hosts[alias]["route"]}

    @staticmethod
    def _diagnostic(message):
        message = message.strip()[-1800:]
        hint = ""
        if "Host key verification failed" in message or "REMOTE HOST IDENTIFICATION" in message:
            hint = "主机密钥尚未信任或已变化，请先在终端人工核对并通过 SSH 登录该设备。 "
        elif "Permission denied" in message:
            hint = "SSH 密钥认证失败，请检查 SSH User、IdentityFile 和 ssh-agent。 "
        elif "Address already in use" in message or "remote port forwarding failed" in message:
            hint = "监听端口被占用或 SSH 服务禁止转发；本程序不会关闭其他程序的隧道。 "
        elif "python3" in message and ("not found" in message or "不是" in message):
            hint = "远端缺少 python3，无法执行安全的端口检查；请安装 python3 后重试。 "
        return hint + (message or "SSH 操作失败，未返回详细错误。")

    def _endpoint_check(self, host, operation, host_address, host_port):
        if host == "local":
            return local_socket_check(operation, host_address, host_port)
        payload = base64.b64encode(json.dumps({"operation": operation, "address": host_address, "port": host_port}).encode()).decode()
        # Only the fixed command is interpreted by the remote shell; all user data
        # is encoded and delivered over stdin, never interpolated into shell code.
        script = "import base64,json,os,socket\np=json.loads(base64.b64decode('" + payload + "'))\n"
        # Match OpenSSH's POSIX listener policy: TIME_WAIT is reusable, an
        # existing listener is not. Also verify that this socket can listen;
        # OpenSSH checks again when acquiring the actual listener after probing.
        # Never use SO_REUSEPORT or Windows SO_REUSEADDR to bypass conflicts.
        script += """try:
 if p['operation']=='available':
  with socket.socket(socket.AF_INET6 if ':' in p['address'] else socket.AF_INET) as s:
   if os.name=='nt' and hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
    s.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
   elif os.name!='nt':
    s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
   s.bind((p['address'],p['port']))
   s.listen(1)
 else:
  with socket.create_connection((p['address'],p['port']),4):
   pass
 print('JM_RESULT:'+json.dumps({'ok':True,'message':'TCP check passed'}))
except OSError as e:
 print('JM_RESULT:'+json.dumps({'ok':False,'message':str(e),'errno':e.errno}))
"""
        try:
            result = self._run([*self._ssh_args(host, probe=True), host, "python3 -"], input=script, timeout=25)
            for line in reversed(result.stdout.splitlines()):
                if line.startswith("JM_RESULT:"):
                    checked = json.loads(line.removeprefix("JM_RESULT:"))
                    if not isinstance(checked, dict) or not isinstance(checked.get("ok"), bool):
                        raise ValueError("远端端口检查返回了无效结果。")
                    return {**checked, "checked": True}
            return {"ok": False, "checked": False, "message": self._diagnostic(result.stderr or result.stdout)}
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "checked": False, "message": str(exc)}

    def _spawn(self, mapping_id, alias, kind, specification):
        if self._closed.is_set():
            raise RuntimeError("程序正在关闭，已取消启动。")
        config = self._effective.get(alias, {})
        if config.get("localforward") or config.get("remoteforward"):
            raise RuntimeError(f"{alias} 已有 SSH 配置转发项；请建立不含 LocalForward / RemoteForward 的专用别名。")
        marker = f"{mapping_id}-{uuid.uuid4().hex}.log"
        log_path = self.logs_dir / marker
        # Windows OpenSSH -E may open its log without sharing read access.
        # Own the file handle instead, so readiness and UI log readers can
        # observe it while SSH is running. -N opens no remote session; this
        # SetEnv value is only an immutable command-line ownership marker.
        args = [*self._ssh_args(alias), "-v", "-N", "-o", "ExitOnForwardFailure=yes",
                "-o", f"SetEnv=JUMPER_MANAGER_ID={marker}", kind, specification, alias]
        with log_path.open("xb", buffering=0) as log_stream:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=log_stream, **hidden_options())
        job = WindowsJob(process)
        record = {"mapping_id": mapping_id, "pid": process.pid, "identity": identity(process.pid), "marker": marker}
        entry = {"process": process, "job": job, "record": record, "log": log_path, "offset": 0, "kind": kind, "alias": alias}
        with self._lock:
            self._processes.setdefault(mapping_id, []).append(entry)
        try:
            self._save_runtime()
        except Exception:
            terminate_owned(record, process, job)
            raise
        self._log(mapping_id, "info", f"后台 SSH 已启动：{alias} {kind} {specification}（PID {process.pid}）。")
        return entry

    def _read_process_log(self, mapping_id, entry):
        try:
            with entry["log"].open("rb") as stream:
                stream.seek(entry["offset"])
                chunk = stream.read(65536)
                entry["offset"] = stream.tell()
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                # Preserve actionable SSH diagnostics; omit verbose key/config traces.
                lowered = line.lower()
                if any(word in lowered for word in ("failed", "failure", "error", "refused", "denied", "disconnect", "timed out", "forwarding listening", "remote forward success", "authenticated to", "cannot", "could not", "address already")):
                    level = "error" if any(word in lowered for word in ("failed", "failure", "refused", "denied", "cannot", "could not", "error")) else "info"
                    self._log(mapping_id, level, line)
        except OSError:
            pass

    def _wait_ready(self, mapping_id, entry, listener_address=None, listener_port=None):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self._closed.is_set():
                raise RuntimeError("程序正在关闭，已取消启动。")
            if entry["process"].poll() is not None:
                self._read_process_log(mapping_id, entry)
                try:
                    output = entry["log"].read_text(encoding="utf-8", errors="replace")[-4000:]
                except OSError:
                    output = "SSH 进程启动后退出。"
                raise RuntimeError(self._diagnostic(output))
            if entry["kind"] == "-R":
                try:
                    content = entry["log"].read_text(encoding="utf-8", errors="replace")
                    if "remote forward success for:" in content:
                        self._read_process_log(mapping_id, entry)
                        return
                except OSError:
                    pass
            elif local_socket_check("connect", listener_address, listener_port)["ok"]:
                self._read_process_log(mapping_id, entry)
                return
            time.sleep(0.15)
        raise RuntimeError("SSH 未在 25 秒内确认端口转发成功，请查看日志。")

    def start(self, mapping_id):
        self._get(mapping_id)
        with self._mapping_locks[mapping_id]:
            if self._closed.is_set():
                raise RuntimeError("程序正在关闭。")
            with self._lock:
                mapping = self._get(mapping_id)
                if mapping["status"] in {"running", "degraded"} and (self._processes.get(mapping_id) or mapping_id in self._relays):
                    return copy.deepcopy(mapping)
                value = self._validate(mapping)
                plan = self.preview(value)
                mapping.update(status="starting", error=None, health=None, plan=plan, route=plan["route"], config_changed=False, config_error=False)
            self._log(mapping_id, "info", "开始检查监听端口并建立隧道；目标服务可以稍后启动。")
            try:
                available = self._endpoint_check(value["source_host"], "available", value["bind_address"], value["source_port"])
                if self._closed.is_set():
                    raise RuntimeError("程序正在关闭，已取消启动。")
                if not available["ok"]:
                    raise RuntimeError(f"监听端口 {value['source_host']} {value['bind_address']}:{value['source_port']} 不可用：{available['message']}")
                source, target_host = value["source_host"], value["target_host"]
                if source == target_host == "local":
                    self._relays[mapping_id] = LocalRelay(value["bind_address"], value["source_port"], value["target_address"], value["target_port"])
                    self._log(mapping_id, "info", "本机 TCP 转发监听已建立。")
                elif source == "local":
                    spec = forward_spec(value["bind_address"], value["source_port"], value["target_address"], value["target_port"])
                    entry = self._spawn(mapping_id, target_host, "-L", spec)
                    self._wait_ready(mapping_id, entry, value["bind_address"], value["source_port"])
                else:
                    destination_address, destination_port = value["target_address"], value["target_port"]
                    if target_host != "local":
                        with socket.socket() as reservation:
                            reservation.bind(("127.0.0.1", 0))
                            relay_port = reservation.getsockname()[1]
                        with self._lock:
                            mapping["relay_port"] = relay_port
                        spec = forward_spec("127.0.0.1", relay_port, value["target_address"], value["target_port"])
                        entry = self._spawn(mapping_id, target_host, "-L", spec)
                        self._wait_ready(mapping_id, entry, "127.0.0.1", relay_port)
                        destination_address, destination_port = "127.0.0.1", relay_port
                    spec = forward_spec(value["bind_address"], value["source_port"], destination_address, destination_port)
                    entry = self._spawn(mapping_id, source, "-R", spec)
                    self._wait_ready(mapping_id, entry)
                with self._lock:
                    mapping["status"] = "running"
                self._log(mapping_id, "info", "隧道监听已建立，继续分别检查隧道和目标服务状态。")
            except Exception as exc:
                try:
                    self._cleanup(mapping_id)
                except Exception as cleanup_error:
                    exc = RuntimeError(f"{exc}；清理失败：{cleanup_error}")
                with self._lock:
                    mapping.update(status="error", error=str(exc))
                self._log(mapping_id, "error", f"启动失败，已清理本次创建的隧道：{exc}")
                raise RuntimeError(str(exc)) from exc
            # Health diagnostics do not own startup rollback. An unavailable
            # target (or a failed diagnostic) must not remove a ready tunnel.
            try:
                return self.check(mapping_id)
            except Exception as exc:
                checked_at = now()
                message = "隧道监听已建立，但状态检查未完成；已保留隧道，可稍后再次检查。"
                with self._lock:
                    mapping.update(status="degraded", error=None, last_checked=checked_at,
                                   health={"ok": False, "tunnel_ok": None, "target_ok": None,
                                           "summary": message, "details": [str(exc)], "checked_at": checked_at})
                self._log(mapping_id, "warning", f"{message} {exc}")
                return copy.deepcopy(mapping)

    def _cleanup(self, mapping_id):
        with self._lock:
            entries = list(self._processes.get(mapping_id, []))
            relay = self._relays.pop(mapping_id, None)
        if relay:
            relay.close()
        errors = []
        remaining = []
        for entry in reversed(entries):
            self._read_process_log(mapping_id, entry)
            try:
                terminate_owned(entry["record"], entry["process"], entry["job"])
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
                if entry["process"].poll() is None:
                    remaining.append(entry)
        with self._lock:
            if remaining:
                self._processes[mapping_id] = remaining
            else:
                self._processes.pop(mapping_id, None)
        self._save_runtime()
        with self._lock:
            if mapping_id in self._mappings:
                self._mappings[mapping_id].pop("relay_port", None)
        if errors:
            self._log(mapping_id, "warning", "清理部分进程时遇到错误：" + "; ".join(errors))
        if remaining:
            raise RuntimeError("部分后台进程无法停止；已保留其身份记录，请查看日志。")

    def stop(self, mapping_id):
        self._get(mapping_id)
        with self._mapping_locks[mapping_id]:
            with self._lock:
                mapping = self._get(mapping_id)
                mapping["status"] = "stopping"
            try:
                self._cleanup(mapping_id)
            except Exception as exc:
                with self._lock:
                    mapping.update(status="error", error=str(exc))
                raise
            with self._lock:
                mapping.update(status="stopped", error=None, health=None)
            self._log(mapping_id, "info", "已停止此映射的监听与转发。")
            return copy.deepcopy(mapping)

    def check(self, mapping_id):
        self._get(mapping_id)
        with self._mapping_locks[mapping_id]:
            mapping = self._get(mapping_id)
            if mapping["status"] not in {"running", "degraded"}:
                return copy.deepcopy(mapping)
            if mapping.get("config_changed"):
                message = "SSH 配置已变化，现有隧道未改道；请停止并重新启动后再检查新路径。"
                with self._lock:
                    mapping.update(status="degraded", error=message, last_checked=now(),
                                   health={"ok": False, "tunnel_ok": None, "target_ok": None,
                                           "summary": message, "details": [message], "checked_at": now()})
                return copy.deepcopy(mapping)
            details = []
            tunnel_ok = True
            entries = self._processes.get(mapping_id, [])
            for entry in entries:
                self._read_process_log(mapping_id, entry)
                if entry["process"].poll() is not None:
                    tunnel_ok = False
                    details.append(f"{entry['alias']} SSH 进程已退出。")
            if not entries and mapping_id not in self._relays:
                tunnel_ok = False
                details.append("找不到本程序持有的转发进程。")
            def probe(host, host_address, host_port):
                try:
                    return self._endpoint_check(host, "connect", host_address, host_port)
                except (OSError, RuntimeError, ValueError) as exc:
                    return {"ok": False, "checked": False, "message": f"检查失败：{exc}"}

            target = probe(mapping["target_host"], mapping["target_address"], mapping["target_port"])
            target_ok = bool(target["ok"]) if target.get("checked", True) else None
            details.append("目标端口：" + ("TCP 可达。" if target_ok else target["message"]))
            listener = probe(mapping["source_host"], mapping["bind_address"], mapping["source_port"])
            details.append("来源监听：" + ("TCP 可连接。" if listener["ok"] else listener["message"]))
            tunnel_ok = tunnel_ok and bool(listener["ok"])
            if mapping.get("relay_port"):
                relay = probe("local", "127.0.0.1", mapping["relay_port"])
                details.append("本机中转端口：" + ("TCP 可连接。" if relay["ok"] else relay["message"]))
                tunnel_ok = tunnel_ok and bool(relay["ok"])
            details.append("上述检查没有发送应用协议数据；TCP 接受连接不代表完整应用请求成功。")
            checked_at = now()
            ok = bool(tunnel_ok and target_ok)
            if ok:
                summary = "SSH/监听与目标 TCP 检查通过；应用协议未验证。"
            elif tunnel_ok and target_ok is False:
                summary = "隧道已建立，目标服务尚未就绪；服务启动后即可使用，无需重启隧道。"
            elif tunnel_ok:
                summary = "隧道已建立，但无法确认目标服务状态；请查看检查详情，可稍后再次检查。"
            else:
                summary = "隧道检查存在异常，请检查详细信息及日志。"
            health = {"ok": ok, "tunnel_ok": tunnel_ok, "target_ok": target_ok,
                      "summary": summary, "details": details, "checked_at": checked_at}
            with self._lock:
                mapping.update(health=health, last_checked=checked_at, status="running" if ok else "degraded",
                               error=None if tunnel_ok and target_ok is not None else "；".join(details[:-1]))
            self._log(mapping_id, "info" if ok else "warning", health["summary"])
            return copy.deepcopy(mapping)

    def _monitor(self):
        while not self._closed.wait(2):
            with self._lock:
                groups = list(self._processes.items())
            for mapping_id, entries in groups:
                lock = self._mapping_locks.get(mapping_id)
                if not lock or not lock.acquire(blocking=False):
                    continue
                try:
                    with self._lock:
                        mapping = self._mappings.get(mapping_id)
                        if not mapping or mapping["status"] not in {"running", "degraded"}:
                            continue
                        # A stop/start may have replaced the process group since
                        # the outer snapshot, before this mapping lock was taken.
                        entries = list(self._processes.get(mapping_id, []))
                    for entry in entries:
                        self._read_process_log(mapping_id, entry)
                    failed = [entry for entry in entries if entry["process"].poll() is not None]
                    if failed:
                        message = "SSH 连接已断开：" + ", ".join(entry["alias"] for entry in failed) + "。已停止相关转发，可手动重新启动。"
                        self._cleanup(mapping_id)
                        with self._lock:
                            mapping.update(status="error", error=message,
                                           health={"ok": False, "tunnel_ok": False, "target_ok": None,
                                                   "summary": message, "details": [message], "checked_at": now()})
                        self._log(mapping_id, "error", message)
                finally:
                    lock.release()

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._monitor_thread.join(timeout=3)
        self._config_thread.join(timeout=3)
        with self._lock:
            ids = list(self._mappings)
        for mapping_id in ids:
            try:
                self.stop(mapping_id)
            except Exception:
                pass
