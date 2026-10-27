"""Daily recovery: deterministic calendar, persistence and manual-action races."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from support import temp_directory
from test_engine import fake_refresh, payload
from jumper_manager.daily_checks import local_now
from jumper_manager.engine import Manager


class DailyCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = temp_directory()
        self.root = Path(self.temp.__enter__())
        self.refresh = patch.object(Manager, "refresh_hosts", fake_refresh)
        self.refresh.start()
        self.manager = Manager(self.root, str(self.root / "config"))
        self.current = datetime(2026, 10, 25, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        self.scheduler = self.manager._daily_checks
        self.scheduler.clock = lambda: self.current

    def tearDown(self):
        self.manager.close()
        self.refresh.stop()
        self.temp.__exit__(None, None, None)

    def create_started(self, **changes):
        mapping = self.manager.create(payload(daily_check_enabled=True, **changes))
        mapping_id = mapping["id"]
        with patch.object(self.manager, "_start_mapping", side_effect=lambda *args: self.healthy(mapping_id)):
            self.manager.start(mapping_id)
        return mapping_id

    def healthy(self, mapping_id, *, target=True):
        mapping = self.manager._get(mapping_id)
        mapping.update(status="running", health={"tunnel_ok": True, "target_ok": target, "ok": target})
        return copy.deepcopy(mapping)

    def due(self, *, day=25, hour=9, minute=0):
        self.current = self.current.replace(day=day, hour=hour, minute=minute)
        return self.scheduler.tick(background=False)

    def state(self, mapping_id):
        return next(mapping for mapping in self.manager.state()["mappings"] if mapping["id"] == mapping_id)

    def test_defaults_strict_validation_and_old_client_preserves_settings(self):
        default = self.manager.create(payload())
        self.assertFalse(default["daily_check_enabled"])
        self.assertEqual(default["daily_check_time"], "09:00")
        for value in (None, 1, "true", [], {}):
            with self.subTest(enabled=value), self.assertRaises(ValueError):
                self.manager.create(payload(daily_check_enabled=value))
        for value in (None, 9, "9:00", "24:00", "09:60", " 09:00", "09:00:00", "０９:００", "09:00\n"):
            with self.subTest(time=value), self.assertRaises(ValueError):
                self.manager.create(payload(daily_check_time=value))
        for change in ({}, {"name": "unsafe"}, {"daily_check_enabled": 1}, {"daily_check_time": "bad"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.manager.set_schedule(default["id"], change)
        self.manager.set_schedule(default["id"], {"daily_check_enabled": True, "daily_check_time": "23:59"})
        updated = self.manager.update(default["id"], payload(name="old client"))
        self.assertTrue(updated["daily_check_enabled"])
        self.assertEqual(updated["daily_check_time"], "23:59")
        saved = json.loads((self.manager.data / "mappings.json").read_text(encoding="utf-8"))
        saved["mappings"][0].pop("daily_check_enabled")
        saved["mappings"][0].pop("daily_check_time")
        (self.manager.data / "mappings.json").write_text(json.dumps(saved), encoding="utf-8")
        self.manager.close()
        self.manager = Manager(self.root, str(self.root / "config"))
        restored = self.manager.state()["mappings"][0]
        self.assertFalse(restored["daily_check_enabled"])
        self.assertEqual(restored["daily_check_time"], "09:00")

    def test_schedule_can_change_live_and_rolls_back_if_save_fails(self):
        mapping_id = self.create_started()
        changed = self.manager.set_schedule(mapping_id, {"daily_check_time": "10:15"})
        self.assertEqual(changed["status"], "running")
        self.assertTrue(changed["daily_check_enabled"])
        self.assertEqual(changed["scheduled_check"]["next_run"], "2026-10-25T10:15:00+08:00")
        with patch.object(self.manager, "_save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.manager.set_schedule(mapping_id, {"daily_check_enabled": False})
        self.assertTrue(self.state(mapping_id)["daily_check_enabled"])

    def test_healthy_tunnel_checked_once_without_restart_even_with_late_target(self):
        mapping_id = self.create_started()
        with patch.object(self.manager, "check", side_effect=lambda key: self.healthy(key, target=False)) as check, \
             patch.object(self.manager, "_start_mapping") as restart:
            self.assertEqual(self.scheduler.tick(background=False), [])
            self.assertEqual(self.due(), [mapping_id])
            self.assertEqual(self.due(hour=10), [])
            self.assertEqual(self.due(day=24), [])
            self.assertEqual(self.due(day=26), [mapping_id])
            self.assertEqual(check.call_count, 2)
            restart.assert_not_called()
        result = self.state(mapping_id)["scheduled_check"]
        self.assertEqual(result["result"], "ok")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["next_run"], "2026-10-27T09:00:00+08:00")

    def test_failed_start_is_eligible_and_reconnect_uses_existing_timeout(self):
        mapping = self.manager.create(payload(daily_check_enabled=True, ssh_timeout=120))
        mapping_id = mapping["id"]
        with patch.object(self.manager, "_endpoint_check", return_value={"ok": False, "message": "SSH timeout"}), \
             self.assertRaisesRegex(RuntimeError, "SSH timeout"):
            self.manager.start(mapping_id)
        self.assertEqual(self.state(mapping_id)["status"], "error")
        self.assertTrue(self.state(mapping_id)["scheduled_check"]["eligible"])
        seen = []

        def restart(key, *args):
            seen.append(self.manager._get(key)["ssh_timeout"])
            return self.healthy(key)

        with patch.object(self.manager, "_start_mapping", side_effect=restart) as start:
            self.due()
            self.due(hour=10)
            self.assertEqual(start.call_count, 1)
        self.assertEqual(seen, [120])
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["result"], "reconnected")

    def test_failed_reconnection_waits_until_next_day(self):
        mapping_id = self.create_started()
        self.manager._get(mapping_id).update(status="error", health={"tunnel_ok": False})
        with patch.object(self.manager, "_start_mapping", side_effect=RuntimeError("authentication failed")) as start:
            self.due()
            self.due(hour=23)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(self.state(mapping_id)["scheduled_check"]["result"], "failed")
            self.due(day=26)
            self.assertEqual(start.call_count, 2)

    def test_enabling_or_starting_after_time_begins_tomorrow(self):
        self.current = self.current.replace(hour=10)
        mapping_id = self.create_started()
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["next_run"], "2026-10-26T09:00:00+08:00")
        with patch.object(self.manager, "check", side_effect=self.healthy) as check:
            self.assertEqual(self.scheduler.tick(background=False), [])
            self.manager.set_schedule(mapping_id, {"daily_check_time": "11:00"})
            self.assertEqual(self.state(mapping_id)["scheduled_check"]["next_run"], "2026-10-25T11:00:00+08:00")
            self.due(hour=11)
            self.assertEqual(check.call_count, 1)
        self.manager.stop(mapping_id)
        self.assertIsNone(self.state(mapping_id)["scheduled_check"]["next_run"])

    def test_sleep_crossing_deadline_catches_up_once_even_on_later_day(self):
        mapping_id = self.create_started()
        with patch.object(self.manager, "check", side_effect=self.healthy) as check:
            self.assertEqual(self.due(day=26, hour=8), [mapping_id])
            self.assertEqual(self.due(day=26, hour=9), [])
            self.assertEqual(check.call_count, 1)
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["next_run"], "2026-10-27T09:00:00+08:00")

    def test_same_settings_do_not_reset_overdue_deadline(self):
        mapping_id = self.create_started()
        self.current = self.current.replace(hour=10)
        self.manager.set_schedule(mapping_id, {"daily_check_enabled": True, "daily_check_time": "09:00"})
        with patch.object(self.manager, "check", side_effect=self.healthy) as check:
            self.scheduler.tick(background=False)
            check.assert_called_once_with(mapping_id)

    def test_stop_and_disable_prevent_future_recovery(self):
        stopped = self.create_started(name="stopped")
        disabled = self.create_started(name="disabled")
        self.manager.stop(stopped)
        self.manager.set_schedule(disabled, {"daily_check_enabled": False})
        with patch.object(self.manager, "check") as check, patch.object(self.manager, "_start_mapping") as start:
            self.assertEqual(self.due(), [])
            check.assert_not_called()
            start.assert_not_called()
        self.assertFalse(self.state(stopped)["scheduled_check"]["eligible"])
        self.assertEqual(self.state(disabled)["status"], "running")

    def test_manual_stop_during_slow_check_cannot_resurrect(self):
        mapping_id = self.create_started()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def check(key):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"status": "error", "health": {"tunnel_ok": False}}

        def stop():
            try:
                self.manager.stop(mapping_id)
            except Exception as exc:
                errors.append(exc)
            finally:
                stopped.set()

        with patch.object(self.manager, "check", side_effect=check), patch.object(self.manager, "_start_mapping") as start:
            self.current = self.current.replace(hour=9)
            self.scheduler.tick()
            self.assertTrue(entered.wait(2))
            stopper = threading.Thread(target=stop)
            stopper.start()
            deadline = time.monotonic() + 2
            while mapping_id in self.scheduler._intent and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertNotIn(mapping_id, self.scheduler._intent)
            self.assertFalse(stopped.is_set())
            release.set()
            stopper.join(3)
            self.assertTrue(stopped.is_set())
            self.assertFalse(errors)
            start.assert_not_called()
        self.assertEqual(self.state(mapping_id)["status"], "stopped")
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["result"], "skipped")

    def test_disabling_during_check_cancels_restart_without_waiting(self):
        mapping_id = self.create_started()

        def check(key):
            self.manager.set_schedule(key, {"daily_check_enabled": False})
            return {"status": "error", "health": {"tunnel_ok": False}}

        with patch.object(self.manager, "check", side_effect=check), patch.object(self.manager, "_start_mapping") as start:
            self.due()
            start.assert_not_called()
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["result"], "skipped")

    def test_deleted_or_changed_ssh_configuration_does_not_rewire(self):
        changed = self.create_started(name="changed")
        removed = self.create_started(name="removed")
        self.manager._get(changed)["config_changed"] = True
        self.manager._get(removed)["target_host"] = "deleted-alias"
        with patch.object(self.manager, "check") as check, patch.object(self.manager, "_start_mapping") as start:
            self.due()
            check.assert_not_called()
            start.assert_not_called()
        self.assertEqual(self.state(changed)["scheduled_check"]["result"], "skipped")
        self.assertEqual(self.state(removed)["scheduled_check"]["result"], "skipped")

    def test_failed_tunnel_retains_original_route_after_ssh_config_edit(self):
        mapping_id = self.create_started()
        mapping = self.manager._get(mapping_id)
        mapping.update(status="error", health={"tunnel_ok": False})
        original = copy.deepcopy(mapping["route"])
        self.manager._effective = {"server-b": {"hostname": ["different-server"]}}
        with self.manager._lock:
            self.manager._refresh_mapping_plans({})
        self.assertTrue(mapping["config_changed"])
        self.assertEqual(mapping["route"], original)
        with patch.object(self.manager, "_start_mapping") as start:
            self.due()
            start.assert_not_called()
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["result"], "skipped")

    def test_file_change_not_yet_seen_by_watcher_also_blocks_reconnect(self):
        mapping_id = self.create_started()
        self.manager._get(mapping_id).update(status="error", health={"tunnel_ok": False})
        self.manager.config_path.write_text("Host new-device\n HostName example.invalid\n", encoding="utf-8")
        with patch.object(self.manager, "_start_mapping") as start:
            self.due()
            start.assert_not_called()
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["result"], "skipped")

    def test_newer_start_overtaking_queued_stop_keeps_new_connection(self):
        mapping_id = self.create_started()
        waiting, release = threading.Event(), threading.Event()
        underlying = threading.RLock()

        class DelayedStopLock:
            def __enter__(self):
                if threading.current_thread().name == "delayed-user-stop":
                    waiting.set()
                    if not release.wait(3):
                        raise RuntimeError("test stop was not released")
                underlying.acquire()

            def __exit__(self, *args):
                underlying.release()

            def acquire(self, **kwargs):
                return underlying.acquire(**kwargs)

            def release(self):
                underlying.release()

        self.manager._mapping_locks[mapping_id] = DelayedStopLock()
        with patch.object(self.manager, "_cleanup") as cleanup:
            stopper = threading.Thread(target=self.manager.stop, args=(mapping_id,), name="delayed-user-stop")
            stopper.start()
            self.assertTrue(waiting.wait(2))
            with patch.object(self.manager, "_start_mapping", side_effect=lambda *args: self.healthy(mapping_id)):
                self.manager.start(mapping_id)
            release.set()
            stopper.join(3)
            self.assertFalse(stopper.is_alive())
            cleanup.assert_not_called()
        self.assertEqual(self.state(mapping_id)["status"], "running")
        self.assertTrue(self.state(mapping_id)["scheduled_check"]["eligible"])

    def test_stop_during_startup_cancels_before_spawning(self):
        mapping = self.manager.create(payload(daily_check_enabled=True))
        mapping_id = mapping["id"]
        entered, release = threading.Event(), threading.Event()
        errors = []

        def available(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test not released")
            return {"ok": True}

        def start():
            try:
                self.manager.start(mapping_id)
            except RuntimeError as exc:
                errors.append(str(exc))

        with patch.object(self.manager, "_endpoint_check", side_effect=available), patch.object(self.manager, "_spawn") as spawn:
            starter = threading.Thread(target=start)
            starter.start()
            self.assertTrue(entered.wait(2))
            stopper = threading.Thread(target=self.manager.stop, args=(mapping_id,))
            stopper.start()
            deadline = time.monotonic() + 2
            while mapping_id in self.scheduler._intent and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertNotIn(mapping_id, self.scheduler._intent)
            release.set()
            starter.join(3)
            stopper.join(3)
            spawn.assert_not_called()
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.state(mapping_id)["status"], "stopped")

    def test_delete_keeps_mapping_if_newer_start_supersedes_its_stop(self):
        mapping_id = self.create_started()
        with patch.object(self.manager, "stop", return_value=self.healthy(mapping_id)), \
             self.assertRaisesRegex(RuntimeError, "取消删除"):
            self.manager.delete(mapping_id)
        self.assertEqual(self.state(mapping_id)["status"], "running")
        saved = json.loads((self.manager.data / "mappings.json").read_text(encoding="utf-8"))
        self.assertIn(mapping_id, [mapping["id"] for mapping in saved["mappings"]])

    def test_persistence_prevents_repeat_after_restart_and_cold_start_stays_stopped(self):
        mapping_id = self.create_started()
        with patch.object(self.manager, "check", side_effect=self.healthy):
            self.due()
        self.manager.close()
        self.manager = Manager(self.root, str(self.root / "config"))
        self.scheduler = self.manager._daily_checks
        self.current = self.current.replace(hour=8)
        self.scheduler.clock = lambda: self.current
        self.assertFalse(self.state(mapping_id)["scheduled_check"]["eligible"])
        self.assertEqual(self.state(mapping_id)["status"], "stopped")
        with patch.object(self.manager, "_start_mapping", side_effect=lambda *args: self.healthy(mapping_id)):
            self.manager.start(mapping_id)
        with patch.object(self.manager, "check") as check:
            self.due()
            check.assert_not_called()
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["result"], "ok")

    def test_malformed_or_unwritable_checkpoint_never_runs_network_action(self):
        mapping_id = self.create_started()
        with patch.object(self.scheduler, "writer", side_effect=OSError("read only")), \
             patch.object(self.manager, "check") as check:
            self.due()
            self.due(hour=10)
            check.assert_not_called()
        self.assertEqual(self.state(mapping_id)["scheduled_check"]["result"], "failed")
        self.manager.close()
        (self.manager.data / "daily-checks.json").write_text("invalid json", encoding="utf-8")
        self.manager = Manager(self.root, str(self.root / "config"))
        self.scheduler = self.manager._daily_checks
        self.scheduler.clock = lambda: self.current
        with patch.object(self.manager, "_start_mapping", side_effect=lambda *args: self.healthy(mapping_id)):
            self.manager.start(mapping_id)
        self.assertIn("无法读取", self.state(mapping_id)["scheduled_check"]["message"])
        with patch.object(self.manager, "check") as check:
            self.due(day=26)
            check.assert_not_called()

    def test_slow_check_does_not_block_other_tunnel_and_no_overlap(self):
        first = self.create_started(name="slow")
        second = self.create_started(name="fast")
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def check(key):
            calls.append(key)
            if key == first:
                entered.set()
                self.assertTrue(release.wait(3))
            else:
                completed.set()
            return self.healthy(key)

        with patch.object(self.manager, "check", side_effect=check):
            self.current = self.current.replace(hour=9)
            self.scheduler.tick()
            self.assertTrue(entered.wait(2))
            self.assertTrue(completed.wait(2))
            self.scheduler.tick()
            release.set()
            for worker in list(self.scheduler._workers):
                worker.join(3)
        self.assertEqual(calls.count(first), 1)
        self.assertEqual(calls.count(second), 1)

    def test_worker_limit_and_busy_manual_operation_do_not_consume_extra_attempts(self):
        ids = [self.create_started(name=f"mapping-{number}") for number in range(6)]
        release, full = threading.Event(), threading.Event()
        calls, counter = [], threading.Lock()

        def check(key):
            with counter:
                calls.append(key)
                if len(calls) == 4:
                    full.set()
            self.assertTrue(release.wait(3))
            return self.healthy(key)

        with patch.object(self.manager, "check", side_effect=check):
            self.current = self.current.replace(hour=9)
            self.scheduler.tick()
            self.assertTrue(full.wait(2))
            self.assertEqual(len(calls), 4)
            self.assertEqual(self.scheduler.tick(), [])
            release.set()
            for worker in list(self.scheduler._workers):
                worker.join(3)
            self.scheduler.tick(background=False)
        self.assertCountEqual(calls, ids)

        busy = self.create_started(name="busy", daily_check_time="10:00")
        held, unlock = threading.Event(), threading.Event()

        def manual_operation():
            with self.manager._mapping_locks[busy]:
                held.set()
                unlock.wait(3)

        owner = threading.Thread(target=manual_operation)
        owner.start()
        self.assertTrue(held.wait(2))
        with patch.object(self.manager, "check", side_effect=self.healthy) as check:
            self.current = self.current.replace(hour=10)
            self.scheduler.tick(background=False)
            self.assertNotIn(busy, self.scheduler.history)
            unlock.set()
            owner.join(3)
            self.scheduler.tick(background=False)
            check.assert_called_once_with(busy)

    @unittest.skipUnless(hasattr(time, "tzset"), "POSIX timezone rule regression")
    def test_os_dst_gap_does_not_fire_early_and_fold_does_not_repeat(self):
        previous = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "EST5EDT,M3.2.0,M11.1.0"
            time.tzset()
            self.current = datetime(2026, 3, 8, 0, 0).astimezone()
            mapping_id = self.create_started(daily_check_time="02:30")
            self.scheduler.clock = local_now  # Exercise production DST resolution.
            with patch.object(self.manager, "check", side_effect=self.healthy) as check:
                self.scheduler.tick(datetime(2026, 3, 8, 1, 45).astimezone(), background=False)
                check.assert_not_called()
                self.scheduler.tick(datetime(2026, 3, 8, 3, 0).astimezone(), background=False)
                check.assert_called_once_with(mapping_id)
            self.scheduler.clock = lambda: self.current
            self.current = datetime(2026, 11, 1, 0, 0).astimezone()
            self.manager.set_schedule(mapping_id, {"daily_check_time": "01:30"})
            with patch.object(self.manager, "check", side_effect=self.healthy) as check:
                for fold in (0, 1):
                    self.scheduler.tick(datetime(2026, 11, 1, 1, 45, fold=fold).astimezone(), background=False)
                check.assert_called_once_with(mapping_id)
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()


if __name__ == "__main__":
    unittest.main()
