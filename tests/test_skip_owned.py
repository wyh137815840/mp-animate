"""库里已有同集就不再提交的测试。用假会话，不连 115。"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
_pkg = types.ModuleType("anime115")
_pkg.__path__ = [str(_ROOT)]
_pkg.__package__ = "anime115"
sys.modules.setdefault("anime115", _pkg)

from anime115.anime_dmhy import SearchOptions, Torrent  # noqa: E402
from anime115.anime_flatten import Node  # noqa: E402
from anime115.anime_jobs import JobConfig, _OwnedIndex, run_daily  # noqa: E402
from anime115.anime_names import Show  # noqa: E402

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40


class FakeSession:
    """最小会话：只提供 run_daily 选片阶段用到的 list_dir/sleep。"""

    def __init__(self, dirs=None, fail=False):
        self.dirs = dirs or {}
        self.fail = fail
        self.cancel = None
        self.added = []

    def list_dir(self, path):
        if self.fail:
            raise RuntimeError("115 列目录失败")
        return list(self.dirs.get(path, []))

    def sleep(self, _seconds):
        return None

    def add_offline(self, magnet, fileid):
        self.added.append(magnet)
        return {"state": True, "code": 0, "data": []}

    def check_login(self):
        return True

    def quota(self):
        return {"data": {"surplus": 99}}

    def ensure_dir(self, path):
        return type("F", (), {"fileid": "1", "path": path})()


def _dir(path, name):
    return Node(path, name, True, 0, raw={"fc": 0})


def _file(path, name, size=900 * 1024 * 1024):
    return Node(path, name, False, size, raw={"pc": "pick"})


def torrent(title, digest=HASH):
    return Torrent(title, digest, "magnet:?xt=urn:btih:" + digest, 900 * 1024 * 1024, NOW)


class OwnedIndexTest(unittest.TestCase):
    def _library_session(self):
        return FakeSession({
            "/动漫": [_dir("/动漫/佐藤同学", "佐藤同学"), _dir("/动漫/我独自升级 (2025)", "我独自升级 (2025)")],
            "/动漫/佐藤同学": [_dir("/动漫/佐藤同学/Season 01", "Season 01")],
            "/动漫/佐藤同学/Season 01": [
                _file("/动漫/佐藤同学/Season 01/佐藤同学 - S01E03 - x.mkv.strm", "佐藤同学 - S01E03 - x.mkv.strm", 10),
                _file("/动漫/佐藤同学/Season 01/[喵] 佐藤同学 - 04.mkv", "[喵] 佐藤同学 - 04.mkv"),
            ],
            "/动漫/我独自升级 (2025)": [_dir("/动漫/我独自升级 (2025)/Season 01", "Season 01")],
            "/动漫/我独自升级 (2025)/Season 01": [
                _file("/动漫/我独自升级 (2025)/Season 01/我独自升级 - S01E01.mkv.strm", "我独自升级 - S01E01.mkv.strm", 10),
            ],
        })

    def test_detects_existing_and_missing_episode(self):
        owned = _OwnedIndex(self._library_session(), JobConfig(library_root="/动漫"))
        pick = types.SimpleNamespace(show="佐藤同学", season=1, episode=3)
        self.assertIs(owned.has(pick), True)
        pick4 = types.SimpleNamespace(show="佐藤同学", season=1, episode=4)
        self.assertIs(owned.has(pick4), True)
        pick5 = types.SimpleNamespace(show="佐藤同学", season=1, episode=5)
        self.assertIs(owned.has(pick5), False)

    def test_year_folder_is_reused(self):
        owned = _OwnedIndex(self._library_session(), JobConfig(library_root="/动漫"))
        pick = types.SimpleNamespace(show="我独自升级", season=1, episode=1)
        self.assertIs(owned.has(pick), True)

    def test_unknown_cases_fail_open(self):
        empty = _OwnedIndex(FakeSession({}), JobConfig(library_root=""))
        self.assertIsNone(empty.has(types.SimpleNamespace(show="佐藤同学", season=1, episode=3)))
        broken = _OwnedIndex(FakeSession(fail=True), JobConfig(library_root="/动漫"))
        self.assertIsNone(broken.has(types.SimpleNamespace(show="佐藤同学", season=1, episode=3)))
        no_folder = _OwnedIndex(
            FakeSession({"/动漫": []}), JobConfig(library_root="/动漫")
        )
        self.assertIsNone(no_folder.has(types.SimpleNamespace(show="佐藤同学", season=1, episode=3)))

    def test_mount_prefix_is_stripped(self):
        session = FakeSession({
            "/动漫": [_dir("/动漫/佐藤同学", "佐藤同学")],
            "/动漫/佐藤同学": [_dir("/动漫/佐藤同学/Season 01", "Season 01")],
            "/动漫/佐藤同学/Season 01": [_file("/动漫/佐藤同学/Season 01/x.mkv", "x.mkv")],
        })
        owned = _OwnedIndex(session, JobConfig(library_root="/115open/动漫", mount_prefix="/115open"))
        pick = types.SimpleNamespace(show="佐藤同学", season=1, episode=3)
        self.assertIs(owned.has(pick), False)


class DuplicateFolderTest(unittest.TestCase):
    def test_year_variant_folder_is_also_checked(self):
        session = FakeSession({
            "/动漫": [
                _dir("/动漫/神之塔", "神之塔"),
                _dir("/动漫/神之塔 -Tower of God (2020)", "神之塔 -Tower of God (2020)"),
            ],
            "/动漫/神之塔": [_dir("/动漫/神之塔/Season 02", "Season 02")],
            "/动漫/神之塔/Season 02": [
                _file("/动漫/神之塔/Season 02/Tower.of.God.S02E26.mkv", "Tower.of.God.S02E26.mkv"),
            ],
            "/动漫/神之塔 -Tower of God (2020)": [
                _dir("/动漫/神之塔 -Tower of God (2020)/Season 02", "Season 02")
            ],
            "/动漫/神之塔 -Tower of God (2020)/Season 02": [
                _file("/动漫/神之塔 -Tower of God (2020)/Season 02/S02E11 - x.mkv", "S02E11 - x.mkv"),
                _file("/动漫/神之塔 -Tower of God (2020)/Season 02/S02E22 - x.mkv", "S02E22 - x.mkv"),
            ],
        })
        owned = _OwnedIndex(session, JobConfig(library_root="/动漫"))
        self.assertIs(owned.has(types.SimpleNamespace(show="神之塔", season=2, episode=11)), True)
        self.assertIs(owned.has(types.SimpleNamespace(show="神之塔", season=2, episode=22)), True)
        self.assertIs(owned.has(types.SimpleNamespace(show="神之塔", season=2, episode=26)), True)
        self.assertIs(owned.has(types.SimpleNamespace(show="神之塔", season=2, episode=27)), False)


class RunDailySkipOwnedTest(unittest.TestCase):
    def _config(self, **kwargs):
        base = dict(
            dry_run=True,
            library_root="/动漫",
            max_submit_per_run=10,
            search=SearchOptions(min_episode_mb=20, max_episode_gb=8, require_sub=False),
        )
        base.update(kwargs)
        return JobConfig(**base)

    def _session(self):
        return FakeSession({
            "/动漫": [_dir("/动漫/佐藤同学", "佐藤同学")],
            "/动漫/佐藤同学": [_dir("/动漫/佐藤同学/Season 01", "Season 01")],
            "/动漫/佐藤同学/Season 01": [
                _file("/动漫/佐藤同学/Season 01/佐藤同学 - S01E03.mkv.strm", "佐藤同学 - S01E03.mkv.strm", 10),
            ],
        })

    def test_preview_skips_owned_episode(self):
        show = Show(keyword="佐藤同学")
        session = self._session()
        result = run_daily([show], session, lambda _show: [torrent("佐藤同学 - 03 [1080p][简体]")], {}, NOW, self._config())
        self.assertEqual(result["picks"], [])
        reasons = [note["reason"] for note in result["notes"]]
        self.assertIn("库里已有这一集", reasons)

    def test_preview_still_lists_missing_episode(self):
        show = Show(keyword="佐藤同学")
        session = self._session()
        other = torrent("佐藤同学 - 05 [1080p][简体]", "b" * 40)
        result = run_daily([show], session, lambda _show: [other], {}, NOW, self._config())
        self.assertEqual([item["episode"] for item in result["picks"]], [5])

    def test_switch_off_still_picks_owned_episode(self):
        show = Show(keyword="佐藤同学")
        session = self._session()
        result = run_daily(
            [show], session, lambda _show: [torrent("佐藤同学 - 03 [1080p][简体]")], {},
            NOW, self._config(skip_owned=False),
        )
        self.assertEqual([item["episode"] for item in result["picks"]], [3])

    def test_real_run_does_not_submit_or_remember_owned(self):
        show = Show(keyword="佐藤同学")
        session = self._session()
        seen = {}
        result = run_daily(
            [show], session, lambda _show: [torrent("佐藤同学 - 03 [1080p][简体]")], seen,
            NOW, self._config(dry_run=False),
        )
        self.assertEqual(result["submitted"], [])
        self.assertEqual(session.added, [])
        self.assertEqual(seen.get("hashes") or {}, {})
        self.assertEqual(seen.get("episodes") or {}, {})


if __name__ == "__main__":
    unittest.main()
