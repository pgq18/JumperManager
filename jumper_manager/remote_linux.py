"""Read Linux TCP metadata over SSH using a read-only POSIX shell helper.

Only rows for the requested port and matching socket ownership are returned.
Address decoding and interpretation happen in the local bundled Python runtime.
The remote host needs no Python runtime, installed agent, or writable directory.
"""
from __future__ import annotations

import ipaddress
import shlex

from .connections import parse_proc_rows
from .target_processes import _normal_address, _unknown, parse_proc_listeners


_PREFIX = "JM_LINUX_SNAPSHOT|"
_HEADER = "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
_LIMIT = 8192
_SHELL = r'''
LC_ALL=C
export LC_ALL
proc_root=/proc
emit() { printf 'JM_LINUX_SNAPSHOT|%s\n' "$*"; }
finish() { emit 'END 1'; exit 0; }
fail() { emit "ERROR $1"; finish; }
emit 'BEGIN 1'
emit "MODE $mode"
order=$(printf '\001\000' | od -An -tu2 2>/dev/null) || fail byteorder
set -- $order
case "${1-}" in
  1) emit 'ORDER little' ;;
  256) emit 'ORDER big' ;;
  *) fail byteorder ;;
esac
if [ "$literal" = 1 ]; then
  emit "ADDR $address"
else
  command -v getent >/dev/null 2>&1 || fail resolution
  addresses=$(getent ahosts "$address" 2>/dev/null) || fail resolution
  # NSS output is kept on the remote machine except for numeric addresses.
  printf '%s\n' "$addresses" | while read -r resolved rest; do
    case "$resolved" in
      ''|*[!0123456789abcdefABCDEF:.%]*) ;;
      *) emit "ADDR $resolved" ;;
    esac
  done
fi
inodes=' '
rows=0
for table in tcp tcp6; do
  path="$proc_root/net/$table"
  if [ ! -e "$path" ] && [ "$table" = tcp6 ]; then
    emit 'ABSENT tcp6'
    continue
  fi
  [ -r "$path" ] || fail tables
  emit "TABLE $table"
  # Redirect the loop instead of using a pipeline so inode state is retained.
  {
    IFS= read -r heading || fail tables
    case "$heading" in *local_address*) ;; *) fail tables ;; esac
    while IFS= read -r row; do
      set -- $row
      [ "$#" -eq 0 ] && continue
      [ "$#" -ge 10 ] || fail tables
      case "$2" in *:"$port_hex") ;; *) continue ;; esac
      if [ "$mode" = target ]; then
        [ "$4" = 0A ] || continue
      else
        [ "$4" = 01 ] || continue
      fi
      rows=$((rows + 1))
      [ "$rows" -le 8192 ] || fail rows_limit
      emit "ROW $table $row"
      if [ "$mode" = target ]; then
        shift 9
        inode=$1
        case "$inode" in ''|*[!0-9]*) fail tables ;; esac
        case "$inodes" in *" $inode "*) ;; *) inodes="$inodes$inode " ;; esac
      fi
    done
  } < "$path"
done
[ "$mode" = target ] || finish
if [ "$inodes" = ' ' ]; then
  emit 'VISIBILITY complete'
  finish
fi
command -v stat >/dev/null 2>&1 || fail ownership
visibility=complete
if [ -r "$proc_root/mounts" ]; then
  while read -r source mount filesystem options rest; do
    [ "$filesystem" = proc ] || continue
    case ",$options," in
      *,hidepid=*)
        case ",$options," in *,hidepid=0,*) ;; *) visibility=denied ;; esac ;;
    esac
  done < "$proc_root/mounts"
else
  visibility=denied
fi
[ -r "$proc_root" ] && [ -x "$proc_root" ] || visibility=denied
owner_rows=0
for directory in "$proc_root"/[0-9]*; do
  [ -d "$directory" ] || continue
  pid=${directory##*/}
  case "$pid" in ''|*[!0-9]*) continue ;; esac
  if [ ! -r "$directory/fd" ] || [ ! -x "$directory/fd" ]; then
    [ -d "$directory" ] && visibility=denied
    continue
  fi
  set -- "$directory"/fd/*
  [ -e "$1" ] || [ -L "$1" ] || continue
  [ "$#" -le 65536 ] || { visibility=denied; continue; }
  # stat emits no filenames. readlink output split on spaces/newlines could
  # mistake a socket-looking substring in a regular filename for a socket.
  records=$(stat -L -c '%i %F' -- "$@" 2>/dev/null) || {
    # Exit races are harmless; failures for surviving processes are uncertain.
    [ -d "$directory" ] && visibility=denied
  }
  seen=' '
  while read -r inode kind rest; do
    if [ "$kind" = socket ] && [ -z "$rest" ]; then
        case "$inode" in ''|*[!0-9]*) continue ;; esac
        case "$inodes" in *" $inode "*) ;; *) continue ;; esac
        case "$seen" in *" $inode "*) continue ;; esac
        seen="$seen$inode "
        owner_rows=$((owner_rows + 1))
        [ "$owner_rows" -le 8192 ] || fail owners_limit
        emit "OWNER $pid $inode"
    fi
  done <<EOF
$records
EOF
done
emit "VISIBILITY $visibility"
finish
'''


