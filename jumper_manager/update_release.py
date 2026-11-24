"""Read and verify executable updates from this project's public releases.

This module only downloads into a private staging directory. It never modifies
the installed application, its configuration, or a running tunnel.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from http.client import HTTPException, IncompleteRead
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import ssl
import stat
import struct
import sys
import tarfile
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, getproxies
import zipfile


REPOSITORY = "pgq18/JumperManager"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_ROOT = f"https://github.com/{REPOSITORY}/releases"
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 256 * 1024
NETWORK_TIMEOUT = 30
NETWORK_ATTEMPTS = 3
DOWNLOAD_TIMEOUT = 20 * 60
_HOSTS = frozenset({"api.github.com", "github.com", "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com", "github-releases.githubusercontent.com"})
_VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                      r"(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_ASSETS = {"windows": ("JumperManager-Windows-x64.zip", "JumperManager.exe"),
           "linux": ("JumperManager-Linux-x86_64.tar.gz", "jumper-manager")}
PACKAGE_DOCS = frozenset({"README.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "SOURCE.md",
                          "SOURCE-LINUX.md", "docs/LINUX.md"})


class UpdateError(RuntimeError):
    """An update could not be checked or safely prepared."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    current_version: str
    available: bool
    url: str
    asset_name: str
    asset_url: str
    checksums_url: str
    asset_size: int
    expected_digest: str | None = None
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _version(value: str, *, stable: bool = False) -> tuple[tuple[int, int, int], str | None]:
    if not isinstance(value, str) or len(value) > 100:
        raise UpdateError("版本号格式无效。")
    match = _VERSION.fullmatch(value)
    if not match or (stable and (match[4] or match[5])):
        raise UpdateError(f"版本号格式无效：{value}")
    for identifier in (match[4] or "").split("."):
        if match[4] and (not identifier or (identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0")):
            raise UpdateError(f"版本号格式无效：{value}")
    if match[5] and any(not part for part in match[5].split(".")):
        raise UpdateError(f"版本号格式无效：{value}")
    return tuple(int(match[index]) for index in (1, 2, 3)), match[4]


def _is_newer(latest: str, current: str) -> bool:
    newest, _ = _version(latest, stable=True)
    installed, suffix = _version(current)
    return newest > installed or (newest == installed and suffix is not None)


def _platform_name(value: str | None) -> str:
    value = (value or sys.platform).lower()
    if value in {"win32", "windows", "win"}:
        result = "windows"
    elif value in {"linux", "linux2"}:
        result = "linux"
    else:
        raise UpdateError("自动更新目前支持 Windows x64 和 Linux x86_64。")
    if platform.machine().lower() not in {"amd64", "x86_64"} or struct.calcsize("P") != 8:
        raise UpdateError("此设备的架构暂不支持自动更新，请从发布页下载适合的版本。")
    return result


def _transport_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname in _HOSTS
                 and parts.port in {None, 443} and not parts.username and not parts.password
                 and not parts.fragment and not any(ord(char) < 32 for char in url))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise UpdateError("更新下载地址不属于受信任的 GitHub HTTPS 地址。")


class _GitHubRedirects(HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _transport_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, *, direct=False, method="GET"):
    _transport_url(url)
    headers = {"User-Agent": "JumperManager-Updater", "Accept-Encoding": "identity"}
    if url == API_URL:
        headers.update(Accept="application/vnd.github+json", **{"X-GitHub-Api-Version": "2022-11-28"})
    # Read proxies afresh: tray applications can outlive changes to Windows'
    # proxy settings. Direct fallback never changes process-wide networking.
    handlers = [_GitHubRedirects()]
    if direct:
        handlers.append(ProxyHandler({}))
    response = build_opener(*handlers).open(Request(url, headers=headers, method=method), timeout=NETWORK_TIMEOUT)
    try:
        _transport_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response


def _certificate_error(exc):
    reason = exc.reason if isinstance(exc, URLError) else exc
    return isinstance(reason, (ssl.SSLCertVerificationError, ssl.CertificateError))


def _retryable(exc):
    if _certificate_error(exc):
        return False
    if isinstance(exc, HTTPError):
        # Rate limits can last an hour. Switch metadata channels instead of
        # hammering the same API; never sleep for an unbounded Retry-After.
        return exc.code in {408, 425, 500, 502, 503, 504}
    return isinstance(exc, (URLError, TimeoutError, ConnectionError, HTTPException, socket.gaierror))


def _proxy_failure(exc):
    return (_retryable(exc) and not isinstance(exc, HTTPError)) or (
        isinstance(exc, HTTPError) and exc.code == 407)


def _request(operation, progress=None):
    """Retry a complete read, so a reset after headers is retryable too."""
    proxies = getproxies()
    can_go_direct = bool(proxies.get("https") or proxies.get("http"))
    direct = False
    for attempt in range(NETWORK_ATTEMPTS):
        try:
            return operation(direct)
        except Exception as exc:
            switch_route = can_go_direct and _proxy_failure(exc)
            if isinstance(exc, HTTPError):
                exc.close()
            if attempt == NETWORK_ATTEMPTS - 1 or not (_retryable(exc) or switch_route):
                raise
            if switch_route:
                direct = not direct
            if progress:
                route = "尝试直接连接" if direct else "重新连接"
                progress(f"网络暂时不可用，{route}（第 {attempt + 2}/{NETWORK_ATTEMPTS} 次）…")
            time.sleep(attempt + 1)


def _response(url, direct, *, method="GET"):
    # Keep the common call simple for injected transports and embedders.
    if not direct and method == "GET":
        return _open(url)
    return _open(url, direct=direct, method=method)


def _fetch_bytes(url: str, limit: int, progress=None) -> bytes:
    def read(direct):
        with _response(url, direct) as response:
            content = response.read(limit + 1)
            length = response.headers.get("Content-Length")
        if len(content) > limit:
            raise UpdateError("更新服务器返回的数据过大。")
        if length is not None:
            try:
                expected = int(length)
            except ValueError as exc:
                raise UpdateError("更新服务器返回了无效的文件长度。") from exc
            if expected != len(content):
                raise IncompleteRead(content, expected - len(content))
        return content
    return _request(read, progress)


def _network_error(exc: Exception) -> UpdateError:
    # urllib exceptions may contain proxy credentials or signed download URLs.
    # Do not put their raw repr/str in dialogs, terminal output, or logs.
    if _certificate_error(exc):
        return UpdateError("无法验证 GitHub 的 HTTPS 证书。请检查系统时间、系统证书或代理的证书设置；未关闭证书校验。")
    if isinstance(exc, HTTPError):
        if exc.code in {403, 429}:
            return UpdateError("GitHub 暂时限制了更新请求，请稍后重试。")
        if exc.code == 404:
            return UpdateError("暂时找不到正式版更新或下载文件，请稍后重试。")
        if exc.code == 407:
            return UpdateError("网络代理要求认证，请检查系统代理或 HTTPS_PROXY 的认证设置后重试。")
        return UpdateError(f"检查或下载更新失败：GitHub HTTP {exc.code}。")
    if isinstance(exc, (URLError, TimeoutError, ConnectionError, HTTPException, socket.gaierror)):
        return UpdateError("无法连接 GitHub，或下载连接中断。已尝试重连；请确认系统代理正在运行，Linux 可设置 HTTPS_PROXY，然后重试。也可从官方发布页手动下载。")
    return UpdateError(f"无法准备更新：{exc}")


def _asset_url(tag: str, name: str) -> str:
    return f"{RELEASE_ROOT}/download/{tag}/{name}"


def _release_info(data: object, current: str, platform_name: str) -> ReleaseInfo:
    if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
        raise UpdateError("更新信息不是正式发布的版本。")
    tag = data.get("tag_name")
    _version(tag, stable=True)
    version = tag.removeprefix("v")
    release_url = f"{RELEASE_ROOT}/tag/{tag}"
    if data.get("html_url") != release_url:
        raise UpdateError("更新信息中的仓库地址不正确。")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("更新信息缺少下载文件。")
    name, _ = _ASSETS[platform_name]

    def find(asset_name):
        matches = [item for item in assets if isinstance(item, dict) and item.get("name") == asset_name]
        if len(matches) != 1:
            raise UpdateError(f"此版本缺少唯一的下载文件：{asset_name}")
        item = matches[0]
        if item.get("browser_download_url") != _asset_url(tag, asset_name):
            raise UpdateError("下载文件的地址与官方发布页不一致。")
        return item

    asset, checksums = find(name), find("SHA256SUMS.txt")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= MAX_ASSET_BYTES:
        raise UpdateError("下载文件大小无效或超出限制。")
    digest = asset.get("digest")
    if digest is not None:
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not _SHA256.fullmatch(digest[7:]):
            raise UpdateError("下载文件的 SHA-256 摘要无效。")
        digest = digest[7:].lower()
    notes = data.get("body") or ""
    if not isinstance(notes, str):
        raise UpdateError("更新说明格式无效。")
    return ReleaseInfo(version, current, _is_newer(version, current), release_url, name,
                       asset["browser_download_url"], checksums["browser_download_url"], size, digest, notes)


def _page_release(current: str, platform_name: str, progress=None) -> ReleaseInfo:
    """Official release-page channel, independent of the REST API quota.

    GitHub documents /releases/latest as its latest-release link. Resolve that
    link once, pin every subsequent request to the same stable tag, and require
    the published checksum before offering an update.
    """
    def headers(url):
        def read(direct):
            try:
                response = _response(url, direct, method="HEAD")
            except HTTPError as exc:
                if exc.code not in {405, 501}:
                    raise
                exc.close()
                response = _response(url, direct)
            with response:
                return response.geturl(), response.headers.get("Content-Length")
        return _request(read, progress)

    release_url, _ = headers(RELEASE_ROOT + "/latest")
    prefix = RELEASE_ROOT + "/tag/"
    if not release_url.startswith(prefix):
        raise UpdateError("官方发布页未指向本项目的正式版本，已取消检查。")
    tag = release_url.removeprefix(prefix)
    _version(tag, stable=True)
    version = tag.removeprefix("v")
    name = _ASSETS[platform_name][0]
    asset_url = _asset_url(tag, name)
    checksums_url = _asset_url(tag, "SHA256SUMS.txt")
    expected = _checksum(_fetch_bytes(checksums_url, MAX_CHECKSUM_BYTES, progress), name)
    _, raw_size = headers(asset_url)
    try:
        size = int(raw_size)
    except (ValueError, TypeError) as exc:
        raise UpdateError("官方发布页的下载文件缺少有效大小，无法安全准备更新。") from exc
    if not 0 < size <= MAX_ASSET_BYTES:
        raise UpdateError("下载文件大小无效或超出限制。")
    return ReleaseInfo(version, current, _is_newer(version, current), release_url,
                       name, asset_url, checksums_url, size, expected)


def check_release(current_version: str, platform_name: str | None = None, *, progress=None) -> ReleaseInfo:
    """Check the latest official stable release; never offer a downgrade."""
    _version(current_version)
    selected_platform = _platform_name(platform_name)
    try:
        if progress:
            progress("正在检查最新正式版本…")
        content = (_fetch_bytes(API_URL, MAX_METADATA_BYTES, progress) if progress
                   else _fetch_bytes(API_URL, MAX_METADATA_BYTES))
        data = json.loads(content)
        return _release_info(data, current_version, selected_platform)
    except UpdateError:
        raise
    except (OSError, HTTPException, ValueError, TypeError) as exc:
        if _certificate_error(exc):
            raise _network_error(exc) from None
        if progress:
            progress("GitHub 更新接口暂不可用，正在通过官方发布页检查…")
        try:
            return _page_release(current_version, selected_platform, progress)
        except UpdateError:
            raise
        except (OSError, HTTPException, ValueError, TypeError) as fallback:
            raise _network_error(fallback) from None


def _validate_info(info: ReleaseInfo) -> str:
    _version(info.version, stable=True)
    if info.version.startswith("v"):
        raise UpdateError("待安装的版本号格式无效。")
    if not _is_newer(info.version, info.current_version) or info.available is not True:
        raise UpdateError("没有可安装的更新，当前版本不会降级或重复安装。")
    platform_name = next((key for key, names in _ASSETS.items() if names[0] == info.asset_name), None)
    if platform_name is None:
        raise UpdateError("更新文件不适用于受支持的平台。")
    # Accept published tags with or without the customary v prefix, but all
    # three URLs must point to the same exact repository and release tag.
    tag = info.url.removeprefix(RELEASE_ROOT + "/tag/")
    if tag not in {info.version, "v" + info.version} or info.url != f"{RELEASE_ROOT}/tag/{tag}":
        raise UpdateError("待安装更新的发布页地址无效。")
    if info.asset_url != _asset_url(tag, info.asset_name) or info.checksums_url != _asset_url(tag, "SHA256SUMS.txt"):
        raise UpdateError("待安装更新的下载地址无效。")
    if type(info.asset_size) is not int or not 0 < info.asset_size <= MAX_ASSET_BYTES:
        raise UpdateError("待安装更新的文件大小无效。")
    if info.expected_digest is not None and (not isinstance(info.expected_digest, str)
                                          or not _SHA256.fullmatch(info.expected_digest)):
        raise UpdateError("待安装更新的摘要无效。")
    return platform_name


def _checksum(content: bytes, asset_name: str) -> str:
    try:
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise UpdateError("SHA256SUMS.txt 编码无效。") from exc
    found = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)", line)
        if match and match[2] == asset_name:
            found.append(match[1].lower())
    if len(found) != 1:
        raise UpdateError(f"SHA256SUMS.txt 缺少唯一的 {asset_name} 校验值。")
    return found[0]


