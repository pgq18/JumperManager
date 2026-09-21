"""Adaptive SSH discovery, Include paths, and configuration change signatures."""
from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from support import temp_directory
from jumper_manager.ssh_config import (
    WILDCARD_HOST_EXPLANATION, _argument_tokens, _directive,
    configuration_signature, discover_aliases,
)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = temp_directory()
        self.root = Path(self.directory.__enter__())
        self.ssh = self.root / ".ssh"
        self.ssh.mkdir()
        self.config = self.ssh / "config"
        self.home = patch("pathlib.Path.home", return_value=self.root)
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.directory.__exit__(None, None, None)

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_multiple_aliases_comments_and_equal_variants(self):
        self.write(self.config, '\ufeffHost alpha beta *.corp !excluded\nHost=gamma\nHost = delta\nHost =epsilon\nHost= zeta\nHost "eta" theta # ignored\nHost alpha local\n')
        self.assertEqual(discover_aliases(self.config), ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"])
        self.assertIn("通配符", WILDCARD_HOST_EXPLANATION)

    def test_windows_backslash_tokens_and_quoted_spaces(self):
        self.assertEqual(_argument_tokens(r'C:\Users\me\.ssh\hosts.conf'), [r'C:\Users\me\.ssh\hosts.conf'])
        self.assertEqual(_argument_tokens(r'"C:\My Documents\ssh\hosts.conf" other.conf'), [r'C:\My Documents\ssh\hosts.conf', "other.conf"])
        self.assertEqual(_argument_tokens(r'"\\server\share\ssh configs\hosts.conf"'), [r'\\server\share\ssh configs\hosts.conf'])
        self.assertEqual(_directive('Include = "C:\\My Documents\\ssh\\*.conf"'), ("include", [r'C:\My Documents\ssh\*.conf']))

    def test_real_absolute_include_paths_with_spaces(self):
        child = self.root / "folder with spaces" / "devices.conf"
        self.write(child, "Host new-machine\n")
        self.write(self.config, f'Include "{child}"\nHost direct\n')
        self.assertEqual(discover_aliases(self.config), ["new-machine", "direct"])

    def test_nested_includes_relative_to_user_ssh_not_parent(self):
        self.write(self.ssh / "fragments" / "a.conf", 'Host alpha\nInclude "more hosts.conf"\n')
        self.write(self.ssh / "more hosts.conf", "Host beta\n")
        self.write(self.config, "Include fragments/*.conf\n")
        self.assertEqual(discover_aliases(self.config), ["alpha", "beta"])

    def test_glob_addition_and_removal_change_signature(self):
        self.write(self.config, "Include devices/*.conf\nHost direct\n")
        initial = configuration_signature(self.config)
        self.write(self.ssh / "devices" / "a.conf", "Host alpha\n")
        added = configuration_signature(self.config)
        self.assertNotEqual(initial, added)
        self.assertEqual(discover_aliases(self.config), ["alpha", "direct"])
        (self.ssh / "devices" / "a.conf").unlink()
        self.assertEqual(configuration_signature(self.config), initial)

    def test_content_changes_detected_even_with_same_size_and_mtime(self):
        self.write(self.config, "Include child.conf\n")
        child = self.ssh / "child.conf"
        self.write(child, "Host alpha\n")
        before = configuration_signature(self.config)
        stat = child.stat()
        self.write(child, "Host bravo\n")
        os.utime(child, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(before, configuration_signature(self.config))
        self.assertEqual(discover_aliases(self.config), ["bravo"])

    def test_missing_root_appears_and_is_removed(self):
        missing = configuration_signature(self.config)
        self.assertEqual(discover_aliases(self.config), [])
        self.write(self.config, "Host appeared\n")
        self.assertNotEqual(missing, configuration_signature(self.config))
        self.config.unlink()
        self.assertEqual(missing, configuration_signature(self.config))

    def test_cycles_are_bounded_and_signature_stable(self):
        self.write(self.config, "Host root\nInclude child.conf\n")
        self.write(self.ssh / "child.conf", "Host nested\nInclude config\n")
        self.assertEqual(discover_aliases(self.config), ["root", "nested"])
        signature = configuration_signature(self.config)
        self.assertEqual(signature, configuration_signature(self.config))
        self.assertTrue(any(record[0] == "already-visited" for record in signature))

    def test_multiple_include_patterns_sorted_and_deduplicated(self):
        self.write(self.ssh / "a.conf", "Host alpha\n")
        self.write(self.ssh / "b.conf", "Host beta alpha\n")
        self.write(self.config, "Include b.conf *.conf absent-*.conf\n")
        self.assertEqual(discover_aliases(self.config), ["beta", "alpha"])
        self.assertTrue(any(record[0] == "include" and not record[-1] for record in configuration_signature(self.config)))

    def test_invalid_quoted_line_does_not_hide_valid_later_aliases(self):
        self.write(self.config, 'Include "unfinished\nHost valid\n')
        self.assertEqual(discover_aliases(self.config), ["valid"])


if __name__ == "__main__":
    unittest.main()