def _script(address: str, port: int, mode: str) -> str:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid TCP port")
    if not isinstance(address, str) or not address or len(address) > 253 or any(ord(c) < 33 for c in address):
        raise ValueError("Invalid target address")
    if address.startswith("-"):
        raise ValueError("Invalid target address")
    try:
        ipaddress.ip_address(address.split("%", 1)[0])
        literal = 1
    except ValueError:
        literal = 0
    return (f"address={shlex.quote(address)}\nport_hex={port:04X}\nliteral={literal}\nmode={mode}\n" + _SHELL)


def remote_snapshot_script(address: str, port: int) -> str:
    """Return a POSIX sh script to sample established peers at an endpoint."""
    return _script(address, port, "connections")


def remote_target_process_script(address: str, port: int) -> str:
    """Return a POSIX sh script to sample listener ownership at an endpoint."""
    return _script(address, port, "target")


def _parse(stdout: str, mode: str) -> dict:
    if not isinstance(stdout, str) or len(stdout) > 4 * 1024 * 1024:
        raise ValueError("远端 TCP 快照过大或无效。")
    lines = [line[len(_PREFIX):] for line in stdout.splitlines() if line.startswith(_PREFIX)]
    if not lines or lines[0] != "BEGIN 1" or lines[-1] != "END 1":
        raise ValueError("远端未返回完整 TCP 快照（需要 Linux /proc 和基本 shell 工具）。")
    snapshot = {"tables": {}, "addresses": set(), "owners": set(), "visibility": None}
    rows = 0
    for line in lines[1:-1]:
        kind, _, value = line.partition(" ")
        if kind == "ERROR":
            raise ValueError({
                "byteorder": "远端无法确定 TCP 地址字节序，需要 od 命令。",
                "tables": "无法读取远端完整 TCP 表（需要 Linux /proc 和读取权限）。",
                "resolution": "远端无法解析目标主机名，请使用 IP 地址或提供 getent 命令。",
                "ownership": "远端无法读取套接字归属，需要支持 -L 和 -c 的 stat 命令。",
                "rows_limit": "远端 TCP 连接过多，无法获得完整快照。",
                "owners_limit": "远端套接字归属过多，无法获得完整快照。",
            }.get(value, "远端 TCP 快照不可用。"))
        if kind == "MODE" and "mode" not in snapshot and value == mode:
            snapshot["mode"] = value
        elif kind == "ORDER" and "byteorder" not in snapshot and value in ("little", "big"):
            snapshot["byteorder"] = value
        elif kind == "ADDR":
            if len(snapshot["addresses"]) > 128:
                raise ValueError("目标主机名解析结果过多。")
            snapshot["addresses"].add(_normal_address(value))
        elif kind in ("TABLE", "ABSENT") and value in ("tcp", "tcp6") and value not in snapshot["tables"]:
            if kind == "ABSENT" and value != "tcp6":
                raise ValueError("远端 TCP 表不完整。")
            snapshot["tables"][value] = [] if kind == "TABLE" else None
        elif kind == "ROW":
            table, _, row = value.partition(" ")
            if table not in snapshot["tables"] or snapshot["tables"][table] is None or not row:
                raise ValueError("远端 TCP 表不完整。")
            rows += 1
            if rows > _LIMIT:
                raise ValueError("远端 TCP 快照过大。")
            fields = row.split()
            if len(fields) < 10:
                raise ValueError("远端 TCP 表不完整。")
            for endpoint in fields[1:3]:
                encoded, number = endpoint.split(":")
                if len(encoded) != (8 if table == "tcp" else 32) or len(number) != 4:
                    raise ValueError("远端 TCP 地址无效。")
                bytes.fromhex(encoded)
                int(number, 16)
            snapshot["tables"][table].append(row)
        elif kind == "OWNER" and mode == "target":
            parts = value.split()
            if len(parts) != 2 or any(not item.isascii() or not item.isdigit() or int(item) < 1 for item in parts):
                raise ValueError("远端套接字归属无效。")
            snapshot["owners"].add((int(parts[0]), int(parts[1])))
            if len(snapshot["owners"]) > _LIMIT:
                raise ValueError("远端套接字归属过多。")
        elif kind == "VISIBILITY" and mode == "target" and snapshot["visibility"] is None and value in ("complete", "denied"):
            snapshot["visibility"] = value
        else:
            raise ValueError("远端 TCP 快照格式无效。")
    if (snapshot.get("mode") != mode or "byteorder" not in snapshot or not snapshot["addresses"]
            or set(snapshot["tables"]) != {"tcp", "tcp6"}
            or (mode == "target" and snapshot["visibility"] is None)):
        raise ValueError("远端未返回完整 TCP 快照。")
    return snapshot