def _download(info: ReleaseInfo, destination: Path, progress: Callable[[str], None] | None) -> str:
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT

    def download(direct):
        digest = hashlib.sha256()
        count, last_report = 0, 0.0
        created = False
        try:
            with _response(info.asset_url, direct) as response, destination.open("xb") as output:
                created = True
                header = response.headers.get("Content-Length")
                if header is not None:
                    try:
                        length = int(header)
                    except ValueError as exc:
                        raise UpdateError("下载服务器返回了无效的文件长度。") from exc
                    if length != info.asset_size:
                        raise UpdateError("下载文件长度与发布信息不一致。")
                while True:
                    if time.monotonic() > deadline:
                        raise UpdateError("下载更新超时，请检查网络后重试。")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > info.asset_size:
                        raise UpdateError("下载文件超过发布信息中的大小。")
                    output.write(chunk)
                    digest.update(chunk)
                    if progress and (time.monotonic() - last_report >= 1 or count == info.asset_size):
                        progress(f"正在下载 {info.version}：{count * 100 // info.asset_size}%")
                        last_report = time.monotonic()
                if count != info.asset_size:
                    raise IncompleteRead(b"", info.asset_size - count)
                output.flush()
                os.fsync(output.fileno())
            return digest.hexdigest()
        except Exception:
            # Never append to a partial archive, and never remove a file that
            # pre-existed this request. Each retry starts from an empty file.
            if created:
                destination.unlink(missing_ok=True)
            raise

    return _request(download, progress)


