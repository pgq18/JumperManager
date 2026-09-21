"""HTTP contract checks with a fake manager; never invoke SSH or real mappings."""

from __future__ import annotations

import copy
import http.client
import json
import threading
import unittest
from pathlib import Path

from jumper_manager.server import AppServer, MAX_BODY
from support import temp_directory


class FakeManager:
    def __init__(self):
        self.calls = []
        self.failures = {}
        self.mapping = {"id": "example", "name": "demo", "status": "stopped", "logs": [{"message": "ready"}]}

    def _record(self, method, *args):
        self.calls.append((method, args))
        if method in self.failures:
            raise self.failures[method]

    def state(self):
        self._record("state")
        return {"version": "1.0", "hosts": [{"id": "local", "label": "本机"}], "mappings": [copy.deepcopy(self.mapping)]}

    def refresh_hosts(self):
        self._record("refresh_hosts")
        return [{"id": "remote"}]

    def probe_host(self, alias):
        self._record("probe_host", alias)
        return {"ok": True, "alias": alias}

    def preview(self, payload):
        self._record("preview", payload)
        return {"route": [], "description": "preview", "warnings": [], "steps": []}

    def create(self, payload):
        self._record("create", payload)
        return {**self.mapping, **payload}

    def update(self, mapping_id, payload):
        self._record("update", mapping_id, payload)
        return {**self.mapping, **payload, "id": mapping_id}

    def delete(self, mapping_id):
        self._record("delete", mapping_id)

    def start(self, mapping_id):
        self._record("start", mapping_id)
        return {**self.mapping, "id": mapping_id, "status": "running"}

    def stop(self, mapping_id):
        self._record("stop", mapping_id)
        return {**self.mapping, "id": mapping_id, "status": "stopped"}

    def check(self, mapping_id):
        self._record("check", mapping_id)
        return {**self.mapping, "id": mapping_id, "health": {"ok": True}}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(temp_directory()))
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>测试</title>", encoding="utf-8")
        (self.web / "app.js").write_text("console.log('test');", encoding="utf-8")
        (self.web / "private.json").write_text('{"secret":true}', encoding="utf-8")
        (self.root / "outside.html").write_text("private outside file", encoding="utf-8")
        self.manager = FakeManager()
        self.identity = {"app": "JumperManager", "instance_id": "test-instance"}
        self.server = AppServer(("127.0.0.1", 0), self.manager, self.web, self.identity)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, *, headers=None, token=True, raw=False):
        request_headers = {}
        if token:
            request_headers["X-Jumper-Token"] = self.server.token
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            if not raw:
                body = json.dumps(body).encode("utf-8")
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            response_headers = dict(response.getheaders())
            parsed = json.loads(content) if content and response_headers.get("Content-Type", "").startswith("application/json") else content
            return response.status, response_headers, parsed
        finally:
            connection.close()

    def test_read_routes_and_session_token(self):
        status, _, ping = self.request("GET", "/api/ping", token=False)
        self.assertEqual((status, ping), (200, self.identity))
        status, _, session = self.request("GET", "/api/session", token=False)
        self.assertEqual((status, session["token"]), (200, self.server.token))
        status, _, state = self.request("GET", "/api/state?refresh=1", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(state["hosts"][0]["label"], "本机")
        self.assertEqual(state["server"]["url"], f"http://127.0.0.1:{self.port}")
        status, _, logs = self.request("GET", "/api/mappings/example/logs")
        self.assertEqual((status, logs), (200, {"logs": self.manager.mapping["logs"]}))

    def test_mutation_routes_pass_correct_arguments(self):
        cases = [
            ("POST", "/api/hosts/refresh", {}, "refresh_hosts", (), 200),
            ("POST", "/api/hosts/probe", {"alias": "remote"}, "probe_host", ("remote",), 200),
            ("POST", "/api/preview", {"source_port": 1234}, "preview", ({"source_port": 1234},), 200),
            ("POST", "/api/mappings", {"name": "new"}, "create", ({"name": "new"},), 201),
            ("PUT", "/api/mappings/example", {"name": "edited"}, "update", ("example", {"name": "edited"}), 200),
            ("POST", "/api/mappings/example/start", {}, "start", ("example",), 200),
            ("POST", "/api/mappings/example/stop", {}, "stop", ("example",), 200),
            ("POST", "/api/mappings/example/check", {}, "check", ("example",), 200),
            ("DELETE", "/api/mappings/example", None, "delete", ("example",), 200),
        ]
        for method, path, body, called_method, args, expected_status in cases:
            with self.subTest(method=method, path=path):
                status, _, data = self.request(method, path, body)
                self.assertEqual(status, expected_status, data)
                self.assertEqual(self.manager.calls[-1], (called_method, args))

    def test_host_origin_and_fetch_site_restrictions(self):
        cases = [
            {"Host": "evil.example"},
            {"Host": f"127.0.0.1.evil.example:{self.port}"},
            {"Host": "127.0.0.1:1"},
            {"Host": ""},
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Origin": f"http://127.0.0.1:{self.port + 1}"},
            {"Sec-Fetch-Site": "cross-site"},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                status, _, _ = self.request("GET", "/api/session", headers=headers)
                self.assertEqual(status, 403)
                status, _, _ = self.request("POST", "/api/mappings", {}, headers=headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.manager.calls, [])

    def test_localhost_and_matching_origin_are_allowed(self):
        for hostname in ("localhost", "127.0.0.1"):
            with self.subTest(hostname=hostname):
                status, _, _ = self.request("POST", "/api/preview", {}, headers={"Host": f"{hostname}:{self.port}", "Origin": f"http://{hostname}:{self.port}", "Sec-Fetch-Site": "same-origin"})
                self.assertEqual(status, 200)

    def test_mutations_require_token(self):
        for method, path in [("POST", "/api/mappings"), ("PUT", "/api/mappings/example"), ("DELETE", "/api/mappings/example"), ("POST", "/api/shutdown")]:
            for token in ("", "incorrect"):
                with self.subTest(method=method, path=path, token=token):
                    status, _, _ = self.request(method, path, {}, token=False, headers={"X-Jumper-Token": token})
                    self.assertEqual(status, 403)
        self.assertEqual(self.manager.calls, [])
        self.assertFalse(self.server.shutting_down)

    def test_non_ascii_token_is_rejected_cleanly(self):
        status, _, data = self.request("POST", "/api/mappings", {}, headers={"X-Jumper-Token": "\u00e9"})
        self.assertEqual(status, 403, data)
        self.assertEqual(self.manager.calls, [])

    def test_bad_bodies_are_rejected_without_manager_calls(self):
        cases = [
            (b"{broken", {}),
            (b"\xff", {}),
            (b"[]", {}),
            (b"null", {}),
            (b"false", {}),
            (b"{}", {"Content-Type": "text/plain"}),
            (b"", {"Content-Length": "bad"}),
            (b"", {"Content-Length": "-1"}),
            (b"", {"Content-Length": str(MAX_BODY + 1)}),
        ]
        for body, headers in cases:
            with self.subTest(body=body, headers=headers):
                status, _, data = self.request("POST", "/api/mappings", body, headers=headers, raw=True)
                self.assertEqual(status, 400, data)
        self.assertEqual(self.manager.calls, [])

    def test_empty_body_is_an_empty_object(self):
        status, _, _ = self.request("POST", "/api/preview")
        self.assertEqual(status, 200)
        self.assertEqual(self.manager.calls[-1], ("preview", ({},)))

    def test_manager_errors_have_documented_statuses(self):
        for error, expected in [(ValueError("invalid"), 400), (KeyError("missing"), 404), (RuntimeError("busy"), 409), (OSError("cannot bind"), 409), (TypeError("internal detail"), 500)]:
            with self.subTest(error=error):
                self.manager.failures["create"] = error
                status, _, data = self.request("POST", "/api/mappings", {})
                self.assertEqual(status, expected)
                self.assertIsInstance(data["error"], str)
                if expected == 500:
                    self.assertNotIn("internal detail", data["error"])

    def test_unknown_routes_and_methods_do_not_call_manager(self):
        for method, path in [("GET", "/api/absent"), ("POST", "/api/absent"), ("POST", "/api/mappings/example/wrong"), ("POST", "/api/mappings/example/start/extra"), ("PUT", "/api/hosts/refresh"), ("DELETE", "/api/shutdown"), ("POST", "/api/mappings/example"), ("PUT", "/api/mappings/example/start")]:
            with self.subTest(method=method, path=path):
                status, _, data = self.request(method, path, {} if method != "GET" else None)
                self.assertEqual(status, 404, data)
        self.assertEqual(self.manager.calls, [])

    def test_missing_mapping_logs_return_404(self):
        status, _, _ = self.request("GET", "/api/mappings/missing/logs")
        self.assertEqual(status, 404)

    def test_extra_logs_path_segments_are_rejected(self):
        status, _, _ = self.request("GET", "/api/mappings/example/extra/logs")
        self.assertEqual(status, 404)

    def test_static_files_and_response_security_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("测试".encode("utf-8"), body)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, body = self.request("GET", "/app.js?version=1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")

    def test_head_returns_headers_without_body(self):
        for path in ("/", "/app.js", "/api/session", "/api/state", "/missing"):
            with self.subTest(path=path):
                status, headers, body = self.request("HEAD", path)
                self.assertEqual(status, 404 if path == "/missing" else 200)
                self.assertGreater(int(headers["Content-Length"]), 0)
                self.assertEqual(body, b"")

    def test_static_traversal_and_disallowed_extensions(self):
        for path in ("/../outside.html", "/%2e%2e/outside.html", "/%2e%2e%5coutside.html", "/private.json", "/missing.js", "/%2e%2e/%2e%2e/outside.html"):
            with self.subTest(path=path):
                status, _, data = self.request("GET", path)
                self.assertEqual(status, 404, data)
                self.assertNotIn("private outside file", str(data))

    def test_mutations_rejected_during_shutdown(self):
        self.server.shutting_down = True
        status, _, _ = self.request("POST", "/api/mappings", {})
        self.assertEqual(status, 503)
        self.assertEqual(self.manager.calls, [])
        status, _, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["server"]["shutting_down"])

    def test_state_remains_responsive_while_start_is_waiting(self):
        entered = threading.Event()
        release = threading.Event()
        result = []

        def delayed_start(mapping_id):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test did not release the start operation")
            return {**self.manager.mapping, "status": "running"}

        def start_request():
            try:
                result.append(self.request("POST", "/api/mappings/example/start", {}))
            except Exception as error:
                result.append(error)

        self.manager.start = delayed_start
        worker = threading.Thread(target=start_request, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1), "start request did not reach the manager")
            status, _, data = self.request("GET", "/api/state")
            self.assertEqual(status, 200, data)
            self.assertFalse(release.is_set())
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], tuple)
        self.assertEqual(result[0][0], 200)

    def test_shutdown_returns_success_and_stops_server_loop(self):
        status, _, data = self.request("POST", "/api/shutdown", {})
        self.assertEqual((status, data), (200, {"ok": True}))
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.server.shutting_down)


if __name__ == "__main__":
    unittest.main()