def _wanted(snapshot: dict, address: str) -> set:
    try:
        # A literal cannot be replaced by another address in the helper output.
        return {_normal_address(address)}
    except ValueError:
        return snapshot["addresses"]


def parse_connection_snapshot(stdout: str, address: str, port: int) -> set:
    """Return peer tuples, or raise on an incomplete/unsupported snapshot."""
    snapshot = _parse(stdout, "connections")
    peers = set()
    for rows in snapshot["tables"].values():
        if rows is None:
            continue
        for wanted in _wanted(snapshot, address):
            peers.update(parse_proc_rows(_HEADER + "\n".join(rows), str(wanted), port,
                                         byteorder=snapshot["byteorder"]))
    return peers


def parse_target_snapshot(stdout: str, address: str, port: int) -> dict:
    """Return only aggregate ownership; never expose remote PID information."""
    try:
        snapshot = _parse(stdout, "target")
        inodes = set()
        ambiguous = False
        for rows in snapshot["tables"].values():
            if rows is None:
                continue
            found, uncertain = parse_proc_listeners(_HEADER + "\n".join(rows), _wanted(snapshot, address),
                                                   port, snapshot["byteorder"])
            inodes.update(found)
            ambiguous = ambiguous or uncertain
        if ambiguous:
            return _unknown("存在 IPv6 通配监听，但无法确认其是否接收目标 IPv4 地址。", True if inodes else None)
        if not inodes:
            return {"process_count": 0, "listening": False, "complete": True, "message": "目标端口没有监听进程。"}
        owners = {(pid, inode) for pid, inode in snapshot["owners"] if inode in inodes}
        if snapshot["visibility"] != "complete" or {inode for _, inode in owners} != inodes:
            return _unknown("目标端口正在监听，但进程权限或命名空间限制导致无法确认进程总数。", True)
        return {"process_count": len({pid for pid, _ in owners}), "listening": True, "complete": True,
                "message": "已统计持有目标监听套接字的进程，按进程去重。"}
    except (ValueError, TypeError, UnicodeError) as error:
        return _unknown(str(error) or "无法读取完整目标监听进程信息。")
