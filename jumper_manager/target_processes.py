"""Count processes owning target TCP listeners without collecting process details.

The fixed portable source is also sent to remote Python interpreters. Keeping it
here makes frozen builds independent of inspect.getsource or installed sources.
"""
from __future__ import annotations

import base64
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys

from .connections import Tcp4Row, Tcp6Row


_PORTABLE_SOURCE = r'''
def _unknown(message, listening=None):
    return {"process_count": None, "listening": listening, "complete": False, "message": message}

def _normal_address(value):
    value = ipaddress.ip_address(str(value).split('%', 1)[0])
    return value.ipv4_mapped if value.version == 6 and value.ipv4_mapped else value

def _wanted_addresses(address, port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid TCP port")
    try:
        return {_normal_address(address)}
    except ValueError:
        return {_normal_address(row[4][0]) for row in socket.getaddrinfo(address, port, type=socket.SOCK_STREAM)}

def _match_address(bound, wanted):
    bound = _normal_address(bound)
    if bound in wanted or (bound.is_unspecified and any(item.version == bound.version for item in wanted)):
        return True
    # A tcp6 table does not expose IPV6_V6ONLY. Do not mistake an IPv6-only
    # listener for an IPv4 service, or silently undercount a dual-stack service.
    if bound.version == 6 and bound.is_unspecified and any(item.version == 4 for item in wanted):
        return None
    return False

def parse_proc_listeners(text, wanted, port, byteorder=None):
    lines = text.splitlines()
    if not lines or 'local_address' not in lines[0]:
        raise ValueError("Incomplete TCP listener table")
    order = byteorder or sys.byteorder
    inodes = set()
    ambiguous = False
    for line in lines[1:]:
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 10:
            raise ValueError("Incomplete TCP listener table")
        if fields[3] != '0A':
            continue
        encoded, number = fields[1].split(':')
        if int(number, 16) != port:
            continue
        data = bytes.fromhex(encoded)
        if order == 'little':
            data = b''.join(data[index:index + 4][::-1] for index in range(0, len(data), 4))
        match = _match_address(ipaddress.ip_address(data), wanted)
        if match is None:
            ambiguous = True
        elif match:
            inode = int(fields[9])
            if inode < 1:
                raise ValueError("Listener ownership is unavailable")
            inodes.add(inode)
    return inodes, ambiguous

def _linux_target_processes(address, port, proc_root='/proc'):
    wanted = _wanted_addresses(address, port)
    root = Path(proc_root)
    inodes = set()
    ambiguous = False
    for table_name in ('tcp', 'tcp6'):
        try:
            content = (root / 'net' / table_name).read_text(encoding='ascii')
        except FileNotFoundError:
            if table_name == 'tcp6':
                continue  # Linux can be built without IPv6 support.
            raise
        found, uncertain = parse_proc_listeners(content, wanted, port)
        inodes.update(found)
        ambiguous = ambiguous or uncertain
    if ambiguous:
        return _unknown('存在 IPv6 通配监听，但无法确认其是否接收目标 IPv4 地址。', True if inodes else None)
    if not inodes:
        return {"process_count": 0, "listening": False, "complete": True, "message": "目标端口没有监听进程。"}

    owners = set()
    matched = set()
    denied = False
    # hidepid can remove entire PID directories from this listing. A partial
    # view must not be reported as a complete count even if some owners match.
    try:
        for line in (root / 'mounts').read_text(encoding='utf-8').splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[2] == 'proc':
                if any(option.startswith('hidepid=') and option != 'hidepid=0' for option in fields[3].split(',')):
                    denied = True
    except OSError:
        denied = True
    try:
        directories = list(root.iterdir())
    except OSError:
        return _unknown('目标端口正在监听，但没有权限枚举所属进程。', True)
    for directory in directories:
        if not directory.name.isdigit():
            continue
        try:
            descriptors = list((directory / 'fd').iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue  # A process exited during the snapshot.
        except OSError:
            denied = True
            continue
        for descriptor in descriptors:
            try:
                link = os.readlink(descriptor)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                denied = True
                continue
            if link.startswith('socket:[') and link.endswith(']'):
                try:
                    inode = int(link[8:-1])
                except ValueError:
                    continue
                if inode in inodes:
                    owners.add(directory.name)
                    matched.add(inode)
    if denied or matched != inodes:
        return _unknown('目标端口正在监听，但进程权限或命名空间限制导致无法确认进程总数。', True)
    return {"process_count": len(owners), "listening": True, "complete": True,
            "message": "已统计持有目标监听套接字的进程，按进程去重。"}
'''

