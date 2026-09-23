"""Protocol and lifecycle regressions for Python-free SSH endpoint checks."""
import io
import ipaddress
import socket
import subprocess
import types
import unittest
from unittest.mock import MagicMock, patch

from jumper_manager.ssh_probe import socks_connect, ssh_tcp_check, _close_probe


class FragmentedReply:
    def __init__(self, value):
        self.value = bytearray(value)
        self.sent = []

    def recv(self, count):
        chunk = bytes(self.value[:1])
        del self.value[:1]
        return chunk

    def sendall(self, value):
        self.sent.append(value)


class SocksTests(unittest.TestCase):
    def test_fragmented_success_and_remote_dns_ipv4_ipv6(self):
        for address, expected in [('127.0.0.1', b'\x01\x7f\0\0\x01'),
                                  ('::1', b'\x04' + ipaddress.ip_address('::1').packed),
                                  ('remote.internal', b'\x03\x0fremote.internal')]:
            with self.subTest(address=address):
                stream = FragmentedReply(b'\x05\x00\x05\x00\x00\x01' + bytes(6))
                self.assertTrue(socks_connect(stream, address, 50051)['ok'])
                self.assertEqual(stream.sent, [b'\x05\x01\x00', b'\x05\x01\x00' + expected + (50051).to_bytes(2, 'big')])

    def test_refusal_is_different_from_ssh_policy_denial(self):
        for reply, checked in [(2, False), (3, True), (4, True), (5, True), (6, True), (7, False), (255, False)]:
            stream = FragmentedReply(b'\x05\x00\x05' + bytes([reply]) + b'\x00\x01' + bytes(6))
            value = socks_connect(stream, '127.0.0.1', 22)
            self.assertFalse(value['ok'])
            self.assertEqual(value['checked'], checked)

    def test_invalid_and_truncated_replies_are_never_success(self):
        for data in (b'', b'\x05\xff', b'\x05\x00\x05\x00', b'\x05\x00\x04\x00\x00\x01' + bytes(6)):
            with self.subTest(data=data), self.assertRaises((OSError, ValueError)):
                socks_connect(FragmentedReply(data), '127.0.0.1', 22)


class LifecycleTests(unittest.TestCase):
    def probe(self, result=None, failure=None, log=b'', exited=False):
        child = MagicMock()
        child.poll.return_value = 255 if exited else None
        stream = MagicMock()
        stream.__enter__.return_value = stream
        def launch(*args, **kwargs):
            kwargs['stderr'].write(log)
            kwargs['stderr'].flush()
            return child
        with patch('jumper_manager.ssh_probe.subprocess.Popen', side_effect=launch) as spawn, \
             patch('jumper_manager.ssh_probe.WindowsJob') as job, \
             patch('jumper_manager.ssh_probe._close_probe') as cleanup, \
             patch('jumper_manager.ssh_probe.socket.create_connection', return_value=stream), \
             patch('jumper_manager.ssh_probe.socks_connect', return_value=result, side_effect=failure):
            value = ssh_tcp_check(['ssh', '-F', 'policy'], 'server-a', '127.0.0.1', 22)
        cleanup.assert_called_once_with(child, job.return_value)
        args = spawn.call_args.args[0]
        self.assertIn('-D', args)
        self.assertIn('ExitOnForwardFailure=yes', args)
        self.assertEqual(args[-1], 'server-a')
        self.assertFalse(any('python' in arg or 'sh -' in arg for arg in args))
        self.assertEqual(spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)
        return value

    def test_success_cleans_temporary_ssh(self):
        self.assertTrue(self.probe(result={'ok': True, 'checked': True})['ok'])

    def test_timeout_also_cleans_and_is_unknown(self):
        self.assertFalse(self.probe(failure=socket.timeout('timeout'))['checked'])

    def test_openssh_eof_failure_reason_is_preserved(self):
        value = self.probe(failure=OSError('closed'), log=b'channel 1: open failed: connect failed: Connection refused\n')
        self.assertFalse(value['ok'])
        self.assertTrue(value['checked'])

    def test_ssh_auth_failure_is_not_a_target_refusal(self):
        value = self.probe(log=b'Permission denied (publickey)', exited=True)
        self.assertFalse(value['checked'])
        self.assertIn('Permission denied', value['message'])

    def test_jump_ssh_port_refusal_is_not_a_target_check(self):
        value = self.probe(log=b'channel 0: open failed: connect failed: Connection refused', exited=True)
        self.assertFalse(value['checked'])

    def test_cleanup_stops_jump_child_after_parent_exits(self):
        child = MagicMock(pid=54321)
        child.poll.return_value = 0
        job = MagicMock()
        kill = MagicMock()
        with patch('jumper_manager.ssh_probe.os', types.SimpleNamespace(name='posix', killpg=kill)), \
             patch('jumper_manager.ssh_probe.signal', types.SimpleNamespace(SIGTERM=15, SIGKILL=9)), \
             patch('jumper_manager.ssh_probe._linux_group', return_value=({54322: {}}, True)), \
             patch('jumper_manager.ssh_probe.time.monotonic', side_effect=[0, 3]):
            _close_probe(child, job)
        self.assertEqual([call.args for call in kill.call_args_list], [(54321, 15), (54321, 9)])
        job.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
