from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
import json
import sys
import unittest

from support import temp_directory
from jumper_manager import update_install as install


class UpdateInstallTests(unittest.TestCase):
    def prepare(self, root, *, active=False, companions=None):
        target = root / ('JumperManager.exe' if sys.platform == 'win32' else 'jumper-manager')
        target.write_bytes(b'old executable')
        source = root / 'download' / target.name
        source.parent.mkdir()
        source.write_bytes(b'new executable')
        for relative, content in (companions or {}).items():
            path = source.parent / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        runtime = {'instance_id': 'old', 'port': 9888, 'url': 'http://127.0.0.1:9888',
                   'ssh_config': '/custom/config', 'tray_ready': False} if active else None
        snapshot = {'runtime': runtime, 'service': False, 'active': [
            {'id': 'mapping', 'name': 'Mapping', 'ssh_timeout': 600}] if active else []}
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(target)), \
                patch.object(install, '_snapshot', return_value=snapshot):
            plan = install.prepare_install(root, source, '1.2.3')
        return plan, install._read(plan), snapshot

    def test_prepare_keeps_original_and_data_and_pins_hashes(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            (root / 'data').mkdir()
            config = root / 'data' / 'mappings.json'
            config.write_text('{"custom":"kept"}')
            plan, data, _ = self.prepare(root)
            self.assertEqual(Path(data['target']).read_bytes(), b'old executable')
            self.assertEqual(install._hash(data['candidate']), data['sha256'])
            self.assertEqual(install._hash(data['helper']), data['previous_sha256'])
            self.assertEqual(config.read_text(), '{"custom":"kept"}')
            self.assertEqual(install._read(plan.parent / 'status.json')['status'], 'prepared')

    def test_stopped_install_replaces_executable_but_does_not_start(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, snapshot = self.prepare(root)
            with patch.object(sys, 'executable', data['helper']), patch.object(install, '_probe'), \
                    patch.object(install, '_snapshot', return_value=snapshot), \
                    patch.object(install, '_stop'), patch.object(install, '_running', return_value=None), \
                    patch.object(install.subprocess, 'Popen') as popen:
                self.assertEqual(install.apply_install(plan), 0)
            popen.assert_not_called()
            self.assertEqual(Path(data['target']).read_bytes(), b'new executable')
            self.assertEqual(Path(data['backup']).read_bytes(), b'old executable')
            self.assertEqual(install._read(plan.parent / 'status.json')['status'], 'success')

    def test_tampered_candidate_does_not_stop_application(self):
        with temp_directory() as temporary:
            plan, data, _ = self.prepare(Path(temporary).resolve(), active=True)
            Path(data['candidate']).write_bytes(b'tampered')
            with patch.object(sys, 'executable', data['helper']), patch.object(install, '_stop') as stop:
                self.assertEqual(install.apply_install(plan), 1)
            stop.assert_not_called()
            self.assertEqual(Path(data['target']).read_bytes(), b'old executable')

    def test_wrong_helper_cannot_install(self):
        with temp_directory() as temporary:
            plan, data, _ = self.prepare(Path(temporary).resolve())
            with self.assertRaisesRegex(install.InstallError, '助手位置'):
                install._validate(plan, helper=True)

    def test_plan_paths_cannot_escape_installation(self):
        with temp_directory() as temporary:
            plan, data, _ = self.prepare(Path(temporary).resolve())
            data['target'] = str(plan.parent / 'another-program')
            install._write(plan, data)
            with self.assertRaises(install.InstallError):
                install._validate(plan)

    def test_failed_probe_keeps_old_executable_and_running_app(self):
        with temp_directory() as temporary:
            plan, data, _ = self.prepare(Path(temporary).resolve(), active=True)
            with patch.object(sys, 'executable', data['helper']), \
                    patch.object(install, '_probe', side_effect=install.InstallError('bad candidate')), \
                    patch.object(install, '_stop') as stop:
                self.assertEqual(install.apply_install(plan), 1)
            stop.assert_not_called()
            self.assertEqual(Path(data['target']).read_bytes(), b'old executable')
            self.assertIn('bad candidate', install._read(plan.parent / 'status.json')['message'])

    def test_start_failure_rolls_back_executable_and_restores_old_run(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, snapshot = self.prepare(root, active=True)
            with patch.object(sys, 'executable', data['helper']), patch.object(install, '_probe'), \
                    patch.object(install, '_snapshot', return_value=snapshot), \
                    patch.object(install, '_stop'), patch.object(install, '_running', return_value=None), \
                    patch.object(install, '_start', side_effect=[RuntimeError('cannot start'), snapshot['runtime']]) as start, \
                    patch.object(install, '_restore', return_value=[]) as restore, \
                    patch.object(install, '_failure_notice'):
                self.assertEqual(install.apply_install(plan), 1)
            self.assertTrue(start.call_args.kwargs['rollback'])
            restore.assert_called_once()
            self.assertEqual(Path(data['target']).read_bytes(), b'old executable')
            state = install._read(plan.parent / 'status.json')
            self.assertIsNone(state['rollback_error'])
            self.assertIn('已恢复原版本', state['message'])

    def test_instance_change_never_stops_unrelated_app(self):
        root = Path('/installation')
        with patch.object(install, '_running', return_value={'instance_id': 'new'}), \
                patch.object(install, '_request') as request:
            with self.assertRaises(install.InstallError):
                install._stop(root, {'instance_id': 'old'}, False)
        request.assert_not_called()

    def test_snapshot_refuses_inflight_mapping_operation(self):
        runtime = {'instance_id': 'old', 'url': 'http://127.0.0.1:9888'}
        with patch.object(install, '_running', return_value=runtime), \
                patch.object(install, '_service', return_value=None), \
                patch.object(install, '_request', return_value={'mappings': [{'status': 'starting'}]}):
            with self.assertRaisesRegex(install.InstallError, '映射正在'):
                install._snapshot(Path('/installation'))

    def test_restore_uses_per_mapping_wait_and_skips_already_started(self):
        runtime = {'instance_id': 'new', 'url': 'http://127.0.0.1:9888'}
        data = {'root': '/installation', 'snapshot': {'active': [
            {'id': 'active', 'name': 'Active', 'ssh_timeout': 30},
            {'id': 'slow', 'name': 'Slow', 'ssh_timeout': 600}]}}
        state = {'mappings': [{'id': 'active', 'status': 'running'}, {'id': 'slow', 'status': 'stopped'}]}
        with patch.object(install, '_running', return_value=runtime), \
                patch.object(install, '_request', side_effect=[{'token': 'token'}, state, state, {}]) as request:
            self.assertEqual(install._restore(data, runtime), [])
        self.assertEqual(request.call_args.kwargs['timeout'], 4860)
        self.assertTrue(request.call_args.args[0].endswith('/slow/start'))
        self.assertEqual(request.call_count, 4)

    def test_resume_marker_matches_plan_hash_and_is_one_shot(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, _ = self.prepare(root)
            install._status(plan, 'running', 'updating')
            marker = root / 'data' / 'update-resume.json'
            install._write(marker, {'plan': str(plan), 'created': install.time.time(), 'sha256': data['previous_sha256']})
            self.assertTrue(install.consume_resume(root))
            self.assertFalse(marker.exists())
            self.assertFalse(install.consume_resume(root))

    def test_resume_marker_does_not_disable_autostart_for_finished_update(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, _ = self.prepare(root)
            install._status(plan, 'success', 'done')
            marker = root / 'data' / 'update-resume.json'
            install._write(marker, {'plan': str(plan), 'created': install.time.time(), 'sha256': data['previous_sha256']})
            self.assertFalse(install.consume_resume(root))

    def test_update_lock_excludes_concurrent_helpers(self):
        with temp_directory() as temporary:
            path = Path(temporary) / 'update.lock'
            one = install._Lock(path).acquire()
            try:
                with self.assertRaises(install.InstallError):
                    install._Lock(path).acquire()
            finally:
                one.close()
            install._Lock(path).acquire().close()

    def test_package_documents_update_without_touching_user_data(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            (root / 'README.md').write_text('old docs')
            (root / 'data').mkdir()
            (root / 'data' / 'mappings.json').write_text('saved config')
            plan, data, snapshot = self.prepare(root, companions={
                'README.md': 'new docs', 'licenses/sample.txt': 'new license', 'data/mappings.json': 'bad config'})
            with patch.object(sys, 'executable', data['helper']), patch.object(install, '_probe'), \
                    patch.object(install, '_snapshot', return_value=snapshot), \
                    patch.object(install, '_stop'), patch.object(install, '_running', return_value=None):
                self.assertEqual(install.apply_install(plan), 0)
            self.assertEqual((root / 'README.md').read_text(), 'new docs')
            self.assertEqual((root / 'licenses/sample.txt').read_text(), 'new license')
            self.assertEqual((root / 'data/mappings.json').read_text(), 'saved config')

    def test_failed_restart_rolls_back_documents_and_new_package_files(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            (root / 'README.md').write_text('old docs')
            plan, data, snapshot = self.prepare(root, active=True, companions={
                'README.md': 'new docs', 'licenses/sample.txt': 'new license'})
            with patch.object(sys, 'executable', data['helper']), patch.object(install, '_probe'), \
                    patch.object(install, '_snapshot', return_value=snapshot), \
                    patch.object(install, '_stop'), patch.object(install, '_running', return_value=None), \
                    patch.object(install, '_start', side_effect=[RuntimeError('cannot start'), snapshot['runtime']]), \
                    patch.object(install, '_restore', return_value=[]), patch.object(install, '_failure_notice'):
                self.assertEqual(install.apply_install(plan), 1)
            self.assertEqual((root / 'README.md').read_text(), 'old docs')
            self.assertFalse((root / 'licenses/sample.txt').exists())

    def test_user_edit_after_staging_is_preserved_and_prevents_shutdown(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            (root / 'README.md').write_text('old docs')
            plan, data, _ = self.prepare(root, active=True, companions={'README.md': 'new docs'})
            (root / 'README.md').write_text('user edit')
            with patch.object(sys, 'executable', data['helper']), patch.object(install, '_stop') as stop:
                self.assertEqual(install.apply_install(plan), 1)
            stop.assert_not_called()
            self.assertEqual((root / 'README.md').read_text(), 'user edit')

    def test_start_records_owned_instance_even_when_version_check_fails(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, _ = self.prepare(root, active=True)
            current = {'instance_id': 'new', 'port': 9888, 'version': 'wrong', 'update_id': plan.parent.name}
            with patch.object(install, '_running', side_effect=[None, current]), \
                    patch.object(install.subprocess, 'Popen'):
                with self.assertRaisesRegex(install.InstallError, '版本或端口'):
                    install._start(plan, data)
            self.assertEqual(data['_started_runtime'], current)

    def test_start_does_not_adopt_unrelated_instance_with_same_version(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, _ = self.prepare(root, active=True)
            current = {'instance_id': 'new', 'port': 9888, 'version': '1.2.3', 'update_id': 'unrelated'}
            with patch.object(install, '_running', side_effect=[None, current]), \
                    patch.object(install.subprocess, 'Popen'):
                with self.assertRaisesRegex(install.InstallError, '其他运行实例'):
                    install._start(plan, data)
            self.assertNotIn('_started_runtime', data)

    def test_systemd_restart_uses_original_service_and_verified_resume_nonce(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, _ = self.prepare(root, active=True)
            data['snapshot']['service'] = True
            runtime = {'instance_id': 'new', 'port': 9888, 'version': '1.2.3', 'update_id': plan.parent.name}
            service = Mock()
            with patch.object(install, '_running', side_effect=[None, runtime]), \
                    patch.object(install, '_service', return_value=service), \
                    patch.object(install.subprocess, 'Popen') as popen:
                self.assertEqual(install._start(plan, data), runtime)
            service.start.assert_called_once()
            popen.assert_not_called()
            marker = install._read(root / 'data' / 'update-resume.json')
            self.assertEqual(marker['plan'], str(plan))
            self.assertEqual(marker['sha256'], data['sha256'])

    def test_replayed_plan_keeps_completed_status_intact(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, _ = self.prepare(root)
            install._status(plan, 'success', 'previous success')
            with patch.object(sys, 'executable', data['helper']), patch.object(install, '_stop') as stop:
                self.assertEqual(install.apply_install(plan), 1)
            stop.assert_not_called()
            self.assertEqual(install._read(plan.parent / 'status.json')['message'], 'previous success')

    def test_helper_exit_rechecks_final_status_before_reporting_failure(self):
        with temp_directory() as temporary:
            root = Path(temporary).resolve()
            plan, data, _ = self.prepare(root)
            process = Mock()
            process.poll.return_value = 0
            with patch.object(install, '_validate', return_value=data), \
                    patch.object(install.subprocess, 'Popen', return_value=process), \
                    patch.object(install, '_read', side_effect=[{'status': 'running'}, {'status': 'success'}]):
                self.assertEqual(install.launch_install(plan, wait=True)['status'], 'success')


if __name__ == '__main__':
    unittest.main()
