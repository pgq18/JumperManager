"""Read SSH aliases and explain routes; OpenSSH remains the configuration parser."""
from __future__ import annotations

import glob
import hashlib
import os
from pathlib import Path
import re
import shlex

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_INCLUDE_DEPTH = 12
WILDCARD_HOST_EXPLANATION = "Host 通配符是匹配规则，不能枚举为具体设备；请添加明确的 Host 别名。"


def _argument_tokens(value: str) -> list[str]:
    """Split config arguments without treating Windows paths as shell escapes.

    OpenSSH accepts both quoted and unquoted Windows Include paths. Shell shlex
    would turn ``C:\\Users\\name`` into ``C:Usersname``. Only explicit escapes of
    quotes/whitespace need consuming here; ordinary backslashes stay literal.
    """
    result: list[str] = []
    token: list[str] = []
    quote = None
    started = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            if following in {"'", '"'} or (quote is None and following.isspace()):
                token.append(following)
                started = True
                index += 2
                continue
        if quote:
            if char == quote:
                quote = None
            else:
                token.append(char)
        elif char in {"'", '"'}:
            quote = char
            started = True
        elif char == "#":
            break
        elif char.isspace():
            if started:
                result.append("".join(token))
                token = []
                started = False
        else:
            token.append(char)
            started = True
        index += 1
    if quote:
        raise ValueError("Unterminated SSH config quote")
    if started:
        result.append("".join(token))
    return result


def _directive(raw: str) -> tuple[str, list[str]] | None:
    match = re.match(r"^\s*([^\s=#]+)\s*(?:=\s*)?(.*)$", raw)
    if not match:
        return None
    try:
        return match[1].lower(), _argument_tokens(match[2])
    except ValueError:
        # OpenSSH -G will provide the authoritative error during host resolution.
        return None


def _canonical(path: Path) -> tuple[Path, str]:
    resolved = path.expanduser().resolve()
    return resolved, os.path.normcase(str(resolved))


def _include_pattern(pattern: str) -> str:
    # Environment changes also change the fingerprint's expanded-pattern record.
    expanded = os.path.expandvars(os.path.expanduser(pattern))
    pattern_path = Path(expanded)
    if not pattern_path.is_absolute():
        # User Include paths are relative to ~/.ssh, including nested Include.
        pattern_path = Path.home() / ".ssh" / pattern_path
    return str(pattern_path)


def _configuration_tree(path: Path) -> tuple[list[str], tuple]:
    aliases: list[str] = []
    alias_set: set[str] = set()
    seen: set[str] = set()
    records: list[tuple] = []

    def visit(filename: Path, depth: int = 0) -> None:
        try:
            filename, key = _canonical(filename)
        except (OSError, RuntimeError) as exc:
            records.append(("path-error", str(filename), type(exc).__name__, getattr(exc, "errno", None)))
            return
        if depth > MAX_INCLUDE_DEPTH:
            records.append(("depth-limit", key))
            return
        if key in seen:
            records.append(("already-visited", key))
            return
        seen.add(key)
        try:
            content = filename.read_bytes()
        except OSError as exc:
            # A missing root must have a different signature when it is created.
            records.append(("file-error", key, type(exc).__name__, exc.errno))
            return
        records.append(("file", key, hashlib.sha256(content).hexdigest()))
        for raw in content.decode("utf-8-sig", errors="replace").splitlines():
            directive = _directive(raw)
            if not directive:
                continue
            keyword, arguments = directive
            if keyword == "host":
                for alias in arguments:
                    # Wildcard and negated patterns describe rules, not devices.
                    if alias != "local" and ALIAS_RE.fullmatch(alias) and alias not in alias_set:
                        aliases.append(alias)
                        alias_set.add(alias)
            elif keyword == "include":
                for pattern in arguments:
                    expanded_pattern = _include_pattern(pattern)
                    matches = sorted(glob.glob(expanded_pattern), key=os.path.normcase)
                    canonical_matches = []
                    for match_path in matches:
                        try:
                            canonical_matches.append(_canonical(Path(match_path))[1])
                        except (OSError, RuntimeError):
                            canonical_matches.append(os.path.normcase(os.path.abspath(match_path)))
                    # Fingerprint the match list even when empty: new/deleted glob
                    # matches must be detected without changing the parent file.
                    records.append(("include", key, expanded_pattern, tuple(canonical_matches)))
                    for match_path in matches:
                        visit(Path(match_path), depth + 1)

    visit(Path(path))
    return aliases, tuple(records)


