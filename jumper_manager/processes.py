"""Hidden child processes and identity-checked ownership (no process-name killing)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import signal
import subprocess
import time


def hidden_options() -> dict:
    if os.name != "nt":
        return {"start_new_session": True}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    # A hidden NEW_CONSOLE is also inherited by ssh ProxyCommand descendants.
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NEW_CONSOLE}


def identity(pid: int) -> dict | None:
    if os.name != "nt":
        try:
            stat = _linux_stat(pid)
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            return {"created": stat["created"], "boot_id": boot_id, "pgid": stat["pgid"],
                    "session": stat["session"], "image": os.readlink(f"/proc/{pid}/exe"), "command": command}
        except (OSError, IndexError):
            return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        created = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        size = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            return None
        # ProcessCommandLineInformation reads the process's own immutable launch args.
        ntdll = ctypes.WinDLL("ntdll")
        query = ntdll.NtQueryInformationProcess
        query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        query.restype = wintypes.LONG
        needed = wintypes.ULONG()
        query(handle, 60, None, 0, ctypes.byref(needed))
        command = ""
        if 0 < needed.value < 1024 * 1024:
            buffer = ctypes.create_string_buffer(needed.value)
            if query(handle, 60, buffer, needed.value, ctypes.byref(needed)) >= 0:
                class UnicodeString(ctypes.Structure):
                    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]
                value = ctypes.cast(buffer, ctypes.POINTER(UnicodeString)).contents
                if value.Buffer and value.Length:
                    command = ctypes.wstring_at(value.Buffer, value.Length // 2)
        return {"created": created, "image": image.value, "command": command}
    finally:
        kernel.CloseHandle(handle)


class WindowsJob:
    """Close-on-crash job; children die even if the app is forcibly terminated."""
    def __init__(self, process: subprocess.Popen):
        self.handle = None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        handle = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if handle and kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) and kernel.AssignProcessToJobObject(handle, int(process._handle)):
            self.handle = handle
        elif handle:
            kernel.CloseHandle(handle)

    def close(self):
        if self.handle:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(self.handle)
            self.handle = None


def _linux_stat(pid):
    # comm can contain spaces and parentheses; fields after its final ')' start
    # with state (field 3). starttime is field 22 and survives exec().
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rfind(")") + 2:].split()
    return {"state": fields[0], "pgid": int(fields[2]), "session": int(fields[3]), "created": fields[19]}


def _linux_group(pgid):
    members = {}
    complete = True
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            info = _linux_stat(int(path.name))
            if info["pgid"] == pgid and info["state"] not in {"Z", "X", "x"}:
                members[int(path.name)] = info
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            complete = False
    return members, complete


def _linux_resources_gone(pid):
    # Unreadable /proc identity is not proof of death. Signal 0 is read-only;
    # a leaderless group can still contain a ProxyCommand descendant.
    try:
        members, complete = _linux_group(pid)
        if members:
            return False
        try:
            state = _linux_stat(pid)
            if state["state"] not in {"Z", "X", "x"}:
                return False
        except FileNotFoundError:
            pass
        if complete:
            return True
    except (OSError, ValueError, IndexError):
        pass
    for probe in (os.kill, os.killpg):
        try:
            probe(pid, 0)
            return False
        except ProcessLookupError:
            continue
        except OSError:
            return False
    return True


def _terminate_linux(record, process=None, job=None, *, grace=5, kill_grace=3):
    pid = int(record["pid"])
    current = identity(pid)
    expected = record.get("identity") or {}
    marker = record.get("marker", "")
    verified = bool(current and expected.get("created") == current.get("created")
                    and expected.get("boot_id") and expected.get("boot_id") == current.get("boot_id")
                    and expected.get("image") == current.get("image")
                    and marker and marker in current.get("command", ""))
    if not verified:
        if process is None or process.poll() is not None:
            return _linux_resources_gone(pid) if current is None else False
        # A live Popen child is owned even if /proc is restricted. Do not infer
        # process-group ownership or act on saved PIDs from missing metadata.
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=kill_grace)
        return _linux_resources_gone(pid)
    if current.get("pgid") != pid or current.get("session") != pid:
        return False  # Never send a signal into the caller's/shared group.
    known, _ = _linux_group(pid)
    # Recheck after enumeration, before sending the first group signal.
    confirmed = identity(pid)
    if not confirmed or any(confirmed.get(key) != current.get(key) for key in ("created", "boot_id", "image", "pgid", "session")):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return _linux_resources_gone(pid)

    def wait_for_group(timeout):
        deadline = time.monotonic() + timeout
        while True:
            if process is not None:
                process.poll()  # Reap our own leader; zombies hold no sockets.
            members, complete = _linux_group(pid)
            if not members and complete:
                return members, True
            if time.monotonic() >= deadline:
                return members, False
            time.sleep(0.05)

    members, gone = wait_for_group(grace)
    if gone:
        return True
    # The leader can exit while a proxy ignores SIGTERM. Only escalate when a
    # member captured in the verified original group is still the same process.
    if not any(member in known and value["created"] == known[member]["created"]
               and value["session"] == pid for member, value in members.items()):
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _, gone = wait_for_group(kill_grace)
    if not gone:
        raise OSError("SSH 进程组尚未完全退出；已保留所有权记录。")
    return True


def terminate_owned(record: dict, process: subprocess.Popen | None = None, job: WindowsJob | None = None) -> bool:
    if os.name != "nt":
        return _terminate_linux(record, process, job)
    pid = int(record["pid"])
    current = identity(pid)
    expected = record.get("identity") or {}
    marker = record.get("marker", "")
    verified = bool(current and expected.get("created") == current.get("created")
                    and expected.get("image", "").casefold() == current.get("image", "").casefold()
                    and marker and marker in current.get("command", ""))
    if process is not None and process.poll() is not None:
        if job:
            job.close()
        return True
    if not verified:
        # A held Popen handle is itself an ownership guarantee, even on a platform
        # without readable command lines. Never use this fallback for saved PIDs.
        if process is None:
            return current is None
        process.terminate()
    elif os.name == "nt":
        if job and job.handle:
            job.close()
        else:
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, **hidden_options())
    if job:
        job.close()
    if process is not None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    return True
