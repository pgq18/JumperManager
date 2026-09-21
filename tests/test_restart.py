"""Restart regressions: TIME_WAIT is reusable, active listeners stay protected.

These tests execute the exact Python payload generated for a remote endpoint.
They never connect over SSH or start/stop any saved mapping. Linux TIME_WAIT is
modeled explicitly; real socket tests use only ephemeral ports on this machine.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import errno
import io
import os
import socket
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from jumper_manager.engine import Manager


class ReuseAwareSocket:
    """Model Linux bind policy plus an independent listen failure."""

    def __init__(self, scenario="time_wait"):
        self.scenario = scenario
        self.events = []
        self.reuse = False
        self.closed = False

    def setsockopt(self, level, option, value):
        self.events.append(("setsockopt", level, option, value))
        if level == socket.SOL_SOCKET and option == socket.SO_REUSEADDR:
            self.reuse = bool(value)

    def bind(self, endpoint):
        self.events.append(("bind", endpoint))
        if self.scenario == "occupied_bind" or (self.scenario == "time_wait" and not self.reuse):
            raise OSError(errno.EADDRINUSE, "Address already in use")

    def listen(self, backlog=1):
        self.events.append(("listen", backlog))
        if self.scenario == "occupied_listen":
            raise OSError(errno.EADDRINUSE, "Address already in use")

    def close(self):
        self.events.append(("close",))
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def fake_socket_module(instance):
    module = types.ModuleType("socket")
    for name in ("AF_INET", "AF_INET6", "SOCK_STREAM", "SOL_SOCKET", "SO_REUSEADDR", "SO_REUSEPORT", "SO_EXCLUSIVEADDRUSE"):
        if hasattr(socket, name):
            setattr(module, name, getattr(socket, name))

    def create(*args, **kwargs):
        instance.events.append(("socket", args, kwargs))
        return instance

    def connect(*args, **kwargs):
        instance.events.append(("connect", args, kwargs))
        return instance

    module.socket = create
    module.create_connection = connect
    return module


class RestartProbeTests(unittest.TestCase):
    def probe(self, *, operation="available", host_address="127.0.0.1", host_port=55289, fake=None):
        # Construct no Manager runtime: _endpoint_check only needs these two
        # substituted methods, avoiding config changes and monitor threads.
        manager = Manager.__new__(Manager)

        def run(args, *, input=None, timeout=None):
            self.assertEqual(args[-1], "python3 -")
            self.assertIsInstance(input, str)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                if fake is None:
                    exec(compile(input, "<generated-remote-probe>", "exec"), {})
                else:
                    remote_os = types.ModuleType("os")
                    remote_os.__dict__.update(vars(os))
                    remote_os.name = "posix"
                    with patch.dict(sys.modules, {"socket": fake_socket_module(fake), "os": remote_os}):
                        exec(compile(input, "<generated-remote-probe>", "exec"), {})
            return subprocess.CompletedProcess(args, 0, stdout.getvalue(), "")

        with patch.object(manager, "_ssh_args", return_value=["ssh", "-T"]), patch.object(manager, "_run", side_effect=run):
            return manager._endpoint_check("test-device", operation, host_address, host_port)

    def test_time_wait_reuse_can_bind_and_listen_then_releases_socket(self):
        probe_socket = ReuseAwareSocket("time_wait")
        result = self.probe(fake=probe_socket)
        self.assertTrue(result["ok"], result)
        self.assertTrue(probe_socket.reuse, "POSIX restart must allow reusing TIME_WAIT")
        event_names = [event[0] for event in probe_socket.events]
        self.assertLess(event_names.index("setsockopt"), event_names.index("bind"))
        self.assertLess(event_names.index("bind"), event_names.index("listen"))
        self.assertTrue(probe_socket.closed)
        if hasattr(socket, "SO_REUSEPORT"):
            self.assertFalse(any(event[:3] == ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEPORT) for event in probe_socket.events),
                             "An availability check must not join another listener using SO_REUSEPORT")

    def test_active_listener_still_rejected_and_errno_preserved(self):
        probe_socket = ReuseAwareSocket("occupied_bind")
        result = self.probe(fake=probe_socket)
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("errno"), errno.EADDRINUSE)
        self.assertTrue(probe_socket.closed, "Failed bind must not leak the probe socket")
        self.assertNotIn("listen", [event[0] for event in probe_socket.events])

    def test_bind_success_without_listen_permission_is_not_available(self):
        probe_socket = ReuseAwareSocket("occupied_listen")
        result = self.probe(fake=probe_socket)
        self.assertFalse(result["ok"], "bind alone is not proof that SSH can create a listener")
        self.assertEqual(result.get("errno"), errno.EADDRINUSE)
        self.assertIn("listen", [event[0] for event in probe_socket.events])
        self.assertTrue(probe_socket.closed)

    def test_target_connection_check_does_not_turn_into_listener(self):
        probe_socket = ReuseAwareSocket("target")
        result = self.probe(operation="connect", fake=probe_socket)
        self.assertTrue(result["ok"], result)
        event_names = [event[0] for event in probe_socket.events]
        self.assertIn("connect", event_names)
        self.assertNotIn("bind", event_names)
        self.assertNotIn("listen", event_names)
        self.assertTrue(probe_socket.closed)

    def test_actual_listening_socket_cannot_be_taken_over(self):
        with socket.socket() as listener:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            endpoint = listener.getsockname()
            result = self.probe(host_port=endpoint[1])
            self.assertFalse(result["ok"], result)
            self.assertIsInstance(result.get("errno"), int)
            # The competing probe did not close or replace our listener.
            self.assertEqual(listener.getsockname(), endpoint)
            self.assertEqual(listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)

    def test_repeated_successful_probes_leave_port_free(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            target_port = reservation.getsockname()[1]
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                result = self.probe(host_port=target_port)
                self.assertTrue(result["ok"], result)
        with socket.socket() as final_listener:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                final_listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            final_listener.bind(("127.0.0.1", target_port))
            final_listener.listen(1)


if __name__ == "__main__":
    unittest.main()
