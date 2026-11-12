"""标题比对与繁简折叠的回归测试。

覆盖两个真实缺陷：
1. 短剧名是长剧名的前缀时会误收别的剧（「死神」对上「死神少爷与黑女仆」）。
2. 繁简折叠表只覆盖 120 对字，常见繁体发布名折不过来，整部剧搜不到。
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
_pkg = types.ModuleType("anime115")
_pkg.__path__ = [str(_ROOT)]
_pkg.__package__ = "anime115"
sys.modules.setdefault("anime115", _pkg)

from anime115.anime_names import Show, _fold_name, title_matches  # noqa: E402


class TraditionalFoldingTest(unittest.TestCase):
    """内置字表要覆盖常见繁体字。"""

    def test_common_traditional_titles_fold_to_simplified(self):
        cases = {
            "碧藍之海": "碧蓝之海",
            "葬送的芙莉蓮": "葬送的芙莉莲",
            "夢幻遊戲": "梦幻游戏",
            "少爺與黑女僕": "少爷与黑女仆",
            "魔法★探險家": "魔法探险家",  # 星号属于符号，比对时被忽略
            "戰鬥": "战斗",
            "選擇": "选择",
            "專業": "专业",
            "導演": "导演",
            "實驗": "实验",
            "終於": "终于",
            "萬物": "万物",
            "優雅": "优雅",
            "盡情": "尽情",
            "別說": "别说",
            "寶物": "宝物",
            "惡魔": "恶魔",
            "殺戮": "杀戮",
        }
        for traditional, simplified in cases.items():
            self.assertEqual(_fold_name(traditional), simplified, traditional)

    def test_traditional_release_title_matches_simplified_show(self):
        self.assertTrue(title_matches("[喵萌奶茶屋] 碧藍之海 - 03 [1080p][简体]", Show(keyword="碧蓝之海")))
        self.assertTrue(title_matches("[喵萌奶茶屋] 葬送的芙莉蓮 - 12 [1080p][简体]", Show(keyword="葬送的芙莉莲")))

    def test_folding_keeps_existing_behaviour(self):
        self.assertEqual(_fold_name("魔法★探險家"), "魔法探险家")
        self.assertEqual(_fold_name("佐藤同学"), "佐藤同学")


class PrefixGuardTest(unittest.TestCase):
    """剧名只是另一部剧的前缀时不算命中。"""

    def test_short_name_does_not_match_a_longer_show(self):
        title = "[ANi] 死神少爺與黑女僕 第二季（僅限港澳台地區） - 12 [1080P][Bilibili][WEB-DL].mp4"
        self.assertFalse(title_matches(title, Show(keyword="死神")))

    def test_season_suffix_still_matches(self):
        for title in (
            "[喵萌奶茶屋] ★07月新番★[神之塔 二期 / Tower of God S2][22][1080p][简体][招募翻译]",
            "[喵萌奶茶屋][神之塔 第二季][10][1080p][简体]",
            "【喵萌奶茶屋】★07月新番★[神之塔 S2][11][1080p][简体]",
        ):
            self.assertTrue(title_matches(title, Show(keyword="神之塔")), title)

    def test_batch_suffix_still_matches(self):
        self.assertTrue(title_matches("佐藤同学 全集 [1080p]", Show(keyword="佐藤同学")))
        self.assertTrue(title_matches("[喵萌] 佐藤同学 合集 [1080p]", Show(keyword="佐藤同学")))

    def test_english_alias_after_slash_still_matches(self):
        title = "[LoliHouse] 魔法★探險家 / Magical Explorer - 07 [1080p][简体]"
        self.assertTrue(title_matches(title, Show(keyword="魔法★探險家")))

    def test_unrelated_title_still_rejected(self):
        self.assertFalse(title_matches("[ANi] 别的节目 - 01 [1080P]", Show(keyword="佐藤同学")))

    def test_exact_title_still_matches(self):
        self.assertTrue(title_matches("[喵萌] 佐藤同学 - 03 [1080p][简体]", Show(keyword="佐藤同学")))


class AliasDisambiguationTest(unittest.TestCase):
    """用 TMDB 别名把本剧续作和同名别的剧分开。"""

    BLEACH_ALIASES = [
        "BLEACH",
        "死神 千年血战篇",
        "BLEACH 千年血战篇",
        "漂灵",
        "死神千年血战诀别谭",
    ]

    def test_correct_show_with_arc_name_is_accepted(self):
        show = Show(keyword="死神", extra_aliases=self.BLEACH_ALIASES)
        for title in (
            "[沸羊羊制作] 死神千年血战篇-相克谭 - 01-14 [AMZN WebRip][1080p]",
            "[桜都字幕组] 死神 千年血战篇-祸进谭- / Bleach： Sennen Kessen Hen - Kashin Tan [10][1080P]",
        ):
            self.assertTrue(title_matches(title, show), title)

    def test_other_show_with_same_prefix_is_rejected(self):
        show = Show(keyword="死神", extra_aliases=self.BLEACH_ALIASES)
        for title in (
            "[Prejudice-Studio] 死神少爺與黑女僕（僅限港澳台地區） Shinigami Bocchan to Kuro Maid S1-S3 [1080P]",
            "[ANi] 死神少爺與黑女僕 第二季 - 12 [1080P][Bilibili][WEB-DL]",
        ):
            self.assertFalse(title_matches(title, show), title)

    def test_without_aliases_the_prefix_rule_still_blocks(self):
        show = Show(keyword="死神")
        self.assertFalse(
            title_matches("[ANi] 死神少爺與黑女僕 第二季 - 12 [1080P]", show)
        )

    def test_alias_equal_to_keyword_is_ignored(self):
        """别名里也有「死神」本身时，不能因此放行别的剧。"""
        show = Show(keyword="死神", extra_aliases=["死神", "BLEACH 千年血战篇"])
        self.assertFalse(
            title_matches("[ANi] 死神少爺與黑女僕 第二季 - 12 [1080P]", show)
        )
        self.assertTrue(
            title_matches("[桜都] 死神 千年血战篇-祸进谭- / Bleach [10][1080P]", show)
        )

    def test_other_shows_are_unaffected(self):
        show = Show(keyword="碧蓝之海", extra_aliases=["Grand Blue", "碧藍之海", "ぐらんぶる"])
        self.assertTrue(title_matches("[VCB-Studio] Grand Blue / 碧蓝之海 / ぐらんぶる 10-bit 1080p", show))
        show2 = Show(keyword="葬送的芙莉莲", extra_aliases=["Sousou no Frieren", "葬送のフリーレン"])
        self.assertTrue(title_matches("[喵萌奶茶屋][葬送的芙莉莲 / Sousou no Frieren][29][1080p]", show2))


if __name__ == "__main__":
    unittest.main()