exec(compile(_PORTABLE_SOURCE, "<target-process-sampler>", "exec"))


def parse_windows_listeners(data, family, wanted, port):
    """Return internal PID set and visibility flags; never expose PIDs in API."""
    row_type = Tcp4Row if family == socket.AF_INET else Tcp6Row
    if len(data) < 4:
        raise ValueError("Incomplete Windows TCP listener table")
    count = int.from_bytes(data[:4], "little")
    row_size = ctypes.sizeof(row_type)
    if 4 + count * row_size > len(data):
        raise ValueError("Incomplete Windows TCP listener table")
    pids = set()
    listening = False
    ambiguous = False
    missing_pid = False
    for index in range(count):
        row = row_type.from_buffer_copy(data, 4 + index * row_size)
        if row.state != 2 or socket.ntohs(row.local_port & 0xffff) != port:
            continue
        packed = row.local_address.to_bytes(4, "little") if family == socket.AF_INET else bytes(row.local_address)
        match = _match_address(ipaddress.ip_address(packed), wanted)
        if match is None:
            ambiguous = True
        elif match:
            listening = True
            if row.pid:
                pids.add(int(row.pid))
            else:
                missing_pid = True
    return pids, listening, ambiguous, missing_pid


def _windows_table(family):
    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                    ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.restype = ctypes.c_uint32
    size = ctypes.c_uint32(0)
    buffer = None
    for _ in range(5):
        result = api(buffer, ctypes.byref(size), False, family, 3, 0)  # TCP_TABLE_OWNER_PID_LISTENER
        if result == 0:
            return buffer.raw[:size.value] if buffer else b"\0" * 4
        if result != 122:
            raise OSError(result, "GetExtendedTcpTable failed")
        if not 4 <= size.value <= 64 * 1024 * 1024:
            raise ValueError("Invalid Windows TCP table size")
        buffer = ctypes.create_string_buffer(size.value)
    raise OSError("TCP listener table changed during snapshot")


def _windows_target_processes(address, port):
    wanted = _wanted_addresses(address, port)
    pids = set()
    listening = ambiguous = missing_pid = False
    for family in (socket.AF_INET, socket.AF_INET6):
        found, present, uncertain, hidden = parse_windows_listeners(_windows_table(family), family, wanted, port)
        pids.update(found)
        listening = listening or present
        ambiguous = ambiguous or uncertain
        missing_pid = missing_pid or hidden
    if ambiguous:
        return _unknown("存在 IPv6 通配监听，但无法确认其是否接收目标 IPv4 地址。", True if listening else None)
    if missing_pid:
        return _unknown("目标端口正在监听，但系统未提供完整进程归属。", True)
    return {"process_count": len(pids), "listening": listening, "complete": True,
            "message": "已统计目标监听进程，按进程去重。" if listening else "目标端口没有监听进程。"}


def local_target_processes(address, port):
    try:
        if os.name == "nt":
            return _windows_target_processes(address, port)
        if sys.platform.startswith("linux"):
            return _linux_target_processes(address, port)
        return _unknown("此平台暂不支持目标监听进程统计。")
    except (OSError, ValueError, UnicodeError):
        return _unknown("无法读取完整目标监听进程信息，可能受到系统权限限制。")


def remote_target_process_script(address, port):
    """Return a fixed Python 3.9 helper for SSH stdin, with base64 parameters."""
    payload = base64.b64encode(json.dumps({"address": address, "port": port}).encode("utf-8")).decode("ascii")
    return ("import base64,ipaddress,json,os,socket,sys\nfrom pathlib import Path\n" + _PORTABLE_SOURCE
            + "\np=json.loads(base64.b64decode('" + payload + "'))\n"
            + "try:\n"
              " if not sys.platform.startswith('linux'):\n"
              "  result=_unknown('远端平台暂不支持监听进程统计（需要 Linux /proc）。')\n"
              " else:\n"
              "  result=_linux_target_processes(p['address'],p['port'])\n"
              "except (OSError,ValueError,UnicodeError):\n"
              " result=_unknown('无法读取完整目标监听进程信息，可能受到系统权限限制。')\n"
              "print('JM_TARGET_PROCESSES:'+json.dumps(result,ensure_ascii=True))\n")
