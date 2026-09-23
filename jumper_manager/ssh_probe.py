"""Probe TCP through OpenSSH SOCKS forwarding without remote programs.

The SOCKS reply confirms a connection made by the remote sshd, not merely a
local SSH listener. Only the SOCKS handshake is sent; no application payload.
"""
from __future__ import annotations

import ipaddress
import os
import signal
import socket
import subprocess
import tempfile
import time

from .processes import WindowsJob, hidden_options, _linux_group


def _read_exact(stream, count, deadline=None):
    data = bytearray()
    while len(data) < count:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("SSH 端口检查超时。")
            stream.settimeout(remaining)
        chunk = stream.recv(count - len(data))
        if not chunk:
            raise OSError("SSH 端口检查连接提前关闭。")
        data.extend(chunk)
    return bytes(data)


def socks_connect(stream, address, port, *, deadline=None):
    """Return TCP outcome (None means SSH policy/protocol cannot check it)."""
    stream.sendall(b"\x05\x01\x00")
    if _read_exact(stream, 2, deadline) != b"\x05\x00":
        raise ValueError("SSH 端口检查返回了无效的 SOCKS 握手。")
    try:
        ip = ipaddress.ip_address(address)
        destination = bytes([1 if ip.version == 4 else 4]) + ip.packed
    except ValueError:
        encoded = address.encode("idna")
        if not 1 <= len(encoded) <= 253:
            raise ValueError("目标主机名长度无效。")
        destination = b"\x03" + bytes([len(encoded)]) + encoded
    stream.sendall(b"\x05\x01\x00" + destination + int(port).to_bytes(2, "big"))
    version, reply, reserved, family = _read_exact(stream, 4, deadline)
    if version != 5 or reserved != 0 or family not in (1, 3, 4):
        raise ValueError("SSH 端口检查返回了无效的 SOCKS 响应。")
    size = {1: 4, 4: 16}.get(family)
    if size is None:
        size = _read_exact(stream, 1, deadline)[0]
    _read_exact(stream, size + 2, deadline)
    if reply == 0:
        return {"ok": True, "checked": True, "message": "TCP 连接成功"}
    reasons = {1: "SSH 无法完成目标连接检查。", 2: "SSH 服务不允许此端口的连接检查。",
               3: "目标网络不可达。", 4: "目标主机不可达。", 5: "目标端口拒绝连接。",
               6: "目标连接超时。", 7: "SSH 服务不支持此连接检查。", 8: "SSH 服务不支持此地址类型。"}
    return {"ok": False, "checked": reply in (3, 4, 5, 6),
            "message": reasons.get(reply, "SSH 端口检查返回未知结果。")}


def _close_probe(process, job):
    # This Popen was started in a new process group/session, including jump
    # children. Never search for or kill another SSH session by process name.
    job.close()
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=2)
    if os.name != "nt":
        # A ProxyJump child may outlive the already reaped SSH parent.
        # The still-existing process group retains its ID until it is empty.
        deadline = time.monotonic() + 2
        while True:
            members, complete = _linux_group(process.pid)
            if complete and not members:
                return
            if time.monotonic() >= deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return
            time.sleep(.05)


def ssh_tcp_check(ssh_args, alias, address, port, *, timeout=25):
    """Use a temporary loopback-only SOCKS listener, then remove it entirely."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        local_port = reservation.getsockname()[1]
    # The manager disallows aliases with configured forwards. Do not add
    # ClearAllForwardings=yes here: it would also remove our explicit -D.
    args = [*ssh_args, "-N", "-o", "ExitOnForwardFailure=yes", "-o", "LogLevel=INFO",
            "-D", f"127.0.0.1:{local_port}", alias]
    process = job = None
    connected = False
    with tempfile.TemporaryFile() as diagnostics:
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=diagnostics, **hidden_options())
            job = WindowsJob(process)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    stream = socket.create_connection(("127.0.0.1", local_port), timeout=.2)
                except OSError:
                    time.sleep(.05)
                    continue
                with stream:
                    connected = True
                    result = socks_connect(stream, address, port, deadline=min(deadline, time.monotonic() + 5))
                    if process.poll() is not None:
                        break
                    return result
            raise OSError("SSH 端口检查未能在限定时间内完成。")
        except (OSError, ValueError) as exc:
            diagnostics.seek(0)
            details = diagnostics.read(16384).decode("utf-8", errors="replace").strip()[-1800:]
            # OpenSSH versions that close SOCKS on connect failure rather than
            # send an error reply still provide the sshd's channel-open reason.
            refused = connected and "open failed: connect failed:" in details
            return {"ok": False, "checked": refused, "message": details or str(exc)}
        finally:
            if process is not None and job is not None:
                _close_probe(process, job)
