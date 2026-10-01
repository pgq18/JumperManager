"""User-initiated update entry points for the tray and terminal."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import time

from jumper_manager import __version__
from jumper_manager.update_release import UpdateError, check_release, stage_release


LOG = logging.getLogger(__name__)


def install_release(info, root: Path, *, progress=None, wait=False):
    if not getattr(sys, "frozen", False):
        raise UpdateError("自动更新适用于下载的独立程序。当前为源码运行，请下载新版程序，或更新源码后重新构建。")
    from jumper_manager.update_install import prepare_install, launch_install
    executable = stage_release(info, Path(root) / "data" / "updates", progress=progress)
    if progress:
        progress("正在准备更新…")
    plan = prepare_install(Path(root), executable, info.version)
    result = launch_install(plan, wait=wait)
    return result


def _message(message, *, question=False, error=False):
    import ctypes
    # The dialog belongs to this background action; no console is created.
    flags = 0x10000 | (0x124 if question else 0x10 if error else 0x40)
    result = ctypes.windll.user32.MessageBoxW(None, message, "JumperManager · 软件更新", flags)
    return result == 6  # IDYES; a closed dialog never consents to installing.


def windows_update(root: Path, *, progress):
    """Called off the tray event loop. Return True after installer handoff."""
    try:
        info = check_release(__version__)
        if not info.available:
            _message(f"没有可用的新版本。\n\n当前版本：{__version__}\n最新正式版本：{info.version}")
            return False
        if not _message(
            f"发现新版本 {info.version}（当前 {__version__}）。\n\n"
            "是否下载并更新？\n配置会保留，更新时映射会短暂中断，完成后恢复已启动的映射。",
            question=True,
        ):
            return False
        progress("正在下载更新…")
        handed_off = install_release(info, root, progress=progress, wait=False)
        progress("正在安装更新…")
        # Normally the installer stops this process and the worker ends with it.
        # If handoff fails before shutdown, unblock the existing tray and explain
        # the failure instead of leaving the menu permanently disabled.
        status_path = handed_off.get("status_path")
        if status_path:
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                try:
                    status = json.loads(Path(status_path).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    status = {}
                if status.get("status") == "error":
                    raise UpdateError(status.get("message") or "安装失败，请查看 data/updates 中的更新日志。")
                if status.get("status") == "success":
                    return False
                time.sleep(0.5)
            raise UpdateError("更新尚未完成，请查看 data/updates 中的更新状态；不要重复启动安装。")
        return True
    except Exception as error:
        LOG.exception("Software update failed")
        _message(f"更新未完成：{error}\n\n可稍后从托盘菜单重试。", error=True)
        return False


def run(argv, *, root):
    parser = argparse.ArgumentParser(prog="jumper-manager update", allow_abbrev=False,
                                     description="检查或安装 GitHub 上最新的正式版本")
    parser.add_argument("--check", action="store_true", help="只检查版本，不下载或安装")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = parser.parse_args(argv)
    try:
        info = check_release(__version__)
        result = {**info.to_dict(), "updated": False, "action": "check" if args.check else "update"}
        if args.check or not info.available:
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"当前版本：{__version__}；最新正式版本：{info.version}")
                print("发现新版本，运行 ./jumper-manager update 安装。" if info.available else "没有可用的新版本。")
            return 0
        progress = None if args.json else lambda message: print(message, flush=True)
        if progress:
            progress(f"正在更新到 {info.version}；配置会保留，已启动的映射会在重启后恢复。")
        installed = install_release(info, Path(root), progress=progress, wait=True)
        result.update(installed)
        result["updated"] = installed.get("status") == "success"
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(installed.get("message") or (f"已更新到 {info.version}。" if result["updated"] else "更新未完成。"))
            for warning in installed.get("warnings", []):
                print(warning, file=sys.stderr)
        return 0 if result["updated"] else 1
    except Exception as error:
        if args.json:
            print(json.dumps({"error": str(error), "updated": False}, ensure_ascii=False))
        else:
            print(f"更新未完成：{error}", file=sys.stderr)
        return 1
