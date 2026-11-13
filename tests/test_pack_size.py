"""蓝光大包的体积判断：整包按单集算，不再因为整体大就被丢掉。"""

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
    _is_whole_pack,
    _pack_episode_count,
    _size_gate,
    score_torrent,
)
from anime115.anime_names import parse_watchlist  # noqa: E402

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40
GB = 1024 ** 3
MB = 1024 ** 2


def torrent(title: str, size: int, infohash: str = HASH) -> Torrent:
    return Torrent(title, infohash, f"magnet:?xt=urn:btih:{infohash}", size, NOW)


class PackCountTest(unittest.TestCase):
    def test_range_gives_count(self):
        self.assertEqual(_pack_episode_count("某剧 01-12 [1080p]"), 12)
        self.assertEqual(_pack_episode_count("某剧 01-12TV全集"), 12)

    def test_no_range_gives_zero(self):
        self.assertEqual(_pack_episode_count("某剧 第05话 [1080p]"), 0)

    def test_whole_pack_words(self):
        self.assertTrue(_is_whole_pack("某剧 全集 [BD-BOX][1080p]"))
        self.assertTrue(_is_whole_pack("某剧 完结合集"))
        self.assertFalse(_is_whole_pack("某剧 第05话 [1080p]"))
        # 剧场版合集不是正片整包
        self.assertFalse(_is_whole_pack("某剧 剧场版合集"))


class BigPackTest(unittest.TestCase):
    """标题里没集数、只有体积大得离谱的蓝光合集，当成整季包收下。"""

    def test_huge_pack_without_episode_is_accepted(self):
        options = SearchOptions(min_episode_mb=150, max_episode_gb=20, max_pack_gb=400)
        self.assertEqual(_size_gate("[喵萌]某剧 / Some Show [BDRip][1080p][简体]", 112 * GB, options), "")

    def test_huge_special_still_rejected(self):
        options = SearchOptions(min_episode_mb=150, max_episode_gb=20, max_pack_gb=400)
        self.assertEqual(
            _size_gate("[喵萌]某剧 剧场版 合集 [BDRip]", 112 * GB, options),
            "体积过大",
        )

    def test_huge_pack_over_total_cap_rejected(self):
        options = SearchOptions(min_episode_mb=150, max_episode_gb=20, max_pack_gb=400)
        self.assertEqual(
            _size_gate("[喵萌]某剧 / Some Show [BDRip][1080p][简体]", 500 * GB, options),
            "整包体积过大",
        )


class SubtitleScoreTest(unittest.TestCase):
    """外挂字幕优先：外挂能单独挂到 Emby 旁边，内封的做不到。"""

    def _score(self, title):
        from anime115.anime_dmhy import score_torrent
        from anime115.anime_names import parse_watchlist

        show = parse_watchlist("杜鹃的婚约")[0]
        options = SearchOptions(min_episode_mb=150, max_episode_gb=20, max_pack_gb=400, allow_batch=True)
        torrent = Torrent(title, "a" * 40, "magnet:?xt=urn:btih:" + "a" * 40, 10 * GB, NOW)
        return score_torrent(show, torrent, options, NOW)[0]

    def test_external_sub_wins_over_embedded(self):
        embedded = self._score("【喵萌奶茶屋】[杜鹃的婚约][01-24][BDRip][1080p][简体][招募翻译]")
        external = self._score("【喵萌奶茶屋】[杜鹃的婚约][01-24][BDRip][1080p][简繁外挂][招募翻译]")
        self.assertIsNotNone(external)
        self.assertGreater(external, embedded)


class SizeGateTest(unittest.TestCase):
    def test_bluray_pack_accepted(self):
        """30G 的 12 集蓝光合集，每集 2.5G，应该收下。"""
        options = SearchOptions(min_episode_mb=150, max_episode_gb=8, max_pack_gb=120)
        self.assertEqual(_size_gate("某剧 01-12 [BDrip][1080p]", 30 * GB, options), "")

    def test_single_episode_still_limited(self):
        """单集还是按单集的体积算，超过上限就丢。"""
        options = SearchOptions(min_episode_mb=150, max_episode_gb=8, max_pack_gb=120)
        self.assertEqual(_size_gate("某剧 - 05 [1080p]", 20 * GB, options), "体积过大")

    def test_pack_over_total_limit_dropped(self):
        """整包超过整包上限就丢。"""
        options = SearchOptions(min_episode_mb=150, max_episode_gb=8, max_pack_gb=50)
        self.assertEqual(_size_gate("某剧 01-12 [BDrip][1080p]", 60 * GB, options), "整包体积过大")

    def test_pack_per_episode_too_big_dropped(self):
        """每集超过单集上限的整包也要丢。"""
        options = SearchOptions(min_episode_mb=150, max_episode_gb=8, max_pack_gb=200)
        self.assertEqual(_size_gate("某剧 01-04 [BDrip][1080p]", 40 * GB, options), "单集体积过大")

    def test_whole_pack_only_checks_total(self):
        """集数不明的整包只看整体上限。"""
        options = SearchOptions(min_episode_mb=150, max_episode_gb=8, max_pack_gb=120)
        self.assertEqual(_size_gate("某剧 全集 [BD-BOX][1080p]", 100 * GB, options), "")
        small = SearchOptions(min_episode_mb=150, max_episode_gb=8, max_pack_gb=50)
        self.assertEqual(_size_gate("某剧 全集 [BD-BOX][1080p]", 60 * GB, small), "整包体积过大")

    def test_score_torrent_keeps_bluray_pack(self):
        show = parse_watchlist("某剧")[0]
        options = SearchOptions(allow_batch=True, min_episode_mb=150, max_episode_gb=8, max_pack_gb=120)
        pack = torrent("某剧 01-12 [BDrip][1080p][简体]", 30 * GB)
        score, reason = score_torrent(show, pack, options, NOW)
        self.assertIsNotNone(score)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
