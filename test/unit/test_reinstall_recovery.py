import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs/airootfs/usr/share/omarchy-iso"))
from orchestrator import preserve_home as reinstall


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.top = Path(directory.name)
        self.workspace = self.top / reinstall.WORKSPACE
        self.workspace.mkdir(mode=0o700)
        self.pending = reinstall.PendingReinstall("fs", "efi", 259, 256, 264, 270, 271, True)
        (self.workspace / "state.json").write_text(json.dumps(asdict(self.pending)))
        self.plan = {"esp_partuuid": "efi", "home_id": 259, "root_id": 256}
        self.ids = {self.top / "@factory": 264, self.top / reinstall.PENDING_FACTORY: 271,
                    self.workspace / "root": 270}
        mocked = patch.object(reinstall, "_optional_id", side_effect=self.ids.get)
        mocked.start()
        self.addCleanup(mocked.stop)

    def read(self):
        return reinstall._pending(self.top, "fs", self.plan)

    def test_accepts_a_crash_on_either_side_of_the_root_exchange(self):
        self.assertEqual(self.read(), self.pending)
        self.ids[self.top / "@factory"] = 271
        self.ids[self.top / reinstall.PENDING_FACTORY] = 264
        self.assertEqual(self.read(), self.pending)
        self.plan["root_id"] = 270
        self.ids[self.workspace / "root"] = 256
        self.assertEqual(self.read(), self.pending)

    def test_rejects_an_unrelated_home_or_efi_partition(self):
        for field in ("home_id", "esp_partuuid"):
            with self.subTest(field=field), patch.dict(self.plan, {field: "different"}):
                with self.assertRaisesRegex(RuntimeError, "different filesystem, home or EFI"):
                    self.read()

    def test_keeps_unrecognized_staging_files(self):
        note = self.workspace / "user-file"
        note.touch()
        with self.assertRaisesRegex(RuntimeError, "Unexpected files"):
            self.read()
        self.assertTrue(note.exists())

    def test_refuses_a_replaced_root_or_snapshot(self):
        with patch.dict(self.plan, root_id=500), self.assertRaisesRegex(RuntimeError, "active root"):
            self.read()
        with patch.dict(self.ids, {self.top / "@factory": 501}), self.assertRaisesRegex(RuntimeError, "factory snapshot"):
            self.read()
        with patch.dict(self.ids, {self.workspace / "root": 502}), self.assertRaisesRegex(RuntimeError, "staged subvolume"):
            self.read()

    def test_old_workspace_without_a_record_is_never_deleted(self):
        (self.workspace / "state.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "Unrecognized reinstall"):
            self.read()
        self.assertTrue(self.workspace.is_dir())

    def test_refuses_an_incomplete_factory_pair_after_the_root_switch(self):
        self.plan["root_id"] = self.pending.new_root
        self.ids[self.workspace / "root"] = self.pending.old_root
        with self.assertRaisesRegex(RuntimeError, "factory snapshot"):
            self.read()

    def test_a_missing_original_factory_is_not_an_uncreated_new_snapshot(self):
        self.pending.new_factory = None
        (self.workspace / "state.json").write_text(json.dumps(asdict(self.pending)))
        self.ids.pop(self.top / "@factory")
        with self.assertRaisesRegex(RuntimeError, "factory snapshot"):
            self.read()
