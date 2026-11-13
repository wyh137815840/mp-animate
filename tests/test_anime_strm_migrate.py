"""历史 strm：外网入口从 443 换到 8443 的迁移。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
sys.path.insert(0, str(PLUGIN))

from anime_strm_migrate import migrate, normalize_url  # noqa: E402

OLD = "https://mp.yuanhot.top/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=abc&file_name="
NEW = "https://mp.yuanhot.top:8443/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=abc&file_name="


class NormalizeUrlTest(unittest.TestCase):
    def test_443_is_rewritten_to_8443(self):
        old = OLD + "%5BLoliHouse%5D%20Show%20-%2001.mkv"
        new = normalize_url(old)
        self.assertTrue(new.startswith(NEW))
        self.assertTrue(new.endswith("%5BLoliHouse%5D%20Show%20-%2001.mkv"))

    def test_already_8443_is_untouched(self):
        old = NEW + "a.mkv"
        self.assertEqual(normalize_url(old), old)

    def test_lan_url_is_untouched(self):
        old = "http://192.168.1.9:3000/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=abc&file_name=a.mkv"
        self.assertEqual(normalize_url(old), old)

    def test_non_url_line_is_untouched(self):
        self.assertEqual(normalize_url("not a url"), "not a url")


class MigrateTest(unittest.TestCase):
    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "a.strm"
            path.write_text(OLD + "a.mkv\n", encoding="utf-8")
            out = migrate(root, dry_run=True)
            self.assertEqual(out["total"], 1)
            self.assertEqual(out["changed"], 1)
            self.assertIn("mp.yuanhot.top/api", path.read_text(encoding="utf-8"))

    def test_only_changed_files_are_rewritten(self):
        with tempfile.TemporaryDirectory() as root:
            changed = Path(root) / "a" / "a.strm"
            kept = Path(root) / "b" / "b.strm"
            changed.parent.mkdir()
            kept.parent.mkdir()
            changed.write_text(OLD + "a.mkv\n", encoding="utf-8")
            kept.write_text(NEW + "b.mkv\n", encoding="utf-8")
            out = migrate(root)
            self.assertEqual(out["total"], 2)
            self.assertEqual(out["changed"], 1)
            self.assertTrue(changed.read_text(encoding="utf-8").startswith(NEW))
            self.assertTrue(kept.read_text(encoding="utf-8").startswith(NEW))


if __name__ == "__main__":
    unittest.main()