def _member_name(name: str, seen: set[str]) -> None:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise UpdateError("更新包包含不安全的文件路径。")
    parts = name.rstrip("/").split("/")
    if (parts[0] != "JumperManager" or len(parts) > 32
            or any(not part or part in {".", ".."} or part.endswith((" ", ".")) for part in parts)):
        raise UpdateError("更新包包含越界或无效的文件路径。")
    normalized = "/".join(parts).casefold()
    if normalized in seen:
        raise UpdateError("更新包包含重复文件路径。")
    seen.add(normalized)


def is_package_file(relative: str, platform_name: str) -> bool:
    """Whether a canonical relative path is an installable package artifact.

    User data and arbitrary executables/scripts are deliberately excluded.
    Installers share this allowlist when copying the prepared package.
    """
    platform_name = {"win32": "windows", "win": "windows", "linux2": "linux"}.get(platform_name, platform_name)
    if platform_name not in _ASSETS or not isinstance(relative, str):
        return False
    try:
        _member_name("JumperManager/" + relative, set())
    except UpdateError:
        return False
    binary = _ASSETS[platform_name][1]
    build_record = "JumperManager.build.json" if platform_name == "windows" else "jumper-manager.build.json"
    checksum = "JumperManager.exe.sha256" if platform_name == "windows" else "jumper-manager.sha256"
    return (relative in PACKAGE_DOCS or relative in {binary, build_record, checksum}
            or (relative.startswith("licenses/") and Path(relative).suffix.lower() in {".txt", ".html", ".json"}))


