from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from support import temp_directory
from jumper_manager.linux_service import LinuxUserService, unit_quote


class LinuxServiceTests(unittest.TestCase):
    def test_unit_escaping_preserves_spaces_specifiers_and_dollar_literals(self):
        self.assertEqual(unit_quote('/home/test a/$USER%H/x'), '"/home/test a/$$USER%%H/x"')
        self.assertEqual(unit_quote('a"b\\c'), '"a\\"b\\\\c"')
        with self.assertRaises(ValueError):
            unit_quote('evil\nExecStart=command')

    def test_enable_uses_foreground_command_owned_unit_and_does_not_start(self):
        with temp_directory() as temp:
            root = Path(temp)
            command = [str(root / 'jumper-manager'), '--serve', '--port', '8876', '--no-tray']
            service = LinuxUserService(root, command, config_home=root / 'config')
            with patch.object(service, '_run') as run:
                service.enable()
            text = service.path.read_text(encoding='utf-8')
            self.assertIn('KillMode=control-group', text)
            self.assertIn('Restart=on-failure', text)
            self.assertIn('--serve', text)
            self.assertEqual(service.settings(), command)
            self.assertEqual([call.args[0] for call in run.call_args_list], ['show-environment', 'daemon-reload', 'enable'])

    def test_different_installations_do_not_overwrite_each_other(self):
        with temp_directory() as temp:
            root = Path(temp)
            one = LinuxUserService(root / 'one', config_home=root)
            two = LinuxUserService(root / 'two', config_home=root)
            self.assertNotEqual(one.name, two.name)
            one.path.parent.mkdir(parents=True)
            one.path.write_text('[Service]\nExecStart=/other/app\n', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                one.disable()

    def test_disable_does_not_stop_running_mappings(self):
        with temp_directory() as temp:
            root = Path(temp)
            service = LinuxUserService(root, ['/binary', '--serve'], config_home=root)
            with patch.object(service, '_run') as run:
                service.enable()
                run.reset_mock()
                service.disable()
                run.assert_called_once_with('disable', service.name)

    def test_unavailable_service_manager_does_not_write_unit(self):
        with temp_directory() as temp:
            root = Path(temp)
            service = LinuxUserService(root, ['/binary', '--serve'], config_home=root)
            with patch.object(service, '_run', side_effect=RuntimeError('no user bus')):
                with self.assertRaises(RuntimeError):
                    service.enable()
            self.assertFalse(service.path.exists())


if __name__ == '__main__':
    unittest.main()
