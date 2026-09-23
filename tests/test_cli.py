"""CLI contracts against fake callbacks and an isolated real HTTP server."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from jumper_manager.cli import CommandError, process_summary, resolve_mapping, run
from jumper_manager.server import AppServer
from support import temp_directory


def sample(mapping_id="abc111", name="demo", **extra):
    return {"id": mapping_id, "name": name, "status": "stopped", "source_host": "local",
            "source_port": 45051, "target_host": "device-a", "target_port": 50051,
            "bind_address": "127.0.0.1", "target_address": "127.0.0.1", "auto_start": False,
            "pinned": False, "error": None, "health": None, "logs": [], **extra}


class FakeAPI:
    def __init__(self):
        self.base = "http://127.0.0.1:18765"
        self.calls = []
        self.mappings = [sample(), sample("abc222", "secondary")]
        self.hosts = [{"id": "local", "alias": "local", "hostname": "127.0.0.1", "route": ["local"]},
                      {"id": "device-a", "alias": "device-a", "hostname": "192.0.2.1", "user": "test",
                       "port": 22, "route": ["local", "device-a"]}]
        self.overrides = {}

    def running(self):
        return {"url": self.base}

    def request(self, url, body=None, token=None, timeout=3, method=None):
        path = urlsplit(url).path
        method = method or ("POST" if body is not None else "GET")
        self.calls.append((method, path, copy.deepcopy(body), token, timeout))
        if path == "/api/session":
            return {"token": "session-token"}
        if path == "/api/state":
            return copy.deepcopy({"hosts": self.hosts, "mappings": self.mappings})
        if method != "GET" and token != "session-token":
            raise HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"error":"bad token"}'))
        override = self.overrides.get((method, path))
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return copy.deepcopy(override)
        if path == "/api/hosts/refresh":
            return {"hosts": copy.deepcopy(self.hosts)}
        if path == "/api/hosts/probe":
            return {"ok": True, "alias": body["alias"], "message": "SSH 测试通过"}
        if path == "/api/mappings" and method == "POST":
            result = sample("new333", **body)
            self.mappings.append(result)
            return copy.deepcopy(result)
        if path == "/api/mappings/reorder":
            self.mappings = [next(item for item in self.mappings if item["id"] == mid) for mid in body["mapping_ids"]]
            return {"mappings": copy.deepcopy(self.mappings)}
        parts = path.split("/")
        mapping = next((item for item in self.mappings if item["id"] == parts[3]), None)
        if mapping is None:
            raise AssertionError(f"Unknown test route: {method} {path}")
        action = parts[4] if len(parts) == 5 else None
        if method == "PUT":
            mapping.update(body)
        elif method == "DELETE":
            self.mappings.remove(mapping)
            return {"ok": True}
        elif action == "pin":
            mapping["pinned"] = body["pinned"]
        elif action in {"start", "check"}:
            if action == "start" or mapping["status"] == "running":
                mapping.update(status="running", health={"ok": False, "tunnel_ok": True, "target_ok": False},
                               target_usage={"process_count": 0, "listening": False, "complete": True})
        elif action == "stop":
            mapping["status"] = "stopped"
        elif action == "logs":
            return {"logs": [{"time": str(i), "level": "info", "message": f"log {i}"} for i in range(4)]}
        else:
            raise AssertionError(f"Unknown test route: {method} {path}")
        return copy.deepcopy(mapping)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()

    def invoke(self, argv, *, running=None, request=None, data=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run(argv, running=running or self.api.running, request=request or self.api.request,
                       data=data or Path("unused-test-data"))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_flag_works_at_all_command_levels(self):
        for argv in (["--json", "hosts", "list"], ["hosts", "--json", "list"], ["hosts", "list", "--json"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.invoke(argv)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout)["hosts"], self.api.hosts)
                self.assertEqual(stderr, "")
        self.assertFalse(any(call[1] == "/api/session" for call in self.api.calls))

    def test_invalid_arguments_are_one_json_error_without_traceback(self):
        for argv in (["mappings", "unknown", "--json"], ["mappings", "add", "--json"],
                     ["logs", "--tail", "-1", "--json"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.invoke(argv)
                self.assertEqual(code, 2)
                self.assertIsInstance(json.loads(stdout)["error"], str)
                self.assertEqual(stderr, "")
        self.assertEqual(self.api.calls, [])

    def test_help_does_not_contact_service(self):
        code, stdout, stderr = self.invoke(["mappings", "add", "--help"], running=lambda: self.fail("service queried"))
        self.assertEqual(code, 0)
        self.assertIn("--source-host", stdout)
        self.assertIn("--no-auto-start", stdout)
        self.assertEqual(stderr, "")

    def test_stopped_service_returns_actionable_nonzero_error(self):
        code, stdout, stderr = self.invoke(["mappings", "list"], running=lambda: None)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("app.py start", stderr)
        self.assertEqual(self.api.calls, [])

    def test_remote_or_credential_urls_cannot_receive_session_token(self):
        for url in ("https://127.0.0.1:80", "http://192.0.2.1:8765", "http://127.0.0.1.evil:8765",
                    "http://user:secret@127.0.0.1:8765", "http://127.0.0.1:8765/other", "http://127.0.0.1:0"):
            with self.subTest(url=url):
                code, _, stderr = self.invoke(["hosts", "sync"], running=lambda: {"url": url})
                self.assertEqual(code, 1)
                self.assertIn("本机", stderr)
        self.assertEqual(self.api.calls, [])

    def test_host_sync_and_probe_use_api_and_token(self):
        for argv, endpoint, body in ((["hosts", "sync"], "/api/hosts/refresh", {}),
                                     (["hosts", "test", "device-a"], "/api/hosts/probe", {"alias": "device-a"})):
            with self.subTest(argv=argv):
                code, _, _ = self.invoke(argv)
                self.assertEqual(code, 0)
                self.assertEqual(self.api.calls[-1][:4], ("POST", endpoint, body, "session-token"))
                self.assertGreaterEqual(self.api.calls[-1][4], 25)

    def test_failed_host_test_retains_json_result_and_exit_failure(self):
        result = {"ok": False, "alias": "device-a", "message": "SSH 登录失败"}
        self.api.overrides[("POST", "/api/hosts/probe")] = result
        code, stdout, _ = self.invoke(["hosts", "test", "device-a", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout), result)

    def test_add_creates_stopped_mapping_and_sends_typed_options(self):
        code, stdout, _ = self.invoke(["mappings", "add", "--name", "新映射", "--source-host", "local",
                                      "--source-port", "50052", "--target-host", "device-a",
                                      "--target-port", "50051", "--auto-start", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "stopped")
        self.assertEqual(self.api.calls[-1][:4], ("POST", "/api/mappings", {
            "name": "新映射", "source_host": "local", "source_port": 50052,
            "target_host": "device-a", "target_port": 50051, "auto_start": True}, "session-token"))

    def test_edit_sends_only_explicit_fields_and_preserves_false(self):
        self.api.mappings[0].update(auto_start=True, target_address="service.internal")
        code, _, _ = self.invoke(["mappings", "edit", "demo", "--no-auto-start"])
        self.assertEqual(code, 0)
        self.assertEqual(self.api.calls[-1][:4], ("PUT", "/api/mappings/abc111", {"auto_start": False}, "session-token"))
        self.assertEqual(self.api.mappings[0]["target_address"], "service.internal")

    def test_empty_edit_and_invalid_ports_do_not_mutate(self):
        code, _, stderr = self.invoke(["mappings", "edit", "demo"])
        self.assertEqual(code, 2)
        self.assertIn("修改的字段", stderr)
        for value in ("0", "65536", "port"):
            code, _, _ = self.invoke(["mappings", "edit", "demo", "--target-port", value])
            self.assertEqual(code, 2)
        self.assertFalse(any(call[0] != "GET" for call in self.api.calls))

    def test_unique_name_prefix_and_exact_id_resolve(self):
        for reference in ("demo", "abc1", "abc111"):
            code, stdout, _ = self.invoke(["mappings", "show", reference, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout)["id"], "abc111")

    def test_ambiguous_prefix_name_or_cross_type_collision_is_rejected(self):
        for mappings, reference in (([sample(), sample("abc222", "other")], "abc"),
                                     ([sample(name="same"), sample("def222", "same")], "same"),
                                     ([sample(), sample("def222", "abc")], "abc")):
            with self.subTest(reference=reference):
                self.api.mappings = mappings
                code, stdout, _ = self.invoke(["mappings", "delete", reference, "--json"])
                self.assertEqual(code, 2)
                self.assertIn("不唯一", json.loads(stdout)["error"])
        self.assertFalse(any(call[0] != "GET" for call in self.api.calls))

    def test_exact_id_wins_over_name_and_missing_reference_errors(self):
        items = [sample(), sample("other", "abc111")]
        self.assertEqual(resolve_mapping(items, "abc111")["id"], "abc111")
        with self.assertRaises(CommandError):
            resolve_mapping(items, "missing")

    def test_delete_pin_and_unpin_use_the_actual_api_methods(self):
        for command, method, path, body in (("pin", "POST", "/api/mappings/abc111/pin", {"pinned": True}),
                                             ("unpin", "POST", "/api/mappings/abc111/pin", {"pinned": False}),
                                             ("delete", "DELETE", "/api/mappings/abc111", {})):
            code, _, _ = self.invoke(["mappings", command, "demo"])
            self.assertEqual(code, 0)
            self.assertEqual(self.api.calls[-1][:4], (method, path, body, "session-token"))

    def test_start_and_check_missing_target_succeed_without_fake_process_counts(self):
        self.api.mappings[0]["usage"] = {"active_connections": 99, "in_use": True}
        for action in ("start", "check"):
            code, stdout, stderr = self.invoke(["mappings", action, "demo"])
            self.assertEqual(code, 0)
            self.assertIn("已启动", stdout)
            self.assertIn("没有进程在用", stdout)
            self.assertNotIn("99", stdout)
            self.assertEqual(stderr, "")
        code, stdout, _ = self.invoke(["mappings", "stop", "demo"])
        self.assertEqual(code, 0)
        self.assertIn("已停止", stdout)

    def test_failed_start_and_check_of_stopped_mapping_return_nonzero(self):
        failed = sample(status="error", error="SSH 登录失败", failure_stage="start")
        self.api.overrides[("POST", "/api/mappings/abc111/start")] = failed
        code, stdout, _ = self.invoke(["mappings", "start", "demo", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout), failed)
        code, stdout, _ = self.invoke(["mappings", "check", "demo"])
        self.assertEqual(code, 1)
        self.assertIn("未执行检查", stdout)

    def test_process_count_requires_complete_real_listener_data(self):
        for value, expected in (({"process_count": 2, "listening": True, "complete": True}, "有 2 个进程在用"),
                                ({"process_count": 2, "listening": True, "complete": False}, "检测到服务，进程数未知"),
                                ({"process_count": True, "listening": True, "complete": True}, "检测到服务，进程数未知"),
                                ({"process_count": None, "listening": None, "checked_at": "now"}, "进程状态未知"),
                                ({}, "尚未检查进程")):
            self.assertEqual(process_summary({"target_usage": value, "usage": {"active_connections": 50}}), expected)

    def test_reorder_requires_complete_unique_ids_and_preserves_pin_groups(self):
        self.api.mappings[0]["pinned"] = True
        for refs in (["demo"], ["demo", "abc111"], ["secondary", "demo"]):
            code, _, _ = self.invoke(["mappings", "reorder", *refs])
            self.assertEqual(code, 2)
        self.assertFalse(any(call[0] != "GET" for call in self.api.calls))
        self.api.mappings[0]["pinned"] = False
        code, stdout, _ = self.invoke(["mappings", "reorder", "secondary", "abc1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual([item["id"] for item in json.loads(stdout)["mappings"]], ["abc222", "abc111"])
        self.assertEqual(self.api.calls[-1][2], {"mapping_ids": ["abc222", "abc111"]})

    def test_http_error_body_is_preserved_in_json_and_no_success_is_printed(self):
        error = HTTPError(self.api.base, 409, "Conflict", {}, io.BytesIO(json.dumps({"error": "请先停止映射"}).encode()))
        self.api.overrides[("DELETE", "/api/mappings/abc111")] = error
        code, stdout, stderr = self.invoke(["mappings", "delete", "demo", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout), {"error": "请先停止映射"})
        self.assertEqual(stderr, "")

    def test_mapping_logs_use_api_and_apply_tail(self):
        for tail, expected in ((2, ["log 2", "log 3"]), (0, [])):
            code, stdout, _ = self.invoke(["logs", "demo", "--tail", str(tail), "--json"])
            self.assertEqual(code, 0)
            self.assertEqual([item["message"] for item in json.loads(stdout)["logs"]], expected)
            self.assertEqual(self.api.calls[-1][1], "/api/mappings/abc111/logs")

    def test_service_log_tail_works_offline_and_missing_log_is_empty(self):
        with temp_directory() as directory:
            root = Path(directory)
            (root / "server.log").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
            code, stdout, _ = self.invoke(["logs", "--server", "-n", "2", "--json"], data=root,
                                           running=lambda: self.fail("offline logs queried service"))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout)["lines"], ["第二行", "第三行"])
            code, stdout, _ = self.invoke(["logs", "--json"], data=root / "missing")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout)["lines"], [])
        self.assertEqual(self.api.calls, [])

    def test_logs_cannot_mix_server_and_mapping(self):
        code, _, _ = self.invoke(["logs", "demo", "--server"])
        self.assertEqual(code, 2)
        self.assertEqual(self.api.calls, [])


class HTTPManager:
    def __init__(self):
        self.mapping = sample("http123", "http-demo")
        self.calls = []

    def state(self):
        return {"hosts": [], "mappings": [copy.deepcopy(self.mapping)]}

    def update(self, mapping_id, body):
        self.calls.append(("update", mapping_id, body))
        self.mapping.update(body)
        return copy.deepcopy(self.mapping)

    def delete(self, mapping_id):
        self.calls.append(("delete", mapping_id))


class CLIHTTPTests(unittest.TestCase):
    def setUp(self):
        self.manager = HTTPManager()
        self.server = AppServer(("127.0.0.1", 0), self.manager, Path(__file__).parent,
                                {"app": "JumperManager", "instance_id": "cli-test"})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = build_opener(ProxyHandler({}))
        self.seen_tokens = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, url, body=None, token=None, timeout=3, method=None):
        headers = {"Origin": self.base, "Content-Type": "application/json"}
        if token:
            headers["X-Jumper-Token"] = token
            self.seen_tokens.append(token)
        req = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers, method=method)
        with self.opener.open(req, timeout=timeout) as response:
            return json.load(response)

    def invoke(self, args, request=None):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = run(args, running=lambda: {"url": self.base}, request=request or self.request,
                         data=Path("unused-test-data"))
        return result, output.getvalue(), errors.getvalue()

    def test_real_http_put_delete_preserve_origin_and_require_fetched_token(self):
        code, output, _ = self.invoke(["mappings", "edit", "http-demo", "--name", "changed", "--json"])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.manager.calls[-1], ("update", "http123", {"name": "changed"}))
        code, output, _ = self.invoke(["mappings", "delete", "http123", "--json"])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.manager.calls[-1], ("delete", "http123"))
        self.assertEqual(self.seen_tokens, [self.server.token, self.server.token])

    def test_actual_api_rejects_wrong_token_without_changing_manager(self):
        def bad_session(url, **kwargs):
            return {"token": "invalid"} if url.endswith("/api/session") else self.request(url, **kwargs)
        code, output, _ = self.invoke(["mappings", "delete", "http123", "--json"], request=bad_session)
        self.assertEqual(code, 1)
        self.assertIn("会话", json.loads(output)["error"])
        self.assertEqual(self.manager.calls, [])


if __name__ == "__main__":
    unittest.main()
