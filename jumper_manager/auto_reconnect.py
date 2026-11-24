"""Bounded recovery of previously established tunnels, independent of targets."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import threading
import time

from .ssh_config import configuration_signature

RETRY_SECONDS = 5
MAX_ATTEMPTS = 3


def validate_reconnect(payload, *, partial=False):
    if not isinstance(payload, dict) or (partial and set(payload) != {"auto_reconnect_enabled"}):
        raise ValueError("仅支持 auto_reconnect_enabled。")
    value = payload.get("auto_reconnect_enabled", True)
    if not isinstance(value, bool):
        raise ValueError("auto_reconnect_enabled 必须是布尔值。")
    return {"auto_reconnect_enabled": value}


class AutoReconnect:
    """Workers share the existing per-mapping action lock and start generation.

    Retries are delayed from completion, not from the start of a slow SSH
    attempt. Exhaustion is durable; only an explicit start resets the budget.
    """

    def __init__(self, manager, writer, *, clock=time.monotonic):
        self.manager, self.writer, self.clock = manager, writer, clock
        self.path = manager.data / "reconnect-exhausted.json"
        self.exhausted = {}
        self.storage_error = None
        self.records = {}
        self._guards = {}
        self._inflight = set()
        self._workers = set()
        self._slots = threading.BoundedSemaphore(4)
        if self.path.exists():
            try:
                if self.path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("记录文件过大")
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("mappings"), dict):
                    raise ValueError("记录格式不正确")
                for key, message in raw["mappings"].items():
                    if not re.fullmatch(r"[0-9a-f]{32}", key) or not isinstance(message, str):
                        raise ValueError("记录内容不正确")
                self.exhausted = raw["mappings"]
            except (OSError, ValueError, TypeError) as exc:
                self.storage_error = f"自动重连记录无法读取，已暂停自动重连：{exc}"

    def _write(self):
        self.writer(self.path, {"version": 1, "mappings": self.exhausted})

    def reset(self, mapping_id):
        """Explicit start is the only operation that clears exhaustion."""
        with self.manager._lock:
            if self.storage_error:
                # Broken optional metadata must never disable manual operation.
                # Keep unattended recovery blocked until the record is repaired.
                self.records.pop(mapping_id, None)
                self.exhausted.pop(mapping_id, None)
                self.refresh(mapping_id)
                return
            if mapping_id in self.exhausted:
                old = self.exhausted.pop(mapping_id)
                try:
                    self._write()
                except Exception:
                    self.exhausted[mapping_id] = old
                    raise
            self.records.pop(mapping_id, None)
            self.refresh(mapping_id)

    def stop(self, mapping_id):
        with self.manager._lock:
            self.records.pop(mapping_id, None)
            self.refresh(mapping_id)

    def started(self, mapping_id, generation):
        with self.manager._lock:
            if self.manager._daily_checks._intent.get(mapping_id) != generation:
                return
            self.records[mapping_id] = {"generation": generation, "revision": 0,
                                       "state": "monitoring", "attempts": 0,
                                       "due": None, "next_retry": None, "message": "", "probe_due": 0}
            self.refresh(mapping_id)

    def changed(self, mapping_id):
        with self.manager._lock:
            record = self.records.get(mapping_id)
            if record:
                record["revision"] += 1
                if not self.manager._get(mapping_id)["auto_reconnect_enabled"]:
                    record.update(state="monitoring", due=None, next_retry=None, message="自动重连已关闭。")
                elif record["state"] == "idle":
                    record.update(state="monitoring", probe_due=0, message="")
            self.refresh(mapping_id)

    def refresh(self, mapping_id):
        mapping = self.manager._mappings.get(mapping_id)
        if mapping is None:
            return
        record = self.records.get(mapping_id, {})
        state = record.get("state", "idle")
        message = record.get("message", "")
        attempts = record.get("attempts", 0)
        if mapping_id in self.exhausted:
            state, attempts, message = "exhausted", MAX_ATTEMPTS, self.exhausted[mapping_id]
        elif self.storage_error:
            state, message = "blocked", self.storage_error
        elif not mapping.get("auto_reconnect_enabled", True):
            state = "disabled"
        mapping["auto_reconnect"] = {"state": state, "attempts": attempts, "max_attempts": MAX_ATTEMPTS,
                                     "retry_seconds": RETRY_SECONDS,
                                     "next_retry": record.get("next_retry") if state == "waiting" else None,
                                     "message": message}

    def blocked_startup(self, mapping_id):
        with self.manager._lock:
            return mapping_id in self.exhausted or bool(self.storage_error)

    def handles(self, mapping_id):
        """Daily recovery must not create an independent retry budget."""
        with self.manager._lock:
            mapping = self.manager._get(mapping_id)
            return self.blocked_startup(mapping_id) or (mapping.get("auto_reconnect_enabled", True)
                                                        and mapping_id in self.records)

    def ensure_current(self, mapping_id):
        guard = self._guards.get(mapping_id)
        if not guard or guard[0] is not threading.current_thread():
            return
        _, record, revision = guard
        if (self.records.get(mapping_id) is not record or record["revision"] != revision
                or not self.manager._get(mapping_id).get("auto_reconnect_enabled", True)):
            raise RuntimeError("自动重连已取消。")
        reason = self._invalid_reason(mapping_id)
        if reason:
            raise RuntimeError(reason)

    def _invalid_reason(self, mapping_id):
        mapping = self.manager._get(mapping_id)
        reason = self.manager._daily_checks._invalid_reason(mapping)
        if reason:
            return reason
        if configuration_signature(self.manager.config_path) != self.manager._config_signature:
            return "SSH 配置正在变化；请确认路径后手动启动，未自动重连。"
        return self.storage_error

    def _wait(self, mapping_id, record, message):
        record.update(state="waiting", due=self.clock() + RETRY_SECONDS,
                      next_retry=(datetime.now(timezone.utc) + timedelta(seconds=RETRY_SECONDS)).isoformat(timespec="seconds"),
                      message=message)
        self.refresh(mapping_id)

    def disconnected(self, mapping_id, reason):
        """Caller holds mapping lock; mark unavailable and schedule first retry."""
        with self.manager._lock:
            mapping = self.manager._get(mapping_id)
            record = self.records.get(mapping_id)
            if not record or record["state"] not in {"monitoring"}:
                return
        # A failed two-leg tunnel must release its surviving leg even when
        # automatic recovery is disabled or blocked by a config change.
        try:
            self.manager._cleanup(mapping_id)
        except (OSError, RuntimeError) as exc:
            reason += f" 清理未完成：{exc}"
        with self.manager._lock:
            if (self.records.get(mapping_id) is not record
                    or self.manager._daily_checks._intent.get(mapping_id) != record["generation"]):
                return
            mapping.update(status="error", error=reason, usage=None, target_usage=None,
                           health={"ok": False, "tunnel_ok": False, "target_ok": None,
                                   "summary": reason, "details": [reason]})
            invalid = self._invalid_reason(mapping_id)
            if invalid:
                record.update(state="blocked", message=invalid)
            elif mapping.get("auto_reconnect_enabled", True):
                self._wait(mapping_id, record, f"检测到断线，{RETRY_SECONDS} 秒后尝试重连（最多 {MAX_ATTEMPTS} 次）。")
            else:
                record.update(state="idle", message="连接已断开；自动重连已关闭。")
            self.refresh(mapping_id)
            self.manager._log(mapping_id, "warning", reason + " " + record["message"])

    def tick(self, *, background=True):
        launched = []
        with self.manager._lock:
            if self.manager._closed.is_set():
                return launched
            ids = list(self.records)
        for mapping_id in ids:
            with self.manager._lock:
                record = self.records.get(mapping_id)
                if (not record or mapping_id in self._inflight or record["state"] not in {"monitoring", "waiting"}
                        or self.manager._daily_checks._intent.get(mapping_id) != record["generation"]):
                    continue
                deadline = record.get("due") if record["state"] == "waiting" else record.get("probe_due", 0)
                if deadline is not None and self.clock() < deadline:
                    continue
                if not self._slots.acquire(blocking=False):
                    break
                self._inflight.add(mapping_id)
                if background:
                    worker = threading.Thread(target=self._work, args=(mapping_id, record), daemon=True,
                                              name=f"reconnect-{mapping_id[:8]}")
                    self._workers.add(worker)
                    worker.start()
            launched.append(mapping_id)
            if not background:
                self._work(mapping_id, record)
        return launched

    def _work(self, mapping_id, record):
        lock = self.manager._mapping_locks.get(mapping_id)
        acquired = bool(lock and lock.acquire(blocking=False))
        try:
            if not acquired:
                return
            with self.manager._lock:
                if (self.manager._closed.is_set() or self.records.get(mapping_id) is not record
                        or self.manager._daily_checks._intent.get(mapping_id) != record["generation"]):
                    return
                mapping = self.manager._get(mapping_id)
                if record["state"] == "monitoring":
                    record["probe_due"] = self.clock() + RETRY_SECONDS
                    monitoring = True
                else:
                    monitoring = False
                    if record["state"] != "waiting" or not mapping.get("auto_reconnect_enabled", True):
                        return
                    reason = self._invalid_reason(mapping_id)
                    if reason:
                        record.update(state="blocked", message=reason, due=None, next_retry=None)
                        self.refresh(mapping_id)
                        return
                    record.update(state="reconnecting", attempts=record["attempts"] + 1, next_retry=None,
                                  message=f"正在重连，第 {record['attempts'] + 1}/{MAX_ATTEMPTS} 次。")
                    self._guards[mapping_id] = (threading.current_thread(), record, record["revision"])
                    self.refresh(mapping_id)
            if monitoring:
                reason = self.manager._transport_failure(mapping_id)
                if reason:
                    self.disconnected(mapping_id, reason)
                return
            self.manager._log(mapping_id, "info", record["message"])
            try:
                self.manager._daily_checks.ensure_current(mapping_id, record["generation"])
                self.manager._cleanup(mapping_id)
                self.manager._daily_checks.ensure_current(mapping_id, record["generation"])
                result = self.manager._start_mapping(mapping_id, record["generation"])
                self.manager._daily_checks.ensure_current(mapping_id, record["generation"])
                if (result.get("health") or {}).get("tunnel_ok") is not True:
                    raise RuntimeError(result.get("error") or "重连后检查未通过。")
            except Exception as exc:
                with self.manager._lock:
                    if (self.records.get(mapping_id) is not record
                            or self.manager._daily_checks._intent.get(mapping_id) != record["generation"]):
                        return
                    if not mapping.get("auto_reconnect_enabled", True):
                        return
                    reason = self._invalid_reason(mapping_id)
                    if reason or record["revision"] != self._guards[mapping_id][2]:
                        record.update(state="blocked", message=reason or "设置已变化，请手动启动。")
                    elif record["attempts"] >= MAX_ATTEMPTS:
                        message = f"连续 {MAX_ATTEMPTS} 次重连失败，已停止重试；请手动启动。最后一次失败：{exc}"
                        self.exhausted[mapping_id] = message
                        try:
                            self._write()
                        except OSError as error:
                            self.storage_error = f"自动重连停止记录无法保存，已暂停自动重连：{error}"
                        record.update(state="exhausted", message=message, due=None, next_retry=None)
                        mapping.update(status="error", error=message)
                        self.manager._daily_checks.disarm(mapping_id)
                    else:
                        self._wait(mapping_id, record, f"第 {record['attempts']}/{MAX_ATTEMPTS} 次重连失败，{RETRY_SECONDS} 秒后重试：{exc}")
                    self.refresh(mapping_id)
                    self.manager._log(mapping_id, "warning", record["message"])
                return
            with self.manager._lock:
                if (self.records.get(mapping_id) is not record
                        or self.manager._daily_checks._intent.get(mapping_id) != record["generation"]):
                    return
                record.update(state="monitoring", attempts=0, due=None, next_retry=None,
                              probe_due=self.clock() + RETRY_SECONDS, message="已自动重连。")
                self.refresh(mapping_id)
                self.manager._log(mapping_id, "info", "已自动重连。")
        finally:
            if acquired:
                lock.release()
            with self.manager._lock:
                guard = self._guards.get(mapping_id)
                if guard and guard[0] is threading.current_thread():
                    self._guards.pop(mapping_id, None)
                self._inflight.discard(mapping_id)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def close(self):
        deadline = time.monotonic() + 3
        with self.manager._lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
