"""Persistent list metadata tests; no SSH, background threads or live mappings."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from jumper_manager.engine import Manager
from support import temp_directory


def payload(name="example", **changes):
    value = {"name": name, "source_host": "local", "target_host": "local",
             "source_port": 51051, "target_port": 50051, "auto_start": False}
    value.update(changes)
    return value


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(temp_directory()))
        self.manager = self.make_manager()
        self.path = self.manager.data / "mappings.json"

    def make_manager(self):
        # Exercise the real metadata implementation without Manager's SSH
        # discovery, crash recovery or background workers.
        manager = object.__new__(Manager)
        manager.data = self.root / "data"
        manager.data.mkdir(exist_ok=True)
        manager.config_path = self.root / "ssh-config"
        manager._lock = threading.RLock()
        manager._closed = threading.Event()
        manager._mappings = {}
        manager._mapping_locks = {}
        manager._processes = {}
        manager._relays = {}
        manager._hosts = [{"id": "local", "label": "本机", "route": ["local"]}]
        manager._discovery = {}
        manager._recovery_warnings = []
        return manager

    def create(self, name, **changes):
        return self.manager.create(payload(name, **changes))["id"]

    def ids(self, manager=None):
        return [mapping["id"] for mapping in (manager or self.manager).state()["mappings"]]

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["mappings"]

    def test_legacy_array_order_and_unpinned_default_survive_reload(self):
        entries = [{"id": character * 32, **payload(character)} for character in ("c", "a", "b")]
        self.path.write_text(json.dumps({"version": "1.0", "mappings": entries}), encoding="utf-8")
        original = self.path.read_bytes()
        self.manager._load()
        self.assertEqual(self.ids(), [entry["id"] for entry in entries])
        self.assertTrue(all(mapping["pinned"] is False for mapping in self.manager.state()["mappings"]))
        self.assertTrue(all(mapping["usage"] is None for mapping in self.manager.state()["mappings"]))
        self.assertEqual(self.path.read_bytes(), original, "Reading a legacy file must not rewrite it")
        self.manager._save()
        self.assertTrue(all(mapping["pinned"] is False for mapping in self.saved()))

    def test_pin_and_unpin_move_to_front_of_their_groups_and_persist(self):
        first, second, third = [self.create(name) for name in ("first", "second", "third")]
        self.manager.pin(second, True)
        self.assertEqual(self.ids(), [second, first, third])
        self.manager.pin(third, True)
        self.assertEqual(self.ids(), [third, second, first])
        self.manager.pin(third, False)
        self.assertEqual(self.ids(), [second, third, first])
        reloaded = self.make_manager()
        reloaded._load()
        self.assertEqual(self.ids(reloaded), [second, third, first])
        self.assertEqual([mapping["pinned"] for mapping in reloaded.state()["mappings"]], [True, False, False])

    def test_reorder_keeps_pinned_group_first_and_preserves_mapping_objects(self):
        first, second, third, fourth = [self.create(name) for name in ("first", "second", "third", "fourth")]
        self.manager.pin(first, True)
        self.manager.pin(third, True)
        references = self.manager._mappings.copy()
        references[first]["status"] = "running"
        references[first]["health"] = {"tunnel_ok": True, "target_ok": False}
        processes = [{"marker": "existing-owned-process"}]
        self.manager._processes[first] = processes
        with patch.object(self.manager, "start") as start, patch.object(self.manager, "stop") as stop:
            result = self.manager.reorder([fourth, first, second, third])
            self.manager.pin(second, True)
            start.assert_not_called()
            stop.assert_not_called()
        self.assertEqual([mapping["id"] for mapping in result], [first, third, fourth, second])
        self.assertEqual(self.ids(), [second, first, third, fourth])
        for mapping_id, mapping in references.items():
            self.assertIs(self.manager._mappings[mapping_id], mapping)
        self.assertIs(self.manager._processes[first], processes)
        self.assertEqual(references[first]["status"], "running")
        self.assertFalse(references[first]["health"]["target_ok"])
        self.assertEqual([mapping["id"] for mapping in self.saved()], self.ids())
        self.assertNotIn("health", self.saved()[1])

    def test_reorder_rejects_duplicate_missing_unknown_and_stale_ids_without_changes(self):
        first, second = [self.create(name) for name in ("first", "second")]
        before = self.path.read_bytes()
        for mapping_ids, error in (([first, first], ValueError), ([first], RuntimeError),
                                   ([first, "f" * 32], RuntimeError), ([], RuntimeError),
                                   ([second, first, "f" * 32], RuntimeError)):
            with self.subTest(mapping_ids=mapping_ids), self.assertRaises(error):
                self.manager.reorder(mapping_ids)
            self.assertEqual(self.ids(), [first, second])
            self.assertEqual(self.path.read_bytes(), before)
        third = self.create("third")
        with self.assertRaises(RuntimeError):
            self.manager.reorder([second, first])
        self.assertEqual(self.ids(), [first, second, third])

    def test_empty_project_can_persist_an_empty_order(self):
        self.assertEqual(self.manager.reorder([]), [])
        self.assertEqual(self.saved(), [])

    def test_metadata_types_are_strict(self):
        mapping_id = self.create("first")
        for value in (None, 1, 0, "true", [], {}):
            with self.subTest(pinned=value), self.assertRaises(ValueError):
                self.manager.pin(mapping_id, value)
            with self.subTest(create_pinned=value), self.assertRaises(ValueError):
                self.manager.create(payload(pinned=value))
        for value in (None, True, mapping_id, {}, [1], [False], [[mapping_id]], [""]):
            with self.subTest(mapping_ids=value), self.assertRaises(ValueError):
                self.manager.reorder(value)
        with self.assertRaises(KeyError):
            self.manager.pin("f" * 32, True)

    def test_pin_storage_failure_rolls_back_flag_order_and_identity(self):
        first, second = [self.create(name) for name in ("first", "second")]
        before = self.path.read_bytes()
        dictionary = self.manager._mappings
        reference = dictionary[second]
        with patch.object(self.manager, "_save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.manager.pin(second, True)
        self.assertIs(self.manager._mappings, dictionary)
        self.assertIs(self.manager._mappings[second], reference)
        self.assertIs(reference["pinned"], False)
        self.assertEqual(self.ids(), [first, second])
        self.assertEqual(self.path.read_bytes(), before)

    def test_reorder_storage_failure_restores_previous_dictionary(self):
        first, second = [self.create(name) for name in ("first", "second")]
        before = self.path.read_bytes()
        dictionary = self.manager._mappings
        with patch.object(self.manager, "_save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.manager.reorder([second, first])
        self.assertIs(self.manager._mappings, dictionary)
        self.assertEqual(self.ids(), [first, second])
        self.assertEqual(self.path.read_bytes(), before)

    def test_edit_preserves_pinned_flag_and_position_even_when_form_omits_it(self):
        first, second, third = [self.create(name) for name in ("first", "second", "third")]
        self.manager.pin(second, True)
        self.manager.pin(first, True)
        for changes in ({}, {"pinned": False}):
            updated = self.manager.update(second, payload("renamed", target_port=50052, **changes))
            self.assertIs(updated["pinned"], True)
            self.assertEqual(updated["target_port"], 50052)
            self.assertEqual(self.ids(), [first, second, third])
        reloaded = self.make_manager()
        reloaded._load()
        self.assertEqual(self.ids(reloaded), [first, second, third])
        self.assertTrue(reloaded._mappings[second]["pinned"])

    def test_failed_create_update_and_delete_preserve_metadata_order(self):
        first, second, third = [self.create(name) for name in ("first", "second", "third")]
        self.manager.pin(second, True)
        original_order = [second, first, third]
        original = self.path.read_bytes()
        reference = self.manager._mappings[first]
        with patch.object(self.manager, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.manager.create(payload("new"))
            self.assertEqual(set(self.manager._mapping_locks), set(original_order))
            with self.assertRaises(OSError):
                self.manager.update(first, payload("renamed"))
            self.assertIs(self.manager._mappings[first], reference)
            with patch.object(self.manager, "stop"), self.assertRaises(OSError):
                self.manager.delete(first)
        self.assertEqual(self.ids(), original_order)
        self.assertIs(self.manager._mappings[first], reference)
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_alias_records_keep_pinned_and_legacy_defaults(self):
        entries = [{"id": "a" * 32, **payload("removed", target_host="missing")},
                   {"id": "b" * 32, **payload("pinned removed", target_host="missing", pinned=True)}]
        self.path.write_text(json.dumps({"version": "1.0", "mappings": entries}), encoding="utf-8")
        self.manager._load()
        mappings = self.manager.state()["mappings"]
        self.assertEqual([mapping["id"] for mapping in mappings], ["b" * 32, "a" * 32])
        self.assertEqual([mapping["pinned"] for mapping in mappings], [True, False])
        self.assertTrue(all(mapping["status"] == "error" for mapping in mappings))
        self.assertTrue(all(mapping["usage"] is None for mapping in mappings))
        self.manager._save()
        self.assertEqual([mapping["pinned"] for mapping in self.saved()], [True, False])

    def test_corrupt_pinned_metadata_is_not_silently_rewritten(self):
        content = json.dumps({"version": "1.0", "mappings": [{"id": "a" * 32, **payload(pinned="false")}]})
        self.path.write_text(content, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "pinned"):
            self.manager._load()
        self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_closed_manager_rejects_metadata_changes(self):
        mapping_id = self.create("first")
        before = self.path.read_bytes()
        self.manager._closed.set()
        with self.assertRaises(RuntimeError):
            self.manager.pin(mapping_id, True)
        with self.assertRaises(RuntimeError):
            self.manager.reorder([mapping_id])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
