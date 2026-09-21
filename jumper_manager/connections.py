"""Read-only TCP connection snapshots; no process inspection or active probes."""
from __future__ import annotations

import base64
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys


def peer_key(host, port):
    return (str(ipaddress.ip_address(host)), int(port))


def parse_proc_rows(text, address, port, *, byteorder=None):
    wanted = ipaddress.ip_address(address)
    order = byteorder or sys.byteorder
    peers = set()
    def endpoint(value):
        encoded, number = value.split(":")
        data = bytes.fromhex(encoded)
        if order == "little":
            data = b"".join(data[index:index + 4][::-1] for index in range(0, len(data), 4))
        return ipaddress.ip_address(data), int(number, 16)
    for line in text.splitlines()[1:]:
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 4:
            raise ValueError("TCP connection table is incomplete")
        if fields[3] != "01":
            continue
        local, local_port = endpoint(fields[1])
        if local == wanted and local_port == port:
            remote, remote_port = endpoint(fields[2])
            peers.add((str(remote), remote_port))
    return peers


class Tcp4Row(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ("state", "local_address", "local_port", "remote_address", "remote_port", "pid")]


class Tcp6Row(ctypes.Structure):
    _fields_ = [("local_address", ctypes.c_ubyte * 16), ("local_scope", ctypes.c_uint32),
                ("local_port", ctypes.c_uint32), ("remote_address", ctypes.c_ubyte * 16),
                ("remote_scope", ctypes.c_uint32), ("remote_port", ctypes.c_uint32),
                ("state", ctypes.c_uint32), ("pid", ctypes.c_uint32)]


def parse_windows_table(data, address, port):
    wanted = ipaddress.ip_address(address)
    row_type = Tcp4Row if wanted.version == 4 else Tcp6Row
    if len(data) < 4:
        raise ValueError("Windows TCP connection table is incomplete")
    count = int.from_bytes(data[:4], "little")
    row_size = ctypes.sizeof(row_type)
    if 4 + count * row_size > len(data):
        raise ValueError("Windows TCP connection table is incomplete")
    peers = set()
    for index in range(count):
        row = row_type.from_buffer_copy(data, 4 + index * row_size)
        if row.state != 5 or socket.ntohs(row.local_port & 0xffff) != port:
            continue
        if wanted.version == 4:
            local = ipaddress.ip_address(row.local_address.to_bytes(4, "little"))
            remote = ipaddress.ip_address(row.remote_address.to_bytes(4, "little"))
        else:
            local = ipaddress.ip_address(bytes(row.local_address))
            remote = ipaddress.ip_address(bytes(row.remote_address))
        if local == wanted:
            peers.add((str(remote), socket.ntohs(row.remote_port & 0xffff)))
    return peers


def _windows_connections(address, port):
    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                    ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.restype = ctypes.c_uint32
    family = 2 if ipaddress.ip_address(address).version == 4 else 23
    size = ctypes.c_uint32(0)
    buffer = None
    # The table may grow between the sizing and reading calls.
    for _ in range(5):
        result = api(buffer, ctypes.byref(size), False, family, 5, 0)
        if result == 0:
            return parse_windows_table(buffer.raw[:size.value] if buffer else b"\0" * 4, address, port)
        if result != 122:
            raise OSError(result, "GetExtendedTcpTable failed")
        if not 4 <= size.value <= 64 * 1024 * 1024:
            raise ValueError("Windows TCP connection table size is invalid")
        buffer = ctypes.create_string_buffer(size.value)
    raise OSError("TCP connection table changed too quickly to sample")


def local_connections(address, port):
    if os.name == "nt":
        return _windows_connections(address, port)
    if sys.platform.startswith("linux"):
        path = Path("/proc/net/tcp6" if ":" in address else "/proc/net/tcp")
        return parse_proc_rows(path.read_text(encoding="ascii"), address, port)
    raise OSError("此平台暂不支持读取 TCP 连接快照。")


def remote_snapshot_script(address, port):
    """Fixed Python 3.9-compatible helper. Parameters travel via stdin."""
    payload = base64.b64encode(json.dumps({"address": address, "port": port}).encode()).decode()
    return "import base64,ipaddress,json,sys\np=json.loads(base64.b64decode('" + payload + "'))\n" + r'''
try:
 if not sys.platform.startswith('linux'):
  raise OSError('远端平台暂不支持 TCP 连接快照（需要 Linux /proc）。')
 wanted=ipaddress.ip_address(p['address'])
 peers=set()
 def endpoint(value):
  encoded,number=value.split(':')
  data=bytes.fromhex(encoded)
  if sys.byteorder=='little':
   data=b''.join(data[i:i+4][::-1] for i in range(0,len(data),4))
  return ipaddress.ip_address(data),int(number,16)
 with open('/proc/net/tcp6' if wanted.version==6 else '/proc/net/tcp',encoding='ascii') as stream:
  next(stream)
  for line in stream:
   fields=line.split()
   if not fields:
    continue
   if len(fields)<4:
    raise ValueError('TCP connection table is incomplete')
   if fields[3]!='01':
    continue
   local,local_port=endpoint(fields[1])
   if local==wanted and local_port==p['port']:
    remote,remote_port=endpoint(fields[2])
    peers.add((str(remote),remote_port))
 print('JM_CONNECTIONS:'+json.dumps({'ok':True,'peers':sorted(peers)}))
except (OSError,ValueError,StopIteration) as e:
 print('JM_CONNECTIONS:'+json.dumps({'ok':False,'message':str(e)}))
'''
