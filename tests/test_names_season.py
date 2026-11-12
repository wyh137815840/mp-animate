"""Anime115 多季解析与整包范围的纯规则测试。不连动漫花园，也不连 115。"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
_pkg = types.ModuleType("anime115")
_pkg.__path__ = [str(_ROOT)]
_pkg.__package__ = "anime115"
sys.modules["anime115"] = _pkg

from anime115.anime_dmhy import (  # noqa: E402
    SearchOptions,
    Torrent,
    _BATCH,
    _is_batch,
    score_torrent,
    select_one,
)
from anime115.anime_names import parse_episode, parse_watchlist  # noqa: E402


NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40


def torrent(title: str, infohash: str = HASH) -> Torrent:
    return Torrent(title, infohash, f"magnet:?xt=urn:btih:{infohash}", 900 * 1024 * 1024, NOW)


class SeasonParseTest(unittest.TestCase):
    def test_period_is_season(self):
        self.assertEqual(parse_episode("[Group] Show 第2期 - 05 [1080p].mkv"), (2, 5))
        self.assertEqual(parse_episode("Show 第2期 - 05 [1080p]"), (2, 5))
        self.assertEqual(parse_episode("Show 2期 - 05.mkv"), (2, 5))
        self.assertEqual(parse_episode("Show 第3期 - 07 [1080p]"), (3, 7))

    def test_season_words(self):
        self.assertEqual(parse_episode("Show 第二季 - 05.mkv"), (2, 5))
        self.assertEqual(parse_episode("Show 第2季 - 05 [1080p]"), (2, 5))
        self.assertEqual(parse_episode("Show 第 2 季 - 05 [1080p]"), (2, 5))
        self.assertEqual(parse_episode("Show 第三季 - 07 [1080p]"), (3, 7))

    def test_english_season_words(self):
        self.assertEqual(parse_episode("[Group] Show Season 2 - 05.mkv"), (2, 5))
        self.assertEqual(parse_episode("Show Season 02 - 05 [1080p]"), (2, 5))
        self.assertEqual(parse_episode("Show Season 3 - 07 [1080p]"), (3, 7))
        self.assertEqual(parse_episode("Show 2nd Season - 05.mkv"), (2, 5))
        self.assertEqual(parse_episode("Show Season 1 - 05 [1080p]"), (1, 5))

    def test_short_season_prefix(self):
        self.assertEqual(parse_episode("Show S2 - 05.mkv"), (2, 5))
        self.assertEqual(parse_episode("Show S02 - 05 [1080p]"), (2, 5))
        self.assertEqual(parse_episode("Show S2 - 05 [1080p][5.1]"), (2, 5))
        # 现有的 SxxExx 行为不能动。
        self.assertEqual(parse_episode("Show S02E05.mkv"), (2, 5))
        self.assertEqual(parse_episode("Show S01E03"), (1, 3))

    def test_existing_episode_forms_unchanged(self):
        self.assertEqual(parse_episode("Show 第3话"), (1, 3))
        self.assertEqual(parse_episode("Show 第3集 [1080p]"), (1, 3))
        self.assertEqual(parse_episode("冷淡的佐藤同学只对我撒娇 - 03 [1080p]"), (1, 3))
        self.assertEqual(parse_episode("Show - 05v2 [1080p]"), (1, 5))
        self.assertEqual(parse_episode("Show [05][1080p]"), (1, 5))

    def test_does_not_misread_resolution_version_audio(self):
        self.assertIsNone(parse_episode("Show [1080P][8bit]"))
        self.assertEqual(parse_episode("Show - 05 [1080p]"), (1, 5))
        self.assertEqual(parse_episode("Show - 05 [1080p][x264-10bit]"), (1, 5))
        self.assertEqual(parse_episode("Show - 05 [1080p][5.1ch]"), (1, 5))
        self.assertEqual(parse_episode("Show 5.1 - 05 [1080p]"), (1, 5))
        # 2nd 只有跟 Season 才算季；单独出现不当成季。
        self.assertEqual(parse_episode("Show 2nd - 05 [1080p]"), (1, 5))

    def test_dates_are_not_episode_ranges(self):
        self.assertEqual(parse_episode("Show 2024-2025 - 05 [1080p]"), (1, 5))
        self.assertEqual(parse_episode("Show (2024-2025) - 05 [1080p]"), (1, 5))
        self.assertEqual(parse_episode("Show 2024.01.15 - 05 [1080p]"), (1, 5))

    def test_multiple_seasons_still_unknown(self):
        self.assertIsNone(parse_episode("Show S01E03 S02E05"))
        self.assertIsNone(parse_episode("Show 第1季 第2季 - 05"))
        self.assertIsNone(parse_episode("Show OVA"))


class EpisodeRangeTest(unittest.TestCase):
    def test_range_pack_returns_none(self):
        # 整包范围不能报成最后一集，应交给上层当「集数不确定」。
        self.assertIsNone(parse_episode("Show 01-12 [1080p]"))
        self.assertIsNone(parse_episode("Show 03-04"))
        self.assertIsNone(parse_episode("Show 01~12 [1080p]"))
        self.assertIsNone(parse_episode("Show EP01-12 [1080p]"))
        self.assertIsNone(parse_episode("Show [01-12] [1080p]"))
        self.assertIsNone(parse_episode("Show 第2期 01-12 [1080p]"))
        self.assertIsNone(parse_episode("Show 05 - 06 [1080p]"))
        self.assertIsNone(parse_episode("Show - 03 - 04"))

    def test_single_episode_still_parses(self):
        self.assertEqual(parse_episode("Show - 05 [1080p]"), (1, 5))
        self.assertEqual(parse_episode("Show - 05.mkv"), (1, 5))
        self.assertEqual(parse_episode("Show 第2期 - 05 [1080p].mkv"), (2, 5))
        self.assertEqual(parse_episode("Show S2 - 05.mkv"), (2, 5))
        # 数字属于剧名编号时不是范围，单集照常解析。
        self.assertEqual(parse_episode("Show001 - 02"), (1, 2))
        self.assertEqual(parse_episode("Show 86 - 01 [1080p]"), (1, 1))


class BatchRangeTest(unittest.TestCase):
    def test_batch_regex_recognizes_range_packs(self):
        for title in (
            "Show 01-12 [1080p]",
            "Show 03-04",
            "Show 01~12 [1080p]",
            "Show 全12集 [1080p]",
            "Show 12集全 [1080p]",
            "Show 全12話 [1080p]",
            "Show 12-13合集 [1080p]",
        ):
            with self.subTest(title=title):
                self.assertIsNotNone(_BATCH.search(title))

    def test_batch_regex_ignores_clearly_single_episode(self):
        for title in (
            "Show - 05 [1080p][简体]",
            "Show - 05 [1080p][x264-10bit]",
            "Show - 05 [1080p][5.1ch]",
            "Show [1080p][8bit]",
            "Show S02E05 [1080p]",
            "Show 2024-2025 [1080p]",
            "Show 2024-01-15 [1080p]",
            "Show 5.1ch [1080p]",
        ):
            with self.subTest(title=title):
                self.assertIsNone(_BATCH.search(title))

    def test_is_batch_requires_ascending_range(self):
        # 纯正则看不出大小，范围还要数字判断：86 - 01 是单集，01-12 才是合集。
        self.assertTrue(_is_batch("Show 01-12 [1080p]"))
        self.assertTrue(_is_batch("Show 03-04"))
        self.assertTrue(_is_batch("Show EP01-12 [1080p]"))
        self.assertTrue(_is_batch("Show 全12集 [1080p]"))
        self.assertFalse(_is_batch("Show 86 - 01 [1080p][简体]"))
        self.assertFalse(_is_batch("Show - 05 [1080p][简体]"))
        self.assertFalse(_is_batch("Show 2024-2025 [1080p]"))

    def test_numeric_show_name_is_not_batch(self):
        # 剧名就是数字时（如「86」），86 - 01 不能被当成合集丢掉。
        show = parse_watchlist("86")[0]
        single = torrent("86 - 01 [1080p][简体]")
        score, reason = score_torrent(show, single, SearchOptions(allow_batch=False), NOW)
        self.assertIsNotNone(score)
        self.assertEqual(reason, "")
        picks, notes = select_one(show, [single], SearchOptions(allow_batch=False), NOW)
        self.assertEqual([(pick.season, pick.episode) for pick in picks], [(1, 1)])
        self.assertEqual(notes, [])

    def test_score_drops_range_when_batch_disallowed(self):
        show = parse_watchlist("佐藤同学 | group=喵萌")[0]
        pack = torrent("佐藤同学 01-12 [1080p][简体]")
        options = SearchOptions(allow_batch=False)
        score, reason = score_torrent(show, pack, options, NOW)
        self.assertIsNone(score)
        self.assertEqual(reason, "合集")
        allowed = SearchOptions(allow_batch=True)
        score, reason = score_torrent(show, pack, allowed, NOW)
        self.assertIsNotNone(score)
        self.assertEqual(reason, "")

    def test_select_one_takes_range_pack_when_allowed(self):
        """允许合集时整包也能选：整包按范围收下，单集照旧。"""
        show = parse_watchlist("佐藤同学 | group=喵萌")[0]
        pack = torrent("佐藤同学 01-12 [1080p][简体]", "b" * 40)
        single = torrent("佐藤同学 - 05 [1080p][简体]", "c" * 40)
        allowed = SearchOptions(allow_batch=True)
        picks, notes = select_one(show, [pack, single], allowed, NOW)
        self.assertEqual(sorted((pick.season, pick.episode) for pick in picks), [(1, 1), (1, 5)])
        pack_picks = [pick for pick in picks if pick.pack]
        self.assertEqual(len(pack_picks), 1)
        self.assertEqual(pack_picks[0].pack, (1, 12))
        self.assertEqual(notes, [])

    def test_select_one_drops_range_pack_by_default(self):
        show = parse_watchlist("佐藤同学 | group=喵萌")[0]
        pack = torrent("佐藤同学 01-12 [1080p][简体]")
        picks, notes = select_one(show, [pack], SearchOptions(allow_batch=False), NOW)
        self.assertEqual(picks, [])
        self.assertEqual(notes, [{"show": "佐藤同学", "title": pack.title, "reason": "合集"}])


if __name__ == "__main__":
    unittest.main()
