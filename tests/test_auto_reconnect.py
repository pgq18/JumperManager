"""Deterministic bounded recovery and cancellation, never touches live mappings."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from support import temp_directory
from test_engine import fake_refresh, payload
from jumper_manager.engine import Manager


class AutoReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = temp_directory()
        self.root = Path(self.temp.__enter__())
        self.refresh = patch.object(Manager, 'refresh_hosts', fake_refresh)
        self.monitor = patch.object(Manager, '_monitor')
        self.refresh.start()
        self.monitor.start()
        self.manager = Manager(self.root, str(self.root / 'config'))
        self.clock = 100.0
        self.recovery = self.manager._auto_reconnect
        self.recovery.clock = lambda: self.clock
        self.manager._daily_checks.clock = lambda: datetime(2026, 11, 25, 8, tzinfo=timezone.utc)

    def tearDown(self):
        self.manager.close()
        self.monitor.stop()
        self.refresh.stop()
        self.temp.__exit__(None, None, None)

    def healthy(self, key):
        mapping = self.manager._get(key)
        mapping.update(status='running', error=None, health={'tunnel_ok': True, 'target_ok': False})
        return copy.deepcopy(mapping)

    def started(self, **changes):
        key = self.manager.create(payload(**changes))['id']
        with patch.object(self.manager, '_start_mapping', side_effect=lambda *args: self.healthy(key)):
            self.manager.start(key)
        return key

    def view(self, key):
        return next(m for m in self.manager.state()['mappings'] if m['id'] == key)

    def disconnect(self, key):
        with patch.object(self.manager, '_transport_failure', return_value='SSH disconnected'):
            self.recovery.tick(background=False)
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'waiting')

    def fail_three(self, key):
        self.disconnect(key)
        with patch.object(self.manager, '_start_mapping', side_effect=RuntimeError('network offline')) as retry:
            for count in range(1, 4):
                self.clock += 4.9
                self.recovery.tick(background=False)
                self.assertEqual(retry.call_count, count - 1)
                self.clock += .1
                self.recovery.tick(background=False)
                self.assertEqual(retry.call_count, count)
            self.clock += 3600
            self.recovery.tick(background=False)
            self.assertEqual(retry.call_count, 3)
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'exhausted')

    def test_defaults_validation_and_old_client_preserves_disabled_setting(self):
        key = self.manager.create(payload())['id']
        self.assertTrue(self.view(key)['auto_reconnect_enabled'])
        for bad in (1, None, 'yes', [], {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.manager.create(payload(auto_reconnect_enabled=bad))
        for bad in ({}, {'auto_reconnect_enabled': 1}, {'auto_reconnect_enabled': True, 'name': 'bad'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.manager.set_reconnect(key, bad)
        self.manager.set_reconnect(key, {'auto_reconnect_enabled': False})
        self.manager.update(key, payload(name='old client'))
        self.assertFalse(self.view(key)['auto_reconnect_enabled'])
        with patch.object(self.manager, '_save', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.manager.set_reconnect(key, {'auto_reconnect_enabled': True})
        self.assertFalse(self.view(key)['auto_reconnect_enabled'])

    def test_exactly_three_failed_attempts_then_daily_and_restart_cannot_reset(self):
        key = self.started(daily_check_enabled=True, auto_start=True)
        self.fail_three(key)
        self.assertEqual(self.view(key)['auto_reconnect']['attempts'], 3)
        self.assertFalse(self.view(key)['scheduled_check']['eligible'])
        with patch.object(self.manager, '_start_mapping') as retry:
            self.manager._daily_checks.tick(datetime(2026, 11, 26, 9, tzinfo=timezone.utc), background=False)
            self.manager.start_automatic(key)
            retry.assert_not_called()
        self.manager.close()
        self.manager = Manager(self.root, str(self.root / 'config'))
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'exhausted')
        with patch.object(self.manager, '_start_mapping') as retry:
            self.manager.start_automatic(key)
            retry.assert_not_called()
        with patch.object(self.manager, '_start_mapping', side_effect=lambda *args: self.healthy(key)):
            self.manager.start(key)
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'monitoring')
        self.assertEqual(self.view(key)['auto_reconnect']['attempts'], 0)

    def test_success_clears_consecutive_failure_budget(self):
        key = self.started()
        self.disconnect(key)
        self.clock += 5
        with patch.object(self.manager, '_start_mapping', side_effect=RuntimeError('offline')):
            self.recovery.tick(background=False)
        self.assertEqual(self.view(key)['auto_reconnect']['attempts'], 1)
        self.clock += 5
        with patch.object(self.manager, '_start_mapping', side_effect=lambda *args: self.healthy(key)):
            self.recovery.tick(background=False)
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'monitoring')
        self.assertEqual(self.view(key)['auto_reconnect']['attempts'], 0)
        self.clock += 5
        self.fail_three(key)

    def test_manual_stop_cancels_pending_attempt_and_cold_mappings_stay_stopped(self):
        key = self.started()
        self.disconnect(key)
        self.manager.stop(key)
        self.clock += 5
        with patch.object(self.manager, '_start_mapping') as start:
            self.recovery.tick(background=False)
            start.assert_not_called()
        self.assertEqual(self.view(key)['status'], 'stopped')
        self.manager.close()
        self.manager = Manager(self.root, str(self.root / 'config'))
        with patch.object(self.manager, '_start_mapping') as start:
            self.manager._auto_reconnect.tick(background=False)
            start.assert_not_called()

    def test_disable_live_then_enable_preserves_budget_and_monitoring(self):
        key = self.started()
        self.disconnect(key)
        self.manager.set_reconnect(key, {'auto_reconnect_enabled': False})
        self.clock += 5
        with patch.object(self.manager, '_start_mapping') as start:
            self.recovery.tick(background=False)
            start.assert_not_called()
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'disabled')
        self.manager.set_reconnect(key, {'auto_reconnect_enabled': True})
        self.disconnect(key)
        self.clock += 5
        with patch.object(self.manager, '_start_mapping', side_effect=lambda *args: self.healthy(key)) as start:
            self.recovery.tick(background=False)
            start.assert_called_once()

    def test_missing_target_does_not_trigger_disconnect_or_network_probes(self):
        key = self.started()
        entry = {'process': Mock(), 'alias': 'server-b'}
        entry['process'].poll.return_value = None
        self.manager._processes[key] = [entry]
        with patch.object(self.manager, '_read_process_log'), patch.object(self.manager, '_endpoint_check') as probe, \
             patch.object(self.manager, '_sample_usage') as usage, patch.object(self.manager, '_start_mapping') as start:
            self.recovery.tick(background=False)
            probe.assert_not_called()
            usage.assert_not_called()
            start.assert_not_called()
        self.manager._processes.clear()
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'monitoring')

    def test_daily_delegates_once_without_bypassing_retry_gap(self):
        key = self.started(daily_check_enabled=True)
        self.manager._get(key).update(status='error', health={'tunnel_ok': False})
        with patch.object(self.manager, '_start_mapping') as start:
            self.manager._daily_checks.tick(datetime(2026, 11, 25, 9, tzinfo=timezone.utc), background=False)
            start.assert_not_called()
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'waiting')
        self.assertEqual(self.view(key)['auto_reconnect']['attempts'], 0)

    def test_failed_initial_start_has_no_retry_budget(self):
        key = self.manager.create(payload())['id']
        with patch.object(self.manager, '_start_mapping', side_effect=RuntimeError('first failed')), self.assertRaises(RuntimeError):
            self.manager.start(key)
        with patch.object(self.manager, '_start_mapping') as start:
            self.recovery.tick(background=False)
            start.assert_not_called()
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'idle')

    def test_config_change_during_retry_gap_blocks_reroute(self):
        key = self.started()
        self.disconnect(key)
        self.manager.config_path.write_text('Host changed\n', encoding='utf-8')
        self.clock += 5
        with patch.object(self.manager, '_start_mapping') as start:
            self.recovery.tick(background=False)
            start.assert_not_called()
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'blocked')

    def test_disabled_detection_cleans_surviving_resources(self):
        key = self.started(auto_reconnect_enabled=False)
        with patch.object(self.manager, '_transport_failure', return_value='SSH ended'), \
             patch.object(self.manager, '_cleanup') as cleanup:
            self.recovery.tick(background=False)
            cleanup.assert_called_once_with(key)
        self.assertEqual(self.view(key)['status'], 'error')
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'disabled')

    def test_optional_corrupt_checkpoint_still_allows_manual_start(self):
        key = self.manager.create(payload())['id']
        self.manager.close()
        (self.root / 'data' / 'reconnect-exhausted.json').write_text('invalid', encoding='utf-8')
        self.manager = Manager(self.root, str(self.root / 'config'))
        with patch.object(self.manager, '_start_mapping', side_effect=lambda *args: self.healthy(key)) as start:
            self.manager.start_automatic(key)
            start.assert_not_called()
            self.manager.start(key)
            start.assert_called_once()
        self.assertEqual(self.view(key)['status'], 'running')
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'blocked')

    def test_stop_interrupts_retry_before_spawn_and_never_resurrects(self):
        key = self.started()
        self.disconnect(key)
        entered = threading.Event()
        def slow_available(*args, **kwargs):
            entered.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.manager._operation_cancel_event().is_set():
                    return {'ok': False, 'message': 'cancelled'}
                time.sleep(.01)
            raise RuntimeError('not cancelled')
        self.clock += 5
        with patch.object(self.manager, '_endpoint_check', side_effect=slow_available), patch.object(self.manager, '_spawn') as spawn:
            self.recovery.tick()
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            self.manager.stop(key)
            self.assertLess(time.monotonic() - start, 1)
            for worker in list(self.recovery._workers):
                worker.join(2)
            spawn.assert_not_called()
        self.assertEqual(self.view(key)['status'], 'stopped')
        self.assertNotIn(key, self.recovery.records)

    def test_disable_during_retry_cancels_without_restart_or_exhaustion(self):
        key = self.started()
        self.disconnect(key)
        def disable(*args, **kwargs):
            self.manager.set_reconnect(key, {'auto_reconnect_enabled': False})
            self.assertTrue(self.manager._operation_cancel_event().is_set())
            return {'ok': True}
        self.clock += 5
        with patch.object(self.manager, '_endpoint_check', side_effect=disable), patch.object(self.manager, '_spawn') as spawn:
            self.recovery.tick(background=False)
            spawn.assert_not_called()
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'disabled')
        self.assertNotIn(key, self.recovery.exhausted)

    def test_cleanup_failure_counts_against_retry_budget_and_never_spawns(self):
        key = self.started()
        self.disconnect(key)
        with patch.object(self.manager, '_cleanup', side_effect=RuntimeError('ownership unknown')), \
             patch.object(self.manager, '_start_mapping') as start:
            for _ in range(3):
                self.clock += 5
                self.recovery.tick(background=False)
            start.assert_not_called()
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'exhausted')

    def test_exhaustion_write_failure_does_not_mislabel_later_manual_start(self):
        key = self.started()
        with patch.object(self.recovery, 'writer', side_effect=OSError('disk full')):
            self.fail_three(key)
        self.assertIsNotNone(self.recovery.storage_error)
        with patch.object(self.manager, '_start_mapping', side_effect=lambda *args: self.healthy(key)):
            self.manager.start(key)
        self.assertEqual(self.view(key)['status'], 'running')
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'blocked')
        self.assertNotIn(key, self.recovery.exhausted)

    def test_toggle_after_two_failures_cannot_reset_the_remaining_attempt(self):
        key = self.started()
        self.disconnect(key)
        with patch.object(self.manager, '_start_mapping', side_effect=RuntimeError('offline')) as retry:
            for _ in range(2):
                self.clock += 5
                self.recovery.tick(background=False)
            self.manager.set_reconnect(key, {'auto_reconnect_enabled': False})
            self.recovery.tick(background=False)
            self.manager.set_reconnect(key, {'auto_reconnect_enabled': True})
            self.recovery.tick(background=False)
            self.clock += 5
            self.recovery.tick(background=False)
            self.assertEqual(retry.call_count, 3)
        self.assertEqual(self.view(key)['auto_reconnect']['state'], 'exhausted')


if __name__ == '__main__':
    unittest.main()
