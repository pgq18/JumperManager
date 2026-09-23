"""CLI client for the running local service; never owns mappings or SSH processes."""

from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit


class CommandError(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CommandError(message)


def _port(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须是 1–65535 的整数") from None
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 1–65535 的整数")
    return number


def _tail_size(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日志行数必须是 0–10000 的整数") from None
    if not 0 <= number <= 10000:
        raise argparse.ArgumentTypeError("日志行数必须是 0–10000 的整数")
    return number


def _mapping_flags(parser, *, required):
    parser.add_argument("--name", default=argparse.SUPPRESS, help="映射名称")
    for name in ("source", "target"):
        label = "来源" if name == "source" else "目标"
        parser.add_argument(f"--{name}-host", required=required, default=argparse.SUPPRESS,
                            help=f"{label}设备的 SSH 别名，本机为 local")
        parser.add_argument(f"--{name}-port", required=required, type=_port,
                            default=argparse.SUPPRESS, help=f"{label}端口")
    parser.add_argument("--bind-address", default=argparse.SUPPRESS, help="来源监听地址")
    parser.add_argument("--target-address", default=argparse.SUPPRESS, help="目标设备上的服务地址")
    startup = parser.add_mutually_exclusive_group()
    startup.add_argument("--auto-start", dest="auto_start", action="store_true",
                         default=argparse.SUPPRESS, help="管理器启动时自动启动此映射")
    startup.add_argument("--no-auto-start", dest="auto_start", action="store_false",
                         default=argparse.SUPPRESS, help="取消此映射的自动启动")


def _parser():
    parser = Parser(prog="app.py", description="管理已启动的 JumperManager 本机服务",
                    epilog="所有命令都支持 --json，位置不限。REF 支持完整 ID、唯一 ID 前缀或唯一名称。")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本读取")
    commands = parser.add_subparsers(dest="command", required=True)
    hosts = commands.add_parser("hosts", help="设备列表、SSH 测试与配置同步")
    host_actions = hosts.add_subparsers(dest="action", required=True)
    host_actions.add_parser("list", help="列出 SSH 配置中的设备")
    probe = host_actions.add_parser("test", help="测试设备的 SSH 登录")
    probe.add_argument("alias", help="SSH 设备别名，例如 device-a")
    host_actions.add_parser("sync", help="重新读取 SSH 配置")

    mappings = commands.add_parser("mappings", help="管理端口映射")
    actions = mappings.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="列出映射及目标进程快照")
    add = actions.add_parser("add", help="保存新映射，默认保持停止")
    _mapping_flags(add, required=True)
    edit = actions.add_parser("edit", help="只修改指定字段；需先停止映射")
    edit.add_argument("ref", metavar="REF")
    _mapping_flags(edit, required=False)
    for action, help_text in (("show", "查看映射详情"), ("delete", "删除已停止的映射"),
                              ("start", "启动映射"), ("stop", "停止映射"),
                              ("check", "检查映射并更新目标进程数"),
                              ("pin", "置顶映射"), ("unpin", "取消置顶")):
        child = actions.add_parser(action, help=help_text)
        child.add_argument("ref", metavar="REF")
    reorder = actions.add_parser("reorder", help="按给定顺序排列全部映射，置顶组必须在前")
    reorder.add_argument("refs", nargs="+", metavar="REF")

    logs = commands.add_parser("logs", help="读取服务日志或指定映射日志")
    logs.add_argument("ref", nargs="?", metavar="REF", help="映射 ID 或名称；省略则读取服务日志")
    logs.add_argument("--server", action="store_true", help="读取本机服务日志（服务停止时也可用）")
    logs.add_argument("--tail", "-n", type=_tail_size, default=50, help="最后 N 条日志，默认 50")
    return parser


def _json_args(argv):
    """Accept --json at every command depth, preserving literals after --."""
    result, enabled, literal = [], False, False
    for item in argv:
        if item == "--":
            literal = True
        if item == "--json" and not literal:
            enabled = True
        else:
            result.append(item)
    return result, enabled


def resolve_mapping(mappings, reference):
    exact = [item for item in mappings if item.get("id") == reference]
    if len(exact) == 1:
        return exact[0]
    matches = [item for item in mappings
               if str(item.get("id", "")).startswith(reference) or item.get("name") == reference]
    if not matches:
        raise CommandError(f"没有找到映射“{reference}”；请运行 app.py mappings list。")
    if len(matches) != 1:
        choices = "、".join(f"{item.get('id')}（{item.get('name', '未命名')}）" for item in matches)
        raise CommandError(f"映射“{reference}”不唯一：{choices}。请使用完整 ID。")
    return matches[0]


class Client:
    def __init__(self, running, request):
        active = running()
        if not isinstance(active, dict) or not active.get("url"):
            raise CommandError("JumperManager 服务未启动；请先运行 app.py start。", 1)
        parsed = urlsplit(active["url"])
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or parsed.port is None or parsed.port < 1):
            raise CommandError("服务地址不是有效的本机 HTTP 地址；请重新运行 app.py start。", 1)
        self.base = active["url"].rstrip("/")
        self.request = request
        self.token = None

    def call(self, path, body=None, *, method=None):
        writing = body is not None or method in {"PUT", "DELETE", "POST"}
        if writing and self.token is None:
            session = self.request(self.base + "/api/session", timeout=10)
            self.token = session.get("token") if isinstance(session, dict) else None
            if not isinstance(self.token, str) or not self.token or not self.token.isascii():
                raise CommandError("服务未返回有效的会话令牌，请重试。", 1)
        kwargs = {"timeout": 300 if writing else 10}
        if writing:
            kwargs.update(body={} if body is None else body, token=self.token)
        # The existing callback infers GET/POST from body. PUT/DELETE use the
        # optional method argument added by the application entry point.
        if method and method not in {"GET", "POST"}:
            kwargs["method"] = method
        response = self.request(self.base + path, **kwargs)
        if not isinstance(response, dict):
            raise CommandError("服务返回了无效的 JSON 对象。", 1)
        return response

    def collection(self, key):
        response = self.call("/api/state")
        items = response.get(key)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise CommandError(f"服务未返回有效的 {key} 列表。", 1)
        if key == "mappings" and any(not isinstance(item.get("id"), str) or not item["id"] for item in items):
            raise CommandError("服务返回的映射缺少有效 ID。", 1)
        return items


def process_summary(mapping):
    usage = mapping.get("target_usage") or {}
    count = usage.get("process_count")
    complete = usage.get("complete") is True
    if complete and type(count) is int and count == 0 and usage.get("listening") is False:
        return "没有进程在用"
    if complete and type(count) is int and count > 0 and usage.get("listening") is True:
        return f"有 {count} 个进程在用"
    if usage.get("listening") is True:
        return "检测到服务，进程数未知"
    return "进程状态未知" if usage.get("checked_at") else "尚未检查进程"


def mapping_status(mapping):
    status = mapping.get("status")
    if status in {"stopped", "starting", "stopping"}:
        return {"stopped": "已停止", "starting": "启动中", "stopping": "停止中"}[status]
    if status == "error" and mapping.get("failure_stage") == "start":
        return "启动失败"
    health = mapping.get("health") or {}
    if status == "error" or mapping.get("error") or mapping.get("config_changed") or health.get("tunnel_ok") is False:
        return "异常"
    if status == "running" or (status == "degraded" and health.get("tunnel_ok") is True):
        return "已启动"
    return "未确认"


def _mapping_path(mapping):
    return "/api/mappings/" + quote(mapping["id"], safe="")


def _payload(args):
    fields = ("name", "source_host", "source_port", "target_host", "target_port",
              "bind_address", "target_address", "auto_start")
    return {field: getattr(args, field) for field in fields if hasattr(args, field)}


def _execute(args, *, running, request, data):
    if args.command == "logs" and (args.server or args.ref is None):
        if args.server and args.ref is not None:
            raise CommandError("logs REF 和 --server 不能同时使用。")
        path = data / "server.log"
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                lines = [line.rstrip("\r\n") for line in deque(handle, maxlen=args.tail)]
        except FileNotFoundError:
            lines = []
        return {"path": str(path), "lines": lines}, "server_logs", 0

    client = Client(running, request)
    if args.command == "hosts":
        if args.action == "list":
            return {"hosts": client.collection("hosts")}, "hosts", 0
        if args.action == "sync":
            return client.call("/api/hosts/refresh", {}), "hosts", 0
        result = client.call("/api/hosts/probe", {"alias": args.alias})
        return result, "probe", 0 if result.get("ok") is True else 1
    if args.command == "logs":
        mapping = resolve_mapping(client.collection("mappings"), args.ref)
        result = client.call(_mapping_path(mapping) + "/logs")
        logs = result.get("logs")
        if not isinstance(logs, list):
            raise CommandError("服务未返回有效的映射日志。", 1)
        return {"mapping_id": mapping["id"], "logs": logs[-args.tail:] if args.tail else []}, "mapping_logs", 0
    if args.action == "add":
        return client.call("/api/mappings", _payload(args)), "mapping", 0
    if args.action == "edit" and not _payload(args):
        raise CommandError("没有指定要修改的字段；例如 --name 新名称 或 --no-auto-start。")
    mappings = client.collection("mappings")
    if args.action == "list":
        return {"mappings": mappings}, "mappings", 0
    if args.action == "reorder":
        ordered = [resolve_mapping(mappings, reference) for reference in args.refs]
        ids = [item["id"] for item in ordered]
        if len(ids) != len(set(ids)) or set(ids) != {item["id"] for item in mappings}:
            raise CommandError("排序必须包含全部映射，且每条映射只能出现一次。")
        found_unpinned = False
        for item in ordered:
            if item.get("pinned") is True and found_unpinned:
                raise CommandError("置顶映射必须排在普通映射之前；请先 pin 或 unpin 再排序。")
            found_unpinned |= item.get("pinned") is not True
        return client.call("/api/mappings/reorder", {"mapping_ids": ids}), "mappings", 0
    mapping = resolve_mapping(mappings, args.ref)
    path = _mapping_path(mapping)
    if args.action == "show":
        return mapping, "mapping", 0
    if args.action == "edit":
        return client.call(path, _payload(args), method="PUT"), "mapping", 0
    if args.action == "delete":
        return client.call(path, {}, method="DELETE"), "deleted", 0
    if args.action in {"pin", "unpin"}:
        return client.call(path + "/pin", {"pinned": args.action == "pin"}), "mapping", 0
    result = client.call(path + "/" + args.action, {})
    if args.action == "check" and result.get("status") == "stopped":
        return result, "unchecked_mapping", 1
    ok = result.get("status") == "stopped" if args.action == "stop" else mapping_status(result) == "已启动"
    return result, "mapping", 0 if ok else 1


def _text(value):
    text = str(value if value is not None else "")
    return "".join(char if char in "\n\t" or ord(char) >= 32 and ord(char) != 127
                   else f"\\x{ord(char):02x}" for char in text)


def _cell(value):
    return _text(value).replace("\n", " ").replace("\r", " ").replace("\t", " ")


def _endpoint(mapping, side):
    return f"{mapping.get(side + '_host', '?')}:{mapping.get(side + '_port', '?')}"


def _print_human(result, kind):
    if kind == "hosts":
        print("设备\tSSH 地址\t设备路径")
        for host in result.get("hosts", []):
            address = f"{host.get('user', '') + '@' if host.get('user') else ''}{host.get('hostname', '本机')}:{host.get('port', 22)}"
            route = " → ".join(str(hop) for hop in host.get("route", []))
            print("\t".join(map(_cell, (host.get("alias") or host.get("id"), address, route))))
        if not result.get("hosts"):
            print("尚未识别设备。")
    elif kind == "probe":
        print(_text(f"{result.get('alias', '')}：{'测试通过' if result.get('ok') is True else '测试失败'}。{result.get('message', '')}"))
    elif kind == "mappings":
        print("ID\t名称\t状态\t目标进程\t入口 → 目标\t置顶")
        for item in result.get("mappings", []):
            print("\t".join(map(_cell, (item.get("id"), item.get("name"), mapping_status(item),
                                        process_summary(item), f"{_endpoint(item, 'source')} → {_endpoint(item, 'target')}",
                                        "是" if item.get("pinned") is True else "否"))))
        if not result.get("mappings"):
            print("尚无映射。")
    elif kind in {"mapping", "unchecked_mapping"}:
        print(_text(f"{result.get('name', '未命名映射')} [{result.get('id', '?')}]  {mapping_status(result)}"))
        print(_text(f"{_endpoint(result, 'source')} → {_endpoint(result, 'target')}"))
        print(_text(f"入口监听地址：{result.get('bind_address', '127.0.0.1')}；目标服务地址：{result.get('target_address', '127.0.0.1')}"))
        if kind == "unchecked_mapping":
            print("映射尚未启动，未执行检查；请先启动此映射。")
        print(process_summary(result))
        target_usage = result.get("target_usage") or {}
        if target_usage.get("checked_at"):
            print(_text("进程快照：" + target_usage["checked_at"] + "（启动或检查时更新）"))
        print(f"置顶：{'是' if result.get('pinned') is True else '否'}；自动启动：{'是' if result.get('auto_start') is True else '否'}")
        if result.get("error"):
            print(_text("原因：" + str(result["error"])))
        elif mapping_status(result) == "未确认" and (result.get("health") or {}).get("summary"):
            print(_text(result["health"]["summary"]))
    elif kind == "deleted":
        print("映射已删除。")
    elif kind == "server_logs":
        for line in result["lines"]:
            print(_text(line))
        if not result["lines"]:
            print("暂无服务日志。")
    elif kind == "mapping_logs":
        for item in result["logs"]:
            if isinstance(item, dict):
                print(_text(f"{item.get('time', '')} {item.get('level', '')} {item.get('message', '')}".strip()))
            else:
                print(_text(item))
        if not result["logs"]:
            print("暂无映射日志。")


def _http_message(error):
    try:
        result = json.loads(error.read(65536).decode("utf-8"))
        if isinstance(result, dict) and isinstance(result.get("error"), str):
            return result["error"]
    except (OSError, ValueError, AttributeError):
        pass
    return f"服务请求失败（HTTP {error.code}）。"


def run(argv, *, running, request, data: Path) -> int:
    """Dispatch hosts/mappings/logs using app.running and app.request callbacks.

    The request callback retains (url, body=None, token=None, timeout=3), with an
    optional method keyword for PUT/DELETE. JSON results are raw API objects;
    errors are {"error": message} on stdout. Human errors go to stderr.
    """
    argv, json_output = _json_args(argv)
    try:
        args = _parser().parse_args(argv)
        result, kind, code = _execute(args, running=running, request=request, data=Path(data))
        if json_output:
            print(json.dumps(result, ensure_ascii=False))
        else:
            _print_human(result, kind)
        return code
    except SystemExit as error:
        return int(error.code or 0)
    except CommandError as error:
        message, code = str(error), error.code
    except HTTPError as error:
        message, code = _http_message(error), 1
    except (URLError, ConnectionError, TimeoutError) as error:
        message, code = f"无法联系本机服务：{error}。请运行 app.py status，必要时运行 app.py start。", 1
    except KeyboardInterrupt:
        message, code = "已取消等待；服务可能仍在处理请求，请查询映射状态。", 130
    except Exception as error:
        message, code = str(error) or "命令执行失败。", 1
    if json_output:
        print(json.dumps({"error": message}, ensure_ascii=False))
    else:
        print(_text("错误：" + message), file=sys.stderr)
    return code
