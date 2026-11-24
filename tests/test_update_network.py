"""Transient outage, proxy fallback and official release-channel recovery."""
from dataclasses import replace
import hashlib
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from jumper_manager import update_release as updates


class Response(io.BytesIO):
    def __init__(self, content=b"", url=updates.API_URL, length=None):
        super().__init__(content)
        self.url = url
        self.headers = {"Content-Length": str(len(content) if length is None else length)}

    def geturl(self):
        return self.url


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(updates.time, "sleep").start()
        self.proxies = patch.object(updates, "getproxies", return_value={}).start()
        self.addCleanup(patch.stopall)

    def test_transient_metadata_recovers_after_two_retries(self):
        with patch.object(updates, "_open", side_effect=[TimeoutError(), URLError("reset"), Response(b"{}")]) as opened:
            self.assertEqual(updates._fetch_bytes(updates.API_URL, 100), b"{}")
        self.assertEqual(opened.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1, 2])

    def test_dead_proxy_tries_direct_without_changing_environment(self):
        self.proxies.return_value = {"https": "http://user:password@localhost:9"}
        messages = []
        with patch.object(updates, "_open", side_effect=[URLError(ConnectionRefusedError()), Response(b"{}")]) as opened:
            self.assertEqual(updates._fetch_bytes(updates.API_URL, 100, messages.append), b"{}")
        self.assertEqual(opened.call_args_list[0].kwargs, {})
        self.assertEqual(opened.call_args_list[1].kwargs, {"direct": True, "method": "GET"})
        self.assertIn("直接连接", messages[0])
        self.assertNotIn("password", messages[0])

    def test_proxy_auth_failure_can_try_direct(self):
        self.proxies.return_value = {"https": "http://localhost:9"}
        error = HTTPError(updates.API_URL, 407, "auth", {}, None)
        with patch.object(updates, "_open", side_effect=[error, Response(b"{}")]):
            self.assertEqual(updates._fetch_bytes(updates.API_URL, 100), b"{}")

    def test_retries_are_bounded_even_when_proxy_and_direct_both_fail(self):
        self.proxies.return_value = {"https": "http://localhost:9"}
        with patch.object(updates, "_open", side_effect=TimeoutError()) as opened:
            with self.assertRaises(TimeoutError):
                updates._fetch_bytes(updates.API_URL, 100)
        self.assertEqual(opened.call_count, 3)

    def test_certificate_errors_never_retry_or_fall_back(self):
        self.proxies.return_value = {"https": "http://localhost:9"}
        error = URLError(ssl.SSLCertVerificationError("untrusted"))
        with patch.object(updates, "_open", side_effect=error) as opened, patch.object(updates, "_page_release") as fallback:
            with self.assertRaisesRegex(updates.UpdateError, "证书"):
                updates.check_release("1.2.3")
        opened.assert_called_once()
        fallback.assert_not_called()

    def test_http_errors_only_retry_transient_status(self):
        for status, count in [(503, 3), (429, 1), (403, 1), (404, 1), (401, 1)]:
            error = HTTPError(updates.API_URL, status, "error", {"Retry-After": "3600"}, None)
            with self.subTest(status=status), patch.object(updates, "_open", side_effect=error) as opened:
                with self.assertRaises(HTTPError):
                    updates._fetch_bytes(updates.API_URL, 100)
                self.assertEqual(opened.call_count, count)

    def test_truncated_metadata_read_is_retried(self):
        with patch.object(updates, "_open", side_effect=[Response(b"{", length=2), Response(b"{}")]) as opened:
            self.assertEqual(updates._fetch_bytes(updates.API_URL, 100), b"{}")
        self.assertEqual(opened.call_count, 2)

    def test_security_validation_errors_are_not_retried(self):
        with patch.object(updates, "_open", return_value=Response(b"too big")) as opened:
            with self.assertRaises(updates.UpdateError):
                updates._fetch_bytes(updates.API_URL, 1)
        opened.assert_called_once()

    def test_untrusted_api_metadata_does_not_switch_channel(self):
        with patch.object(updates, "_fetch_bytes", return_value=b'{"draft":true}'), patch.object(updates, "_page_release") as fallback:
            with self.assertRaises(updates.UpdateError):
                updates.check_release("1.2.3")
        fallback.assert_not_called()

    def test_network_errors_do_not_expose_proxy_credentials_or_signed_urls(self):
        for error in [URLError("http://user:password@127.0.0.1:7897"),
                      HTTPError("https://github.com/a?token=private", 500, "secret", {}, None)]:
            if isinstance(error, HTTPError):
                self.addCleanup(error.close)
            message = str(updates._network_error(error))
            for secret in ["password", "private", "secret", "7897"]:
                self.assertNotIn(secret, message)

    def test_direct_opener_explicitly_disables_proxy_handler(self):
        with patch.object(updates, "build_opener") as builder:
            builder.return_value.open.return_value = Response()
            updates._open(updates.API_URL, direct=True)
            handlers = builder.call_args.args
        self.assertEqual(len(handlers), 2)
        self.assertEqual(handlers[1].proxies, {})

    def info(self, size=6):
        tag = "v1.2.4"
        name = updates._ASSETS["windows"][0]
        return updates.ReleaseInfo("1.2.4", "1.2.3", True, f"{updates.RELEASE_ROOT}/tag/{tag}", name,
                                   updates._asset_url(tag, name), updates._asset_url(tag, "SHA256SUMS.txt"), size)

    def test_download_restarts_empty_after_truncation(self):
        info = self.info()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "archive.zip"
            def open_response(url):
                self.assertFalse(target.exists(), "partial archive must be removed before retry")
                if opened.call_count == 1:
                    return Response(b"ab", length=6)
                return Response(b"abcdef")
            with patch.object(updates, "_open", side_effect=open_response) as opened:
                digest = updates._download(info, target, None)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertEqual(digest, hashlib.sha256(b"abcdef").hexdigest())
            self.assertEqual(opened.call_count, 2)

    def test_three_truncated_downloads_leave_no_partial_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "archive.zip"
            with patch.object(updates, "_open", side_effect=lambda url: Response(b"ab", length=6)) as opened:
                with self.assertRaises(IncompleteRead):
                    updates._download(self.info(), target, None)
            self.assertFalse(target.exists())
            self.assertEqual(opened.call_count, 3)

    def test_download_never_deletes_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "archive.zip"
            target.write_bytes(b"keep")
            with patch.object(updates, "_open", return_value=Response(b"abcdef")) as opened:
                with self.assertRaises(FileExistsError):
                    updates._download(self.info(), target, None)
            self.assertEqual(target.read_bytes(), b"keep")
            opened.assert_called_once()

    def test_rate_limited_api_uses_validated_official_page_and_checksums(self):
        info = self.info()
        digest = hashlib.sha256(b"abcdef").hexdigest()
        def open_response(url, **kwargs):
            if url == updates.API_URL:
                raise HTTPError(url, 403, "rate limited", {}, None)
            if url.endswith("/latest"):
                return Response(url=info.url)
            if url == info.checksums_url:
                return Response(f"{digest}  {info.asset_name}\n".encode(), url)
            if url == info.asset_url:
                return Response(url=url, length=6)
            self.fail(f"unexpected URL: {url}")
        messages = []
        with patch.object(updates, "_open", side_effect=open_response):
            actual = updates.check_release("1.2.3", "windows", progress=messages.append)
        self.assertEqual(actual, replace(info, expected_digest=digest))
        self.assertTrue(any("官方发布页" in message for message in messages))

    def test_page_cannot_redirect_to_other_repo_or_nonstable_tag(self):
        for url in ["https://github.com/other/project/releases/tag/v1.2.4",
                    updates.RELEASE_ROOT + "/tag/v1.2.4-dev",
                    updates.RELEASE_ROOT + "/tag/v1.2.4?unexpected=1",
                    updates.RELEASE_ROOT + "/tag/v1.2.4/evil"]:
            with self.subTest(url=url), patch.object(updates, "_open", return_value=Response(url=url)), patch.object(updates, "_fetch_bytes") as fetch:
                with self.assertRaises(updates.UpdateError):
                    updates._page_release("1.2.3", "windows")
            fetch.assert_not_called()

    def test_page_does_not_offer_update_without_checksum(self):
        with patch.object(updates, "_open", return_value=Response(url=self.info().url)), patch.object(updates, "_fetch_bytes", return_value=b"invalid"):
            with self.assertRaisesRegex(updates.UpdateError, "校验值"):
                updates._page_release("1.2.3", "windows")

    def test_page_checks_no_downgrade_and_pins_tag_for_every_asset(self):
        info = self.info()
        digest = "a" * 64
        with patch.object(updates, "_open", side_effect=[Response(url=info.url), Response(length=6)]) as opened, patch.object(updates, "_fetch_bytes", return_value=f"{digest}  {info.asset_name}\n".encode()) as fetch:
            result = updates._page_release("1.2.5", "windows")
        self.assertFalse(result.available)
        self.assertEqual(fetch.call_args.args[0], info.checksums_url)
        self.assertEqual(opened.call_args.args[0], info.asset_url)


if __name__ == "__main__":
    unittest.main()