def _copy_file(source, destination: Path, size: int) -> None:
    if not 0 <= size <= MAX_ASSET_BYTES:
        raise UpdateError("更新包中的文件大小无效。")
    count = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > size:
                raise UpdateError("更新包中的文件大小不一致。")
            output.write(chunk)
        if count != size:
            raise UpdateError("更新包中的文件不完整。")
        output.flush()
        os.fsync(output.fileno())


def _extract(archive_path: Path, executable: Path, platform_name: str) -> None:
    expected = f"JumperManager/{executable.name}"
    seen, total, count, targets = set(), 0, 0, []
    if platform_name == "windows":
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                count += 1
                total += member.file_size
                # ZipInfo normalizes backslashes on Windows and truncates NULs;
                # validate the original archive name before that normalization.
                _member_name(member.orig_filename, seen)
                kind = stat.S_IFMT(member.external_attr >> 16)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or (member.flag_bits & 1):
                    raise UpdateError("更新包包含链接、特殊文件或加密文件。")
                if kind == stat.S_IFDIR and not member.is_dir():
                    raise UpdateError("更新包中的文件类型不一致。")
                if count > 10000 or total > MAX_EXPANDED_BYTES:
                    raise UpdateError("更新包解压大小超出限制。")
                if not member.is_dir() and is_package_file(member.filename.removeprefix("JumperManager/"), platform_name):
                    targets.append(member)
            if not any(member.filename == expected for member in targets):
                raise UpdateError("更新包中缺少 JumperManager 可执行文件。")
            for target in targets:
                destination = executable.parent / target.filename.removeprefix("JumperManager/")
                with archive.open(target) as source:
                    _copy_file(source, destination, target.file_size)
    else:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                count += 1
                total += member.size
                _member_name(member.name, seen)
                if not (member.isfile() or member.isdir()) or member.issparse():
                    raise UpdateError("更新包包含链接或特殊文件。")
                if count > 10000 or total > MAX_EXPANDED_BYTES:
                    raise UpdateError("更新包解压大小超出限制。")
                if member.isfile() and is_package_file(member.name.removeprefix("JumperManager/"), platform_name):
                    targets.append(member)
            if not any(member.name == expected for member in targets):
                raise UpdateError("更新包中缺少 JumperManager 可执行文件。")
            for target in targets:
                destination = executable.parent / target.name.removeprefix("JumperManager/")
                with archive.extractfile(target) as source:
                    _copy_file(source, destination, target.size)
    _executable_format(executable, platform_name)
    if platform_name == "linux":
        executable.chmod(0o755)