def discover_aliases(path: Path) -> list[str]:
    """Enumerate literal Host aliases; wildcard Host rules are not devices."""
    return _configuration_tree(path)[0]


def configuration_signature(path: Path) -> tuple:
    """Fingerprint root, recursive Include bytes and glob expansion membership.

    The returned immutable value can be compared on each watcher tick. It also
    accounts for missing/unreadable paths and recursion limits; no SSH commands
    or network calls are performed, and timestamps are intentionally irrelevant.
    """
    return _configuration_tree(path)[1]


def parse_effective_config(output: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            result.setdefault(parts[0].lower(), []).append(parts[1])
    return result


def simple_proxy(command: str) -> dict | None:
    """Recognize ssh -W host:port [options] gateway, never execute our parse."""
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not parts or Path(parts[0].replace("\\", "/")).name.lower() not in {"ssh", "ssh.exe"}:
        return None
    if any(token in {"|", "||", "&&", ";", ">", "<", "&"} for token in parts):
        return None
    destination = None
    forward = None
    options: list[str] = []
    index = 1
    while index < len(parts):
        token = parts[index]
        if token == "-W" and index + 1 < len(parts):
            forward = parts[index + 1]
            index += 2
        elif token in {"-p", "-l", "-i", "-F", "-o", "-J"} and index + 1 < len(parts):
            options.extend([token, parts[index + 1]])
            index += 2
        elif token.startswith("-W") and len(token) > 2:
            forward = token[2:]
            index += 1
        elif token in {"-q", "-T", "-v", "-vv", "-vvv", "-4", "-6"}:
            options.append(token)
            index += 1
        elif not token.startswith("-") and destination is None:
            destination = token
            index += 1
        else:
            return None
    if not forward or not destination:
        return None
    alias = destination.rsplit("@", 1)[-1]
    if not ALIAS_RE.fullmatch(alias):
        return None
    return {"alias": alias, "destination": destination, "forward": forward, "options": options}


def jump_alias(value: str) -> str:
    value = value.removeprefix("ssh://").rsplit("@", 1)[-1]
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.split(":", 1)[0]


def route_for(alias: str, resolve, stack: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    if alias in stack or len(stack) >= 12:
        return [alias], ["SSH 路由含循环或超过 12 层，请检查 ProxyJump / ProxyCommand。"]
    config = resolve(alias)
    jump = config.get("proxyjump", ["none"])[0]
    proxy = config.get("proxycommand", ["none"])[0]
    route: list[str] = []
    warnings: list[str] = []
    if jump not in {"none", ""}:
        for token in jump.split(","):
            gateway = jump_alias(token)
            if not ALIAS_RE.fullmatch(gateway):
                warnings.append("ProxyJump 含特殊地址；拓扑仅显示可识别的节点，连接仍由 OpenSSH 处理。")
                continue
            subroute, subwarnings = route_for(gateway, resolve, (*stack, alias))
            route.extend(subroute)
            warnings.extend(subwarnings)
    elif proxy not in {"none", ""}:
        parsed = simple_proxy(proxy)
        if parsed:
            subroute, subwarnings = route_for(parsed["alias"], resolve, (*stack, alias))
            route.extend(subroute)
            warnings.extend(subwarnings)
        else:
            warnings.append("自定义 ProxyCommand 无法完整解析；将按原 SSH 配置连接，拓扑可能省略中间节点。")
    route.append(alias)
    # Preserve route order, without duplicate gateways in multi-hop chains.
    return list(dict.fromkeys(route)), list(dict.fromkeys(warnings))
