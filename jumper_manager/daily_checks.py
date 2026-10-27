"""Daily, local-time tunnel recovery, with durable once-per-day claims."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import re
import threading
import time as monotonic_time

from .ssh_config import configuration_signature


DEFAULT_DAILY_CHECK_TIME = "09:00"
SCHEDULE_FIELDS = ("daily_check_enabled", "daily_check_time")
RESULTS = {"checking", "ok", "reconnected", "failed", "skipped"}


def validate_schedule(payload: dict, *, partial=False) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("每日检查设置必须是 JSON 对象。")
    if partial and (not payload or set(payload) - set(SCHEDULE_FIELDS)):
        raise ValueError("仅支持 daily_check_enabled 和 daily_check_time。")
    result = {}
    if not partial or "daily_check_enabled" in payload:
        enabled = payload.get("daily_check_enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("daily_check_enabled 必须是布尔值。")
        result["daily_check_enabled"] = enabled
    if not partial or "daily_check_time" in payload:
        clock = payload.get("daily_check_time", DEFAULT_DAILY_CHECK_TIME)
        if not isinstance(clock, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
            raise ValueError("每日检查时间必须为 HH:MM（00:00 到 23:59）。")
        result["daily_check_time"] = clock
    return result


def local_now():
    return datetime.now().astimezone()


class DailyChecks:
    """All metadata uses the manager lock; network work uses only a mapping lock.

    ``clock`` and ``tick(at=..., background=False)`` permit deterministic tests.
    User start/stop generations cancel work already queued behind another action.
    Schedule revisions additionally cancel recovery after a live schedule edit.
    """

    def __init__(self, manager, writer, *, clock=local_now, max_workers=4):
        self.manager = manager
        self.writer = writer
        self.clock = clock
        self.path = manager.data / "daily-checks.json"
        self.history = {}
        self.storage_error = None
        self._generations = {}
        self._intent = {}
        self._since = {}
        self._revisions = {}
        self._inflight = set()
        self._workers = set()
        self._slots = threading.BoundedSemaphore(max_workers)
        self._load()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="daily-tunnel-checks")
        self._thread.start()

    def _load(self):
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("记录文件过大")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("checks"), dict):
                raise ValueError("记录格式不正确")
            for mapping_id, record in raw["checks"].items():
                if not re.fullmatch(r"[0-9a-f]{32}", mapping_id) or not isinstance(record, dict):
                    raise ValueError("记录中的映射 ID 不正确")
                day = record.get("date")
                if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or date.fromisoformat(day) == date.max:
                    raise ValueError("记录日期不正确")
                stamp = datetime.fromisoformat(record["last_run"])
                if stamp.tzinfo is None or record.get("result") not in RESULTS or not isinstance(record.get("message"), str):
                    raise ValueError("记录状态不正确")
                record = {key: record[key] for key in ("date", "last_run", "result", "message")}
                if record["result"] == "checking":
                    record.update(result="failed", message="上次每日检查被中断；当天不重复重连，下次将按计划检查。")
                self.history[mapping_id] = record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.storage_error = f"每日检查记录无法读取，已暂停自动检查以避免重复重连；请检查 {self.path}：{exc}"

    def _write(self):
        self.writer(self.path, {"version": 1, "checks": self.history})

    def arm(self, mapping_id):
        with self.manager._lock:
            generation = self._generations.get(mapping_id, 0) + 1
            self._generations[mapping_id] = generation
            self._intent[mapping_id] = generation
            self._since[mapping_id] = self.clock()
            self.refresh(mapping_id)
            return generation

    def disarm(self, mapping_id):
        with self.manager._lock:
            self._generations[mapping_id] = self._generations.get(mapping_id, 0) + 1
            self._intent.pop(mapping_id, None)
            self._since.pop(mapping_id, None)
            self.refresh(mapping_id)
            return self._generations[mapping_id]

    def latest_operation(self, mapping_id, generation):
        with self.manager._lock:
            return self._generations.get(mapping_id) == generation

    def changed(self, mapping_id):
        with self.manager._lock:
            self._revisions[mapping_id] = self._revisions.get(mapping_id, 0) + 1
            if mapping_id in self._intent:
                self._since[mapping_id] = self.clock()
            self.refresh(mapping_id)

    def ensure_current(self, mapping_id, generation, revision=None):
        with self.manager._lock:
            if self.manager._closed.is_set():
                raise RuntimeError("程序正在关闭，已取消启动。")
            if self._intent.get(mapping_id) != generation:
                raise RuntimeError("此启动操作已被后续的停止或启动操作取消。")
            if revision is not None:
                mapping = self.manager._mappings.get(mapping_id)
                if self._revisions.get(mapping_id, 0) != revision or not mapping or not mapping.get("daily_check_enabled"):
                    raise RuntimeError("每日检查设置已更改，已取消本次自动重连。")
                reason = self._invalid_reason(mapping)
                if reason:
                    raise RuntimeError(reason)
                # Do not let an unprocessed config-file edit slip through the
                # watcher interval and silently change an automatic reconnect.
                if configuration_signature(self.manager.config_path) != self.manager._config_signature:
                    raise RuntimeError("SSH 配置正在变化；请等待设备列表刷新并手动确认路径，未自动重连。")

    def _invalid_reason(self, mapping):
        if mapping.get("config_changed") or mapping.get("config_error") or self.manager._discovery.get("error"):
            return "SSH 配置已变化或无效；请确认设备路径并手动启动，未自动改道。"
        try:
            self.manager._validate(mapping)
        except (ValueError, TypeError) as exc:
            return f"映射配置需要修正，未自动重连：{exc}"
        return None

    def _deadline(self, mapping):
        mapping_id = mapping["id"]
        if mapping_id not in self._intent or not mapping.get("daily_check_enabled") or self.manager._closed.is_set():
            return None
        try:
            settings = validate_schedule(mapping)
        except ValueError:
            return None
        hour, minute = map(int, settings["daily_check_time"].split(":"))
        since = self._since[mapping_id]
        day = since.date()
        # Enabling or starting after today's time starts tomorrow. An already
        # armed mapping that sleeps across its deadline instead catches up.
        if (since.hour, since.minute, since.second, since.microsecond) > (hour, minute, 0, 0):
            day += timedelta(days=1)
        previous = self.history.get(mapping_id)
        if previous:
            day = max(day, date.fromisoformat(previous["date"]) + timedelta(days=1))
        return datetime.combine(day, time(hour, minute))

    def _next(self, mapping, moment):
        value = self._deadline(mapping)
        if value is None:
            return None
        # The OS resolves tomorrow's DST offset. An injected aware clock keeps
        # its supplied timezone so tests and embedders are deterministic.
        if self.clock is local_now:
            resolved = value.astimezone()
            # A nonexistent spring-forward time can normalize backwards on
            # POSIX. Display the first valid minute after the requested time.
            while resolved.replace(tzinfo=None) < value:
                value += timedelta(minutes=1)
                resolved = value.astimezone()
            return resolved
        return value.replace(tzinfo=moment.tzinfo)

    def view(self, mapping, at=None):
        moment = at or self.clock()
        record = self.history.get(mapping["id"], {})
        next_run = self._next(mapping, moment)
        invalid = self._invalid_reason(mapping) if next_run else None
        error = self.storage_error if mapping.get("daily_check_enabled") else None
        return {"next_run": next_run.isoformat(timespec="seconds") if next_run and not invalid and not error else None,
                "last_run": record.get("last_run"), "result": "failed" if error else record.get("result"),
                "message": error or invalid or record.get("message", ""),
                "timezone": moment.tzname() or str(moment.tzinfo),
                "eligible": bool(next_run and not invalid and not error)}

    def refresh(self, mapping_id, at=None):
        mapping = self.manager._mappings.get(mapping_id)
        if mapping is not None:
            mapping["scheduled_check"] = self.view(mapping, at)

    def _due(self, mapping, moment):
        next_run = self._deadline(mapping)
        # Compare wall clock values: a repeated DST hour is still the same day.
        return bool(next_run and next_run <= moment.replace(tzinfo=None))

    def tick(self, at=None, *, background=True):
        moment = at or self.clock()
        launched = []
        with self.manager._lock:
            if self.storage_error or self.manager._closed.is_set():
                return launched
            ids = list(self.manager._mappings)
        for mapping_id in ids:
            with self.manager._lock:
                mapping = self.manager._mappings.get(mapping_id)
                if not mapping or mapping_id in self._inflight or not self._due(mapping, moment):
                    continue
                if not self._slots.acquire(blocking=False):
                    break
                self._inflight.add(mapping_id)
                if background:
                    worker = threading.Thread(target=self._work, args=(mapping_id, moment), daemon=True,
                                              name=f"daily-check-{mapping_id[:8]}")
                    self._workers.add(worker)
                    worker.start()
            launched.append(mapping_id)
            if not background:
                self._work(mapping_id, moment)
        return launched

    def _finish(self, mapping_id, result, message):
        with self.manager._lock:
            self.history[mapping_id].update(result=result, message=message)
            try:
                self._write()
            except OSError as exc:
                # The claim was already durable. Do not retry either the write
                # or the reconnect every polling tick after a filesystem error.
                self.storage_error = f"每日检查结果无法保存，已暂停自动检查：{exc}"
            self.refresh(mapping_id)
            self.manager._log(mapping_id, "info" if result in {"ok", "reconnected"} else "warning", message)

    def _work(self, mapping_id, moment):
        lock = self.manager._mapping_locks.get(mapping_id)
        acquired = bool(lock and lock.acquire(blocking=False))
        try:
            if not acquired:
                return  # Manual operation owns this mapping; try a later tick.
            with self.manager._lock:
                mapping = self.manager._mappings.get(mapping_id)
                if not mapping or self.storage_error or not self._due(mapping, moment):
                    return
                generation = self._intent[mapping_id]
                revision = self._revisions.get(mapping_id, 0)
                self.history[mapping_id] = {"date": moment.date().isoformat(),
                                            "last_run": moment.isoformat(timespec="seconds"),
                                            "result": "checking", "message": "正在进行每日检查。"}
                try:
                    self._write()  # Claim before any network side effects.
                except OSError as exc:
                    self.storage_error = f"每日检查记录无法保存，已暂停自动检查：{exc}"
                    self.history[mapping_id].update(result="failed", message=self.storage_error)
                    self.refresh(mapping_id)
                    self.manager._log(mapping_id, "error", self.storage_error)
                    return
                self.refresh(mapping_id)
            try:
                self.ensure_current(mapping_id, generation, revision)
            except RuntimeError as exc:
                self._finish(mapping_id, "skipped", f"每日检查已跳过：{exc}")
                return
            self.manager._log(mapping_id, "info", "开始每日检查；仅在隧道断开时重连一次。")
            try:
                result = self.manager.check(mapping_id)
                self.ensure_current(mapping_id, generation, revision)
            except Exception as exc:
                # Cancellation/config changes must never be mistaken for a
                # broken transport and trigger a fresh start.
                try:
                    self.ensure_current(mapping_id, generation, revision)
                except RuntimeError as cancelled:
                    self._finish(mapping_id, "skipped", f"每日检查已取消：{cancelled}")
                    return
                self._finish(mapping_id, "failed", f"每日检查未完成，未自动重连：{exc}；下次按计划检查，也可手动检查。")
                return
            if result.get("status") in {"running", "degraded", "starting"} and (result.get("health") or {}).get("tunnel_ok") is True:
                self._finish(mapping_id, "ok", "每日检查完成：隧道未断开，无需重连。")
                return
            try:
                self.ensure_current(mapping_id, generation, revision)
                self.manager._log(mapping_id, "warning", "每日检查发现隧道断开，正在重连一次。")
                self.manager._cleanup(mapping_id)
                self.ensure_current(mapping_id, generation, revision)
                result = self.manager._start_mapping(mapping_id, generation, revision)
                self.ensure_current(mapping_id, generation, revision)
                if (result.get("health") or {}).get("tunnel_ok") is not True:
                    raise RuntimeError(result.get("error") or "重连后的检查未通过。")
            except Exception as exc:
                try:
                    self.ensure_current(mapping_id, generation, revision)
                except RuntimeError as cancelled:
                    self._finish(mapping_id, "skipped", f"每日重连已取消：{cancelled}")
                    return
                self._finish(mapping_id, "failed", f"每日重连失败：{exc}；今天不再自动重试，下次按计划检查，也可手动启动。")
                return
            self._finish(mapping_id, "reconnected", "每日检查发现断连，已重新启动隧道。")
        finally:
            if acquired:
                lock.release()
            with self.manager._lock:
                self._inflight.discard(mapping_id)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def _loop(self):
        while not self.manager._closed.wait(5):
            self.tick()

    def close(self):
        # Manager sets its shared cancellation event first, including SSH probes.
        deadline = monotonic_time.monotonic() + 3
        with self.manager._lock:
            workers = [self._thread, *self._workers]
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - monotonic_time.monotonic()))