def _executable_format(path: Path, platform_name: str) -> None:
    with path.open("rb") as source:
        header = source.read(64)
        if platform_name == "windows":
            if len(header) < 64 or header[:2] != b"MZ":
                raise UpdateError("更新包中不是有效的 Windows 可执行文件。")
            offset = struct.unpack_from("<I", header, 60)[0]
            if not 64 <= offset <= min(path.stat().st_size - 26, 16 * 1024 * 1024):
                raise UpdateError("更新包中的 Windows 文件头无效。")
            source.seek(offset)
            pe = source.read(26)
            if pe[:6] != b"PE\x00\x00\x64\x86" or pe[24:26] != b"\x0b\x02":
                raise UpdateError("更新包不是 Windows x64 可执行文件。")
        elif (len(header) < 64 or header[:6] != b"\x7fELF\x02\x01"
              or struct.unpack_from("<H", header, 18)[0] != 62
              or struct.unpack_from("<H", header, 16)[0] not in {2, 3}):
            raise UpdateError("更新包不是 Linux x86_64 可执行文件。")


def stage_release(info: ReleaseInfo, staging_dir: Path,
                  progress: Callable[[str], None] | None = None) -> Path:
    """Verify and stage the executable and package documents without installing.

    The returned path belongs to a new private child directory of staging_dir.
    Callers own its eventual cleanup. progress, when provided, accepts a short
    human-readable message. All incomplete staging files are removed on failure.
    """
    selected_platform = _validate_info(info)
    staging_dir = Path(staging_dir)
    if staging_dir.is_symlink():
        raise UpdateError("更新暂存目录不能是符号链接。")
    work = None
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="release-", dir=staging_dir))
        if progress:
            progress(f"正在下载并校验 {info.version}…")
        expected = _checksum(_fetch_bytes(info.checksums_url, MAX_CHECKSUM_BYTES, progress), info.asset_name)
        if info.expected_digest and expected != info.expected_digest.lower():
            raise UpdateError("发布信息与 SHA256SUMS.txt 中的校验值不一致，已取消更新。")
        archive = work / info.asset_name
        actual = _download(info, archive, progress)
        if actual != expected:
            raise UpdateError("更新文件的 SHA-256 校验失败，已取消更新。")
        executable = work / _ASSETS[selected_platform][1]
        _extract(archive, executable, selected_platform)
        archive.unlink()
        if progress:
            progress("更新文件已通过校验。")
        return executable
    except Exception as exc:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        if isinstance(exc, UpdateError):
            raise
        if isinstance(exc, (OSError, HTTPException, ValueError, RuntimeError, EOFError, tarfile.TarError, zipfile.BadZipFile)):
            raise _network_error(exc) from None
        raise
