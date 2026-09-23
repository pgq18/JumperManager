from contextlib import redirect_stderr, redirect_stdout
import io
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import unittest

from test_desktop import loaded_app
from test_cli import FakeAPI


class LinuxEntryTests(unittest.TestCase):
    def test_frozen_linux_defaults_to_background_without_tray_or_browser(self):
        with loaded_app(frozen=True, windows=False) as app, patch.object(app.sys, 'argv', ['jumper-manager', 'start']), patch.object(app, 'launch', return_value=0) as launch:
            self.assertEqual(app.main(), 0)
            self.assertFalse(launch.call_args.args[0].tray)
            self.assertTrue(launch.call_args.args[0].no_browser)

    def test_frozen_linux_launch_spawns_itself_in_a_new_session(self):
        with loaded_app(frozen=True, windows=False) as app, patch.object(app, 'running', side_effect=[None, {'url':'http://127.0.0.1:8876'}]), patch.object(app, 'linux_service', return_value=None), patch.object(app.Path, 'mkdir'), patch.object(app.Path, 'open') as opened, patch.object(app.subprocess, 'Popen') as spawn:
            args = SimpleNamespace(no_browser=True, port=8876, ssh_config=None, tray=False)
            self.assertEqual(app.launch(args), 0)
            self.assertEqual(spawn.call_args.args[0][0], app.sys.executable)
            self.assertNotIn('app.py', ' '.join(spawn.call_args.args[0]))
            self.assertIn('--serve', spawn.call_args.args[0])
            self.assertEqual(spawn.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
            self.assertFalse(args.tray)

    def test_request_supports_authenticated_put_and_delete(self):
        with loaded_app(windows=False) as app, patch.object(app.OPENER, 'open') as opener:
            opener.return_value.__enter__.return_value.read.return_value = b'{}'
            for method in ('PUT', 'DELETE'):
                app.request('http://127.0.0.1:8765/api/mappings/id', {}, 'token', method=method)
                request = opener.call_args.args[0]
                self.assertEqual(request.get_method(), method)
                self.assertEqual(request.get_header('X-jumper-token'), 'token')

    def test_cli_actions_delegate_without_starting_another_manager(self):
        with loaded_app(windows=False) as app, patch.object(app.sys, 'argv', ['app.py', 'mappings', 'list']), patch('jumper_manager.cli.run', return_value=0) as run, patch.object(app, 'launch') as launch:
            self.assertEqual(app.main(), 0)
            self.assertEqual(run.call_args.args[0], ['mappings', 'list'])
            launch.assert_not_called()

    def test_linux_tray_is_rejected_and_foreground_mode_is_headless(self):
        with loaded_app(windows=False) as app, patch.object(app.sys, 'argv', ['app.py', 'serve', '--tray']):
            with self.assertRaises(SystemExit) as raised:
                app.main()
            self.assertEqual(raised.exception.code, 2)
        with loaded_app(windows=False) as app, patch.object(app.sys, 'argv', ['app.py', 'serve']), patch.object(app, 'serve') as serve:
            app.main()
            self.assertFalse(serve.call_args.args[0].tray)
            self.assertFalse(serve.call_args.args[0].open_browser)

    def test_service_command_for_frozen_linux_uses_installed_executable(self):
        with loaded_app(frozen=True, windows=False) as app:
            command = app.service_command(SimpleNamespace(port=8876, ssh_config=None))
            self.assertEqual(command, [str(app.Path(app.sys.executable).resolve()), '--serve', '--no-tray', '--no-browser', '--port', '8876'])


class LinuxShortcutTests(unittest.TestCase):
    """Exercise entry-point parsing through the real CLI, with no live service."""

    def invoke_mapping_command(self, arguments, api=None):
        api = api or FakeAPI()
        output, errors = io.StringIO(), io.StringIO()
        with loaded_app(windows=False) as app, \
                patch.object(app.sys, 'argv', ['jumper-manager', *arguments]), \
                patch.object(app, 'running', side_effect=api.running), \
                patch.object(app, 'request', side_effect=api.request), \
                patch.object(app, 'launch') as launch, \
                patch.object(app, 'stop_background') as stop, \
                patch.object(app, 'serve') as serve, \
                redirect_stdout(output), redirect_stderr(errors):
            try:
                code = app.main()
            except SystemExit as error:
                code = error.code
            launch.assert_not_called()
            stop.assert_not_called()
            serve.assert_not_called()
        return code, output.getvalue(), errors.getvalue(), api

    def test_list_shortcut_matches_mapping_list_in_text_and_json(self):
        for options in ([], ['--json']):
            with self.subTest(options=options):
                shortcut = self.invoke_mapping_command(['list', *options])
                explicit = self.invoke_mapping_command(['mappings', 'list', *options])
                self.assertEqual(shortcut[:3], explicit[:3])
                self.assertEqual(shortcut[0], 0)
                self.assertEqual(shortcut[3].calls, explicit[3].calls)
                self.assertEqual(shortcut[3].calls[0][1], '/api/state')
                self.assertFalse(any(call[0] != 'GET' for call in shortcut[3].calls))
        code, output, errors, api = self.invoke_mapping_command(['--json', 'list'])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)['mappings'], api.mappings)

    def test_start_stop_shortcuts_resolve_ids_prefixes_and_names_via_authenticated_api(self):
        for action in ('start', 'stop'):
            for reference in ('abc111', 'abc1', 'demo tunnel'):
                with self.subTest(action=action, reference=reference):
                    api = FakeAPI()
                    api.mappings[0].update(name='demo tunnel', status='running' if action == 'stop' else 'stopped')
                    code, output, errors, api = self.invoke_mapping_command([action, reference, '--json'], api)
                    self.assertEqual(code, 0, errors)
                    result = json.loads(output)
                    self.assertEqual(result['id'], 'abc111')
                    self.assertEqual(result['status'], 'running' if action == 'start' else 'stopped')
                    writes = [call for call in api.calls if call[0] != 'GET']
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(writes[0][:4], ('POST', '/api/mappings/abc111/' + action, {}, 'session-token'))
                    self.assertEqual(api.mappings[1]['status'], 'stopped')

    def test_global_json_start_stop_match_original_mapping_commands(self):
        for action in ('start', 'stop'):
            with self.subTest(action=action):
                shortcut = self.invoke_mapping_command(['--json', action, 'demo'])
                explicit = self.invoke_mapping_command(['mappings', action, 'demo', '--json'])
                self.assertEqual(shortcut[:3], explicit[:3])
                self.assertEqual(shortcut[0], 0)
                self.assertEqual(shortcut[3].calls, explicit[3].calls)

    def test_dash_prefixed_mapping_name_is_preserved_after_literal_separator(self):
        for name in ('-demo', '--json', '--port'):
            with self.subTest(name=name):
                api = FakeAPI()
                api.mappings[0]['name'] = name
                code, output, errors, api = self.invoke_mapping_command(['start', '--', name], api)
                self.assertEqual(code, 0, errors)
                self.assertIn('已启动', output)
                self.assertEqual(api.calls[-1][1], '/api/mappings/abc111/start')

    def test_unknown_or_ambiguous_reference_never_falls_back_to_manager_lifecycle(self):
        for action in ('start', 'stop'):
            for reference, duplicate_name in (('missing', False), ('abc', False), ('demo', True)):
                with self.subTest(action=action, reference=reference, duplicate_name=duplicate_name):
                    api = FakeAPI()
                    if duplicate_name:
                        api.mappings[1]['name'] = 'demo'
                    code, output, errors, api = self.invoke_mapping_command([action, reference, '--json'], api)
                    self.assertEqual(code, 2)
                    self.assertIn('error', json.loads(output))
                    self.assertEqual(errors, '')
                    self.assertFalse(any(call[0] != 'GET' for call in api.calls))
                    self.assertNotIn('/api/session', [call[1] for call in api.calls])

    def test_reference_with_manager_options_is_rejected_before_any_api_call(self):
        for action in ('start', 'stop'):
            for options in (['--port', '8876'], ['--port=8876'], ['--po', '8876'],
                            ['--ssh-config', '/tmp/test-config'], ['--ssh-c=/tmp/test-config'],
                            ['--no-browser'], ['--open'], ['--tray'], ['--no-tray'], ['--serve']):
                with self.subTest(action=action, options=options):
                    code, _, _, api = self.invoke_mapping_command([action, 'demo', *options])
                    self.assertEqual(code, 2)
                    self.assertEqual(api.calls, [])

    def test_only_start_and_stop_accept_a_mapping_reference(self):
        for action in ('list', 'status', 'restart', 'serve', 'open'):
            with self.subTest(action=action):
                code, _, _, api = self.invoke_mapping_command([action, 'demo'])
                self.assertEqual(code, 2)
                self.assertEqual(api.calls, [])

    def test_start_stop_without_reference_keep_manager_lifecycle(self):
        for action, options in (('start', []), ('start', ['--port', '8876']), ('stop', [])):
            with self.subTest(action=action, options=options), loaded_app(windows=False) as app, \
                    patch.object(app.sys, 'argv', ['jumper-manager', action, *options]), \
                    patch.object(app, 'launch', return_value=0) as launch, \
                    patch.object(app, 'stop_background', return_value=0) as stop, \
                    patch('jumper_manager.cli.run') as cli_run, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(), 0)
                cli_run.assert_not_called()
                if action == 'start':
                    launch.assert_called_once()
                    stop.assert_not_called()
                    self.assertEqual(launch.call_args.args[0].port, 8876 if options else 8765)
                else:
                    stop.assert_called_once_with()
                    launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
