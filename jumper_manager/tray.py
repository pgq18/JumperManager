"""Windows notification-area controls for the local JumperManager service.

The native event loop runs on its own thread, which pystray supports on Windows.
Importing this module does not load a GUI backend or start any threads. Pillow is
also imported lazily so the command-line server can run without tray packages.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import importlib
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
import webbrowser


LOG = logging.getLogger(__name__)


def create_icon(size: int = 256):
    """Return a transparent RGBA Pillow image; suitable for a multi-size ICO."""
    if isinstance(size, bool) or not isinstance(size, int) or not 16 <= size <= 1024:
        raise ValueError("图标尺寸必须是 16–1024 之间的整数。")
    from PIL import Image, ImageDraw

    scale = 4
    image = Image.new("RGBA", (256 * scale, 256 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    dark, accent = "#0c121b", "#36d4b7"

    def box(values):
        return tuple(round(value * scale) for value in values)

    draw.rounded_rectangle(box((10, 10, 246, 246)), radius=56 * scale, fill=accent)
    width = 22 * scale
    draw.line([box((57, 81)), box((136, 81)), box((136, 145))], fill=dark, width=width, joint="curve")
    draw.arc(box((56, 105, 136, 185)), start=0, end=180, fill=dark, width=width)
    draw.line([box((127, 81)), box((200, 81))], fill=dark, width=width)
    draw.line([box((172, 52)), box((201, 81)), box((172, 110))], fill=dark, width=width, joint="curve")
    for x, y in ((57, 81), (67, 145), (172, 52), (172, 110)):
        radius = 10.5
        draw.ellipse(box((x - radius, y - radius, x + radius, y + radius)), fill=dark)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _load_pystray():
    try:
        return importlib.import_module("pystray")
    except ImportError as exc:
        raise RuntimeError("缺少系统托盘依赖 pystray / Pillow，请使用完整 EXE 或安装项目依赖。") from exc


class TrayController:
    """Own one tray icon, its event loop and a lightweight status poller.

    ``get_status`` should return ``Manager.state()`` and must return promptly.
    ``on_exit`` is called once on a background thread; it should request graceful
    server shutdown. The server's ``finally`` block must call :meth:`stop`.
    An optional ``autostart`` provider can be assigned before :meth:`start`;
    its ``is_enabled()`` reads the real setting and ``set_enabled(bool)`` writes
    it. Only an explicit menu action calls the write method.
    A stopped controller cannot be restarted; instantiate another controller.
    """

    POLL_INTERVAL = 3.0

    def __init__(self, url: str, data_dir: Path,
                 get_status: Callable[[], Mapping[str, Any]],
                 on_exit: Callable[[], None]):
        self.url = url
        self.data_dir = Path(data_dir)
        self.get_status = get_status
        self.on_exit = on_exit
        self.autostart = None
        self._lock = threading.RLock()
        self._icon_lock = threading.RLock()
        self._autostart_io_lock = threading.Lock()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._exit_requested = threading.Event()
        self._icon = None
        self._thread = None
        self._status_thread = None
        self._autostart_thread = None
        self._error: Exception | None = None
        self._status_error: str | None = None
        self._counts = (0, 0, 0, 0, 0)  # running tunnels, waiting, degraded, error, total
        self._has_status = False
        self._last_display = None
        self._last_open = float("-inf")
        self._autostart_enabled = False
        self._autostart_error: str | None = None
        self._autostart_busy = False

    @property
    def is_running(self) -> bool:
        return bool(self._ready.is_set() and not self._stopped.is_set()
                    and self._thread is not None and self._thread.is_alive())

    @property
    def error(self) -> Exception | None:
        return self._error

    def start(self, timeout: float = 8.0):
        """Show the icon and wait for native initialization, or raise promptly."""
        if sys.platform != "win32":
            raise RuntimeError("系统托盘模式目前仅支持 Windows。")
        with self._lock:
            if self._stopped.is_set():
                raise RuntimeError("托盘控制器已经停止，请创建新的控制器。")
            if self._thread is None:
                pystray = _load_pystray()
                try:
                    menu = pystray.Menu(
                        pystray.MenuItem("打开管理界面", self._open_ui, default=True),
                        pystray.MenuItem(self._status_text, None, enabled=False),
                        pystray.Menu.SEPARATOR,
                        pystray.MenuItem("打开日志目录", self._open_logs),
                        pystray.MenuItem(self._autostart_text, self._toggle_autostart,
                                         checked=self._autostart_checked,
                                         enabled=self._autostart_available),
                        pystray.Menu.SEPARATOR,
                        pystray.MenuItem(self._exit_text, self._request_exit,
                                         enabled=lambda _item: not self._exit_requested.is_set()),
                    )
                    self._icon = pystray.Icon("JumperManager", create_icon(),
                                              "JumperManager · 正在读取状态", menu)
                    self._thread = threading.Thread(target=self._run, name="jumper-tray", daemon=True)
                    self._thread.start()
                except Exception as exc:
                    self._error = exc
                    self._stopped.set()
                    raise RuntimeError(f"系统托盘初始化失败：{exc}") from exc
        if not self._ready.wait(max(0.0, timeout)):
            self.stop()
            raise RuntimeError("系统托盘初始化超时，请查看日志或重新启动程序。")
        if self._error is not None:
            self.stop()
            raise RuntimeError(f"系统托盘初始化失败：{self._error}") from self._error
        if self._stopped.is_set():
            raise RuntimeError("系统托盘在初始化期间已停止。")
        return self

    def _run(self):
        try:
            self._icon.run(setup=self._setup)
            if not self._stopped.is_set():
                raise RuntimeError("系统托盘事件循环意外退出。")
        except Exception as exc:
            with self._lock:
                if self._error is None:
                    self._error = exc
            LOG.exception("Tray event loop failed")
        finally:
            self._stopped.set()
            self._ready.set()

    def _setup(self, icon):
        try:
            with self._lock:
                if self._stopped.is_set():
                    return
                with self._icon_lock:
                    icon.visible = True
                self._status_thread = threading.Thread(target=self._poll_status, name="jumper-tray-status", daemon=True)
                self._status_thread.start()
            self._ready.set()
        except Exception as exc:
            with self._lock:
                self._error = exc
            LOG.exception("Tray setup failed")
            self._stopped.set()
            self._ready.set()
        finally:
            if self._stopped.is_set():
                try:
                    icon.stop()
                except Exception:
                    LOG.exception("Could not stop tray after initialization cancellation")

    def _poll_status(self):
        while not self._stopped.is_set():
            self._refresh_status()
            if self._stopped.wait(self.POLL_INTERVAL):
                break

    def _refresh_status(self):
        try:
            snapshot = self.get_status()
            mappings = snapshot.get("mappings", [])
            if not isinstance(mappings, (list, tuple)):
                raise ValueError("映射状态格式无效")
            valid_mappings = [mapping for mapping in mappings if isinstance(mapping, Mapping)]
            statuses = [mapping.get("status") for mapping in valid_mappings]
            waiting = sum(
                mapping.get("status") == "degraded"
                and not mapping.get("config_changed")
                and isinstance(mapping.get("health"), Mapping)
                and mapping["health"].get("tunnel_ok") is True
                and mapping["health"].get("target_ok") is False
                for mapping in valid_mappings
            )
            counts = (statuses.count("running") + waiting, waiting,
                      statuses.count("degraded") - waiting, statuses.count("error"), len(statuses))
            with self._lock:
                self._counts = counts
                self._has_status = True
                self._status_error = None
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            with self._lock:
                changed = message != self._status_error
                self._status_error = message
            if changed:
                LOG.exception("Could not read mapping status for tray")
        self._refresh_autostart()
        self._update_display()

    def _refresh_autostart(self):
        """Read registry-backed state without ever enabling it implicitly."""
        with self._lock:
            provider = self.autostart
            if self._autostart_busy or self._stopped.is_set():
                return
        enabled, error = False, None
        if provider is not None:
            try:
                with self._autostart_io_lock:
                    enabled = bool(provider.is_enabled())
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                with self._lock:
                    changed = error != self._autostart_error
                if changed:
                    LOG.exception("Could not read Windows autostart setting")
        with self._lock:
            # A user action or a new provider may have superseded this poll.
            if provider is self.autostart and not self._autostart_busy:
                self._autostart_enabled = enabled
                self._autostart_error = error

    def _autostart_text(self, _item=None):
        with self._lock:
            if self._autostart_busy:
                return "开机自启（正在设置…）"
            if self._autostart_error is not None:
                return "开机自启（状态读取失败）"
            return "开机自启（登录后）"

    def _autostart_checked(self, _item=None):
        # This cache is updated only by actual reads, never optimistic writes.
        with self._lock:
            return self.autostart is not None and self._autostart_enabled

    def _autostart_available(self, _item=None):
        with self._lock:
            return (self.autostart is not None and not self._autostart_busy
                    and not self._exit_requested.is_set() and not self._stopped.is_set())

    def _toggle_autostart(self, _icon=None, _item=None):
        with self._lock:
            if not self._autostart_available():
                return
            provider = self.autostart
            self._autostart_busy = True
        self._update_display()

        def change_setting():
            enabled, read_error, failure = False, None, None
            try:
                with self._autostart_io_lock:
                    desired = not bool(provider.is_enabled())
                    provider.set_enabled(desired)
                    enabled = bool(provider.is_enabled())
                    if enabled != desired:
                        raise RuntimeError("设置后的开机自启状态与请求不一致，请检查 Windows 启动设置。")
            except Exception as exc:
                failure = str(exc) or type(exc).__name__
                LOG.exception("Could not change Windows autostart setting")
                # Recover the real checkbox state even if a write partially
                # succeeded; a failed read must never invent a checked state.
                try:
                    with self._autostart_io_lock:
                        enabled = bool(provider.is_enabled())
                except Exception as read_exc:
                    enabled = False
                    read_error = str(read_exc) or type(read_exc).__name__
            finally:
                with self._lock:
                    if provider is self.autostart:
                        self._autostart_enabled = enabled
                        self._autostart_error = read_error
                    self._autostart_busy = False
                self._update_display()
            if failure is not None:
                self._notify(f"无法修改开机自启：{failure}")
            elif enabled:
                self._notify("已启用开机自启，下次登录 Windows 后将后台启动 JumperManager。")
            else:
                self._notify("已关闭开机自启。")

        with self._lock:
            if self._stopped.is_set() or self._exit_requested.is_set():
                self._autostart_busy = False
                return
            self._autostart_thread = self._dispatch("autostart", change_setting)

    def _notify(self, message):
        with self._icon_lock:
            if self._stopped.is_set() or self._icon is None:
                return
            try:
                if getattr(self._icon, "HAS_NOTIFICATION", False):
                    self._icon.notify(message[:250], "JumperManager")
            except Exception:
                LOG.warning("Could not display tray notification", exc_info=True)

    def _status_text(self, _item=None):
        with self._lock:
            if self._exit_requested.is_set():
                return "正在停止映射并退出…"
            if self._status_error is not None:
                return "状态读取失败（请查看日志）"
            if not self._has_status:
                return "正在读取映射状态…"
            running, waiting, degraded, errors, total = self._counts
            return f"映射：{running} 运行中 / {total} 总数 · {waiting} 等待服务 · {degraded + errors} 需关注"

    def _exit_text(self, _item=None):
        return "正在退出…" if self._exit_requested.is_set() else "退出 JumperManager（停止映射）"

    def _update_display(self):
        with self._lock:
            if self._stopped.is_set() or self._icon is None:
                return
            if self._exit_requested.is_set():
                title = "JumperManager · 正在停止映射并退出"
            elif self._status_error is not None:
                title = "JumperManager · 状态读取失败，请查看日志"
            else:
                running, waiting, degraded, errors, total = self._counts
                title = f"JumperManager · 运行 {running}/{total} · 等待服务 {waiting} · 降级 {degraded} · 异常 {errors}"
            signature = (title, self._status_text(), self._autostart_text(),
                         self._autostart_checked(), self._autostart_available())
            if signature == self._last_display:
                return
        try:
            with self._icon_lock:
                if self._stopped.is_set():
                    return
                self._icon.title = title
                self._icon.update_menu()
            with self._lock:
                self._last_display = signature
        except Exception:
            if not self._stopped.is_set():
                LOG.exception("Could not update tray menu")

    def _dispatch(self, name, action):
        def work():
            try:
                action()
            except Exception:
                LOG.exception("Tray action failed: %s", name)
        thread = threading.Thread(target=work, name=f"jumper-tray-{name}", daemon=True)
        thread.start()
        return thread

    def _open_ui(self, _icon=None, _item=None):
        # A double-click generates two button releases on Windows.
        with self._lock:
            if self._stopped.is_set() or time.monotonic() - self._last_open < 0.6:
                return
            self._last_open = time.monotonic()
        self._dispatch("open-ui", lambda: webbrowser.open(self.url, new=2))

    def _open_logs(self, _icon=None, _item=None):
        if self._stopped.is_set():
            return

        def open_directory():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.data_dir))

        self._dispatch("open-logs", open_directory)

    def _request_exit(self, _icon=None, _item=None):
        with self._lock:
            if self._exit_requested.is_set() or self._stopped.is_set():
                return
            self._exit_requested.set()
        self._update_display()

        def exit_service():
            try:
                self.on_exit()
            except Exception:
                self._exit_requested.clear()
                self._update_display()
                raise

        self._dispatch("exit", exit_service)

    def stop(self, timeout: float = 5.0):
        """Remove the icon and finish its threads; safe after partial startup."""
        with self._lock:
            self._stopped.set()
            icon = self._icon
            threads = (self._thread, self._status_thread, self._autostart_thread)
        if icon is not None:
            with self._icon_lock:
                try:
                    icon.visible = False
                except Exception:
                    LOG.debug("Tray icon was already removed", exc_info=True)
                try:
                    icon.stop()
                except Exception:
                    LOG.debug("Tray event loop was already stopped", exc_info=True)
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            if thread is not None and thread is not threading.current_thread() and thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    LOG.warning("Tray thread did not finish before timeout: %s", thread.name)
