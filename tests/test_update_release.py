"""Release trust boundaries, version ordering and executable archive staging."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
import zipfile

from jumper_manager import update_release as updates


def windows_executable():
    value = bytearray(128)
    value[:2] = b"MZ"
    struct.pack_into("<I", value, 60, 64)
    value[64:70] = b"PE\0\0\x64\x86"
    value[88:90] = b"\x0b\x02"
    return bytes(value)


def linux_executable():
    value = bytearray(128)
    value[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", value, 16, 3, 62)
    return bytes(value)


def package(platform_name="windows", entries=None):
    target = "JumperManager/" + updates._ASSETS[platform_name][1]
    executable = windows_executable() if platform_name == "windows" else linux_executable()
    entries = entries if entries is not None else [(target, executable), ("JumperManager/README.md", b"Read me")]
    output = io.BytesIO()
    if platform_name == "windows":
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                item = name if isinstance(name, zipfile.ZipInfo) else zipfile.ZipInfo(name)
                # ZipInfo normalizes backslashes on Windows during construction.
                # Preserve the raw name to test hostile archives consistently.
                if isinstance(name, str):
                    item.filename = name
                archive.writestr(item, content)
    else:
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, content in entries:
                item = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
    return output.getvalue()


def metadata(platform_name="windows", data=None):
    data = data if data is not None else package(platform_name)
    tag = "v1.2.3"
    name = updates._ASSETS[platform_name][0]
    digest = hashlib.sha256(data).hexdigest()
    return {"draft": False, "prerelease": False, "tag_name": tag,
            "html_url": f"{updates.RELEASE_ROOT}/tag/{tag}", "body": "Changes",
            "assets": [{"name": name, "size": len(data), "digest": "sha256:" + digest,
                        "browser_download_url": updates._asset_url(tag, name)},
                       {"name": "SHA256SUMS.txt", "size": 100,
                        "browser_download_url": updates._asset_url(tag, "SHA256SUMS.txt")}]}


class Response(io.BytesIO):
    def __init__(self, data, url, length=None):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def geturl(self):
        return self.url


class VersionsTests(unittest.TestCase):
    def test_ordering_and_no_development_downgrade(self):
        cases = [("1.2.3", "1.2.2", True), ("1.2.3", "1.2.3-dev", True),
                 ("1.2.2", "1.2.3-dev", False), ("1.2.3", "1.2.3", False),
                 ("1.2.3", "1.2.3+build.9", False), ("1.10.0", "1.9.99", True),
                 ("2.0.0", "2.0.0-rc.1", True), ("1.9.9", "2.0.0-rc.1", False)]
        for latest, current, expected in cases:
            with self.subTest(latest=latest, current=current):
                self.assertEqual(updates._is_newer(latest, current), expected)

    def test_reject_invalid_versions(self):
        for value in [None, "", "1.2", "v01.2.3", "1.2.3.4", "1.2.3-dev..1", "1.2.3-01", "1.2.3+", "9" * 101]:
            with self.subTest(value=value), self.assertRaises(updates.UpdateError):
                updates._version(value)

    def test_release_version_must_be_stable(self):
        with self.assertRaises(updates.UpdateError):
            updates._is_newer("1.2.3-dev", "1.2.2")


class ReleaseTests(unittest.TestCase):
    def test_checks_platform_release_with_serializable_result(self):
        for platform_name in ["windows", "linux"]:
            with self.subTest(platform_name=platform_name):
                with patch.object(updates, "_fetch_bytes", return_value=json.dumps(metadata(platform_name)).encode()) as fetch:
                    with patch.object(updates.platform, "machine", return_value="x86_64"):
                        info = updates.check_release("1.2.2", platform_name)
                self.assertTrue(info.available)
                self.assertEqual(info.version, "1.2.3")
                self.assertEqual(info.asset_name, updates._ASSETS[platform_name][0])
                self.assertEqual(json.loads(json.dumps(info.to_dict()))["current_version"], "1.2.2")
                fetch.assert_called_once_with(updates.API_URL, updates.MAX_METADATA_BYTES)

    def test_refuses_wrong_architecture_or_platform_before_network(self):
        with patch.object(updates, "_fetch_bytes") as fetch:
            for platform_name in ["darwin", "freebsd"]:
                with self.assertRaises(updates.UpdateError):
                    updates.check_release("1.2.2", platform_name)
            with patch.object(updates.platform, "machine", return_value="aarch64"):
                with self.assertRaises(updates.UpdateError):
                    updates.check_release("1.2.2", "linux")
            fetch.assert_not_called()

    def test_rejects_untrusted_or_incomplete_release_metadata(self):
        changes = [lambda data: data.update(draft=True), lambda data: data.update(prerelease=True),
                   lambda data: data.update(tag_name="v1.2.3-dev"),
                   lambda data: data.update(html_url="https://github.com/other/repo/releases/tag/v1.2.3"),
                   lambda data: data["assets"][0].update(browser_download_url="https://example.com/app.zip"),
                   lambda data: data["assets"][1].update(browser_download_url="https://github.com/pgq18/JumperManager/releases/download/v1.2.2/SHA256SUMS.txt"),
                   lambda data: data["assets"].pop(), lambda data: data["assets"].append(data["assets"][0]),
                   lambda data: data["assets"][0].update(size=0), lambda data: data["assets"][0].update(size=True),
                   lambda data: data["assets"][0].update(size=updates.MAX_ASSET_BYTES + 1),
                   lambda data: data["assets"][0].update(digest="md5:abc")]
        for change in changes:
            data = metadata()
            change(data)
            with self.subTest(data=data), self.assertRaises(updates.UpdateError):
                updates._release_info(data, "1.2.2", "windows")

    def test_missing_github_digest_allowed_with_required_checksum_asset(self):
        data = metadata()
        data["assets"][0].pop("digest")
        self.assertIsNone(updates._release_info(data, "1.2.2", "windows").expected_digest)

    def test_rate_limit_has_useful_error(self):
        error = HTTPError(updates.API_URL, 403, "rate limited", {}, None)
        self.addCleanup(error.close)
        with patch.object(updates, "_fetch_bytes", side_effect=error):
            with self.assertRaisesRegex(updates.UpdateError, "稍后重试"):
                updates.check_release("1.2.2")

    def test_invalid_json_becomes_update_error(self):
        with patch.object(updates, "_fetch_bytes", return_value=b"not json"):
            with self.assertRaises(updates.UpdateError):
                updates.check_release("1.2.2")


class TransportTests(unittest.TestCase):
    def test_all_redirects_must_stay_on_allowed_https_hosts(self):
        handler = updates._GitHubRedirects()
        request = Request(updates.API_URL)
        good = "https://release-assets.githubusercontent.com/github-production-release-asset/a/b?signature=c"
        self.assertEqual(handler.redirect_request(request, None, 302, "", {}, good).full_url, good)
        for value in ["http://github.com/pgq18/JumperManager", "https://github.com.example.org/a",
                      "https://example.org/a", "https://github.com:444/a", "https://user@github.com/a",
                      "https://github.com/a#b", "file:///tmp/example", "https://github.com/a\nb"]:
            with self.subTest(value=value), self.assertRaises(updates.UpdateError):
                handler.redirect_request(request, None, 302, "", {}, value)

    def test_metadata_download_limit(self):
        with patch.object(updates, "_open", return_value=Response(b"12345", updates.API_URL)):
            with self.assertRaises(updates.UpdateError):
                updates._fetch_bytes(updates.API_URL, 4)

    def test_network_uses_default_proxies_and_timeout(self):
        response = Response(b"{}", updates.API_URL)
        with patch.object(updates, "build_opener") as builder:
            builder.return_value.open.return_value = response
            self.assertIs(updates._open(updates.API_URL), response)
            self.assertEqual(len(builder.call_args.args), 1)
            self.assertIsInstance(builder.call_args.args[0], updates._GitHubRedirects)
            self.assertEqual(builder.return_value.open.call_args.kwargs, {"timeout": updates.NETWORK_TIMEOUT})


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def stage(self, blob=None, platform_name="windows", *, info_change=None, manifest=None, length=None):
        blob = package(platform_name) if blob is None else blob
        info = updates._release_info(metadata(platform_name, blob), "1.2.2", platform_name)
        if info_change:
            info = info_change(info)
        manifest = (f"{hashlib.sha256(blob).hexdigest()}  {info.asset_name}\n".encode()
                    if manifest is None else manifest)
        messages = []
        with patch.object(updates, "_fetch_bytes", return_value=manifest):
            with patch.object(updates, "_open", side_effect=lambda url: Response(blob, url, length)):
                result = updates.stage_release(info, self.root, messages.append)
        return result, messages

    def test_windows_stages_executable_and_documentation_and_reports_progress(self):
        result, messages = self.stage()
        self.assertEqual(result.read_bytes(), windows_executable())
        self.assertEqual({item.name for item in result.parent.iterdir()}, {"JumperManager.exe", "README.md"})
        self.assertEqual((result.parent / "README.md").read_bytes(), b"Read me")
        self.assertEqual(result.parent.parent, self.root)
        self.assertIn("100%", " ".join(messages))
        self.assertIn("通过校验", messages[-1])

    def test_linux_binary_staged_without_configuration_or_documents(self):
        blob = package("linux", [("JumperManager/jumper-manager", linux_executable()),
                                 ("JumperManager/data/mappings.json", b"must not be extracted")])
        result, _ = self.stage(blob, "linux")
        self.assertEqual(result.read_bytes(), linux_executable())
        self.assertEqual(list(result.parent.iterdir()), [result])
        if os.name != "nt":
            self.assertTrue(result.stat().st_mode & stat.S_IXUSR)

    def test_companion_allowlist_excludes_user_data_and_scripts(self):
        allowed = ["README.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "SOURCE.md", "SOURCE-LINUX.md",
                   "docs/LINUX.md", "licenses/Python-LICENSE.txt", "licenses/linux/COMPONENTS.json",
                   "JumperManager.build.json", "JumperManager.exe.sha256"]
        ignored = ["data/mappings.json", ".ssh/config", "app.py", "malware.exe", "licenses/script.py", "docs/other.md"]
        entries = [("JumperManager/JumperManager.exe", windows_executable())]
        entries.extend(("JumperManager/" + relative, b"companion") for relative in allowed + ignored)
        result, _ = self.stage(package(entries=entries))
        files = {item.relative_to(result.parent).as_posix() for item in result.parent.rglob("*") if item.is_file()}
        self.assertEqual(files, set(allowed) | {"JumperManager.exe"})
        for relative in ignored + ["../README.md", "licenses/../evil.txt", "licenses/a\\bad.txt"]:
            self.assertFalse(updates.is_package_file(relative, "windows"))

    def test_rejects_downgrade_and_modified_urls_before_network(self):
        info = updates._release_info(metadata(), "1.2.2", "windows")
        changes = [{"current_version": "1.2.4-dev"}, {"available": False},
                   {"asset_url": "https://example.com/app.zip"}, {"checksums_url": "https://example.com/SHA256SUMS.txt"},
                   {"asset_name": "not-an-application.zip"}, {"expected_digest": "abc"}]
        with patch.object(updates, "_open") as opened:
            for change in changes:
                with self.subTest(change=change), self.assertRaises(updates.UpdateError):
                    updates.stage_release(replace(info, **change), self.root)
            opened.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_hash_mismatch_and_manifest_disagreement_leave_no_staged_files(self):
        blob = package()
        bad = b"0" * 64 + b"  JumperManager-Windows-x64.zip\n"
        for modify in [None, lambda info: replace(info, expected_digest=None)]:
            with self.subTest(github_digest=modify), self.assertRaisesRegex(updates.UpdateError, "校验"):
                self.stage(blob, manifest=bad, info_change=modify)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_or_duplicate_checksum_refused(self):
        blob = package()
        checksum = f"{hashlib.sha256(blob).hexdigest()}  JumperManager-Windows-x64.zip\n".encode()
        for manifest in [b"", b"not a checksum", checksum + checksum, b"\xff"]:
            with self.subTest(manifest=manifest), self.assertRaises(updates.UpdateError):
                self.stage(blob, manifest=manifest)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_truncated_or_oversized_download_refused(self):
        for delta in [-1, 1]:
            with self.subTest(delta=delta), self.assertRaises(updates.UpdateError):
                self.stage(info_change=lambda info: replace(info, asset_size=info.asset_size + delta))
            self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_archives_and_wrong_executable_format_refused(self):
        for blob in [b"not an archive", package(entries=[("JumperManager/JumperManager.exe", b"not an exe")]),
                     package(entries=[("JumperManager/README.md", b"no exe")])]:
            with self.subTest(blob=blob[:40]), self.assertRaises(updates.UpdateError):
                self.stage(blob)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_archive_traversal_and_links_refused_even_for_ignored_files(self):
        for platform_name in ["windows", "linux"]:
            name = "JumperManager/" + updates._ASSETS[platform_name][1]
            executable = windows_executable() if platform_name == "windows" else linux_executable()
            for bad in ["../outside", "/JumperManager/absolute", "JumperManager/../outside",
                        "JumperManager/a\\b", "JumperManager/C:outside", "JumperManager/a/./b"]:
                blob = package(platform_name, [(name, executable), (bad, b"bad")])
                with self.subTest(platform_name=platform_name, bad=bad), self.assertRaises(updates.UpdateError):
                    self.stage(blob, platform_name)
                self.assertEqual(list(self.root.iterdir()), [])
        symlink = zipfile.ZipInfo("JumperManager/innocent")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        blob = package(entries=[("JumperManager/JumperManager.exe", windows_executable()), (symlink, b"/etc/passwd")])
        with self.assertRaises(updates.UpdateError):
            self.stage(blob)
        symlink = tarfile.TarInfo("JumperManager/innocent")
        symlink.type, symlink.linkname = tarfile.SYMTYPE, "/etc/passwd"
        blob = package("linux", [("JumperManager/jumper-manager", linux_executable()), (symlink, b"")])
        with self.assertRaises(updates.UpdateError):
            self.stage(blob, "linux")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_wrong_architecture_executable_is_refused(self):
        pe = bytearray(windows_executable())
        pe[68:70] = b"\x4c\x01"
        elf = bytearray(linux_executable())
        struct.pack_into("<H", elf, 18, 183)
        for platform_name, content in [("windows", bytes(pe)), ("linux", bytes(elf))]:
            name = "JumperManager/" + updates._ASSETS[platform_name][1]
            with self.subTest(platform_name=platform_name), self.assertRaises(updates.UpdateError):
                self.stage(package(platform_name, [(name, content)]), platform_name)

    def test_duplicate_archive_names_and_unpacked_size_limit_refused(self):
        for platform_name in ["windows", "linux"]:
            name = "JumperManager/" + updates._ASSETS[platform_name][1]
            executable = windows_executable() if platform_name == "windows" else linux_executable()
            blob = package(platform_name, [(name, executable), (name.upper(), executable)])
            with self.subTest(platform_name=platform_name), self.assertRaises(updates.UpdateError):
                self.stage(blob, platform_name)
            with patch.object(updates, "MAX_EXPANDED_BYTES", 100):
                with self.assertRaises(updates.UpdateError):
                    self.stage(package(platform_name), platform_name)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_download_or_progress_failure_cleans_partial_stage(self):
        info = updates._release_info(metadata(), "1.2.2", "windows")
        with patch.object(updates, "_fetch_bytes", side_effect=TimeoutError("offline")):
            with self.assertRaises(updates.UpdateError):
                updates.stage_release(info, self.root)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
