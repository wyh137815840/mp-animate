"""选片阶段：范围整包、整季包、标题末尾粘集数的识别。"""

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
sys.modules.setdefault("anime115", _pkg)

from anime115.anime_dmhy import (  # noqa: E402
    SearchOptions,
    Torrent,
    _head_episode,
    _is_season_pack,
    select_one,
)
from anime115.anime_names import parse_episode_range  # noqa: E402

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40


def torrent(title, digest=HASH, size=900 * 1024 * 1024):
    return Torrent(title, digest, "magnet:?xt=urn:btih:" + digest, size, NOW)


class RangeTest(unittest.TestCase):
    def test_range_followed_by_letters(self):
        self.assertEqual(parse_episode_range("[01-13TV全集][1080P][BDRip]"), (1, 13))
        self.assertEqual(parse_episode_range("[01-13TV全集+特典映像][1080P]"), (1, 13))
        self.assertEqual(parse_episode_range("[01-24 END][1080p]"), (1, 24))

    def test_not_a_range(self):
        for title in ("x264-10bit", "[2024-2025][1080p]", "Show - 05 [1080p]", "[S01E03]"):
            self.assertIsNone(parse_episode_range(title), title)


class HeadEpisodeTest(unittest.TestCase):
    def test_episode_glued_to_the_title(self):
        title = ('[搬运][Erai-raws]"你们先走我断后"于是10年后我成为了传说04'
                 '|ここは俺に任せて先に行けと言ってから 10年がたったら伝説になっていた04 繁')
        self.assertEqual(_head_episode(title), 4)

    def test_no_head_episode(self):
        for title in ("[Group] Show - 05 [1080p]", "[Group] Show [1080p][BDRip]", "Show 2024 [1080p]"):
            self.assertEqual(_head_episode(title), 0, title)


class SeasonPackTest(unittest.TestCase):
    def test_batch_like_titles(self):
        self.assertTrue(_is_season_pack("[H-Enc] 挣扎吧，亚当君 / Modaete yo, Adam-kun (BDRip 1080p HEVC FLAC)"))
        self.assertTrue(_is_season_pack("[DBD-Raws][堀与宫村][01-13TV全集][1080P][BDRip]"))

    def test_specials_are_not_season_packs(self):
        for title in (
            "[Group] Show OVA [1080p]",
            "[Group] Show SP01 [1080p]",
            "[Group] Show 剧场版 [1080p]",
            "[Group] Show 特典 [1080p]",
            "[Group] Show NCOP&NCED [1080p]",
        ):
            self.assertFalse(_is_season_pack(title), title)


class SelectPackTest(unittest.TestCase):
    def _show(self, name="佐藤同学"):
        from anime115.anime_names import parse_watchlist

        return parse_watchlist(name)[0]

    def test_range_pack_is_picked(self):
        show = self._show()
        options = SearchOptions(allow_batch=True, min_episode_mb=20, max_episode_gb=8)
        pack = torrent("佐藤同学 [01-12TV全集][1080p][简体]", "b" * 40)
        picks, notes = select_one(show, [pack], options, NOW)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].pack, (1, 12))
        self.assertEqual(notes, [])

    def test_season_pack_without_episode_is_picked(self):
        show = self._show()
        options = SearchOptions(allow_batch=True, min_episode_mb=20, max_episode_gb=8)
        pack = torrent("[H-Enc] 佐藤同学 / Satou-san (BDRip 1080p HEVC FLAC)", "c" * 40)
        picks, notes = select_one(show, [pack], options, NOW)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].pack, (0, 0))
        self.assertEqual(notes, [])

    def test_glued_episode_is_picked_as_single(self):
        show = self._show()
        options = SearchOptions(allow_batch=True, min_episode_mb=20, max_episode_gb=8)
        item = torrent("[搬运]佐藤同学 于是10年后04|伝説になっていた04 繁", "d" * 40)
        picks, notes = select_one(show, [item], options, NOW)
        self.assertEqual([pick.episode for pick in picks], [4])
        self.assertEqual(notes, [])

    def test_ova_is_still_dropped(self):
        show = self._show()
        options = SearchOptions(allow_batch=True, min_episode_mb=20, max_episode_gb=8)
        ova = torrent("佐藤同学 OVA [1080p]", "e" * 40)
        picks, notes = select_one(show, [ova], options, NOW)
        self.assertEqual(picks, [])
        self.assertEqual([note["reason"] for note in notes], ["集数不确定"])


if __name__ == "__main__":
    unittest.main()
