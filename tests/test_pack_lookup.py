"""离线包认领：115 上是种子原名，花园上是重命名后的标题。

生产故障（2026-11-12 02:38，整理 63 条里 47 条「找不到这一包」）的成因：
动漫花园展示的标题是重命名过的（字幕组中文名 + 中文剧名 + 换过的标签），
115 落地的离线包目录却用种子原名（字幕组原名 + 原剧名 + 原扩展名）。
两边对不上，插件在收件箱里找不到自己的那一包，任务永远留在待整理。

这里用 115 上真实存在的目录名和花园上真实存在的标题做对照，
只验证「找自己那一包」，不连 115 也不连动漫花园。
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
sys.modules["anime115"] = _pkg

from anime115.anime_flatten import Node  # noqa: E402
from anime115.anime_jobs import (  # noqa: E402
    JobConfig,
    _choose_child,
    _pack_candidates,
    _pack_episodes,
    _pack_match,
    run_organize,
)

HASH = "a" * 40
DAY = "/离线下载/动漫/20261109"


def _nodes(*names: str) -> list[Node]:
    """收件箱里的离线包目录。115 上离线包是目录，名字是种子原名。"""
    return [
        Node(path=f"{DAY}/{name}", name=name, is_dir=True, raw={"fid": str(index)})
        for index, name in enumerate(names)
    ]


class _Fake:
    """只列目录的假会话。"""

    def __init__(self, dirs: dict, offline: list):
        self.dirs = dirs
        self.offline = offline
        self.moves = []
        self.listed = []

    def check_login(self):
        return True

    def sleep(self, _seconds):
        return None

    def quota(self):
        return {"data": {}}

    def ensure_dir(self, path):
        return type("Folder", (), {"fileid": "99", "path": path})()

    def list_offline(self, _page):
        return {"data": {"tasks": self.offline}}

    def list_dir(self, path):
        self.listed.append(path)
        return list(self.dirs.get(path, []))

    def move(self, node, dest_dir, new_name):
        self.moves.append((node.path, dest_dir, new_name))
        return True

    def delete_dir(self, path):
        return True


class PackLookupTest(unittest.TestCase):
    """115 上的原名目录，要能被花园上的重命名标题找到。"""

    def test_translated_group_and_chinese_title(self):
        """喵萌奶茶屋 -> Nekomoe kissaten，标题多出中文剧名，标签也换过。"""
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        pack = "[Nekomoe kissaten&LoliHouse] Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
        chosen, why = _choose_child(_nodes(pack), [wanted], "", 38)
        self.assertIsNotNone(chosen)
        self.assertEqual(why, "")
        self.assertEqual(chosen.name, pack)

    def test_group_name_kept_out_of_the_pack(self):
        """字幕组两边的写法完全不一样，也不能当成认不出的理由。"""
        wanted = "【悠哈璃羽字幕社】[葬送的芙莉莲_Sousou no Frieren][27v2][x264 1080p][CHS]"
        pack = "[UHA-Wing][Sousou no Frieren][27v2][x264 1080P][CHS].mp4"
        chosen, why = _choose_child(_nodes(pack), [wanted], "", 27)
        self.assertIsNotNone(chosen)
        self.assertEqual(why, "")
        self.assertEqual(chosen.name, pack)

    def test_chinese_title_on_115(self):
        """115 上带中文剧名时一样认得出。"""
        wanted = "[桜都字幕组] 迷宫饭 / Dungeon Meshi [24][1080p][简体内嵌]"
        pack = "[Sakurato] Dungeon Meshi [24][AVC-8bit 1080p AAC][CHS].mp4"
        chosen, why = _choose_child(_nodes(pack), [wanted], "", 24)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, pack)

    def test_case_and_bracket_differences(self):
        """大小写、方括号、扩展名不同也要认。"""
        wanted = "[7³ACG] 噬血狂袭 最终季/ストライク・ザ・ブラッドFINAL/Strike the Blood Final | 01-04[简繁字幕] BDrip x265 FLAC"
        pack = "[BDrip] Strike The Blood Final [JP][7³ACG]"
        chosen, why = _choose_child(_nodes(pack), [wanted], "", 0)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, pack)

    def test_other_show_is_not_taken(self):
        """别的剧的包不能当成自己的。"""
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        other = "[Nekomoe kissaten&LoliHouse] Dungeon Meshi - 38 [WebRip 1080p HEVC-10bit AAC].mkv"
        chosen, why = _choose_child(_nodes(other), [wanted], "", 38)
        self.assertIsNone(chosen)
        self.assertEqual(why, "")

    def test_other_episode_is_not_taken(self):
        """同一部剧的别的集也不能当成自己的。"""
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        other = "[Nekomoe kissaten&LoliHouse] Sousou no Frieren - 37 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
        chosen, why = _choose_child(_nodes(other), [wanted], "", 38)
        self.assertIsNone(chosen)
        self.assertEqual(why, "")

    def test_two_candidates_are_left_alone(self):
        """同一部剧同一集有两包时不猜，宁可留着不动。"""
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        first = "[Nekomoe kissaten&LoliHouse] Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
        second = "[Haruhana] Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC].mkv"
        chosen, why = _choose_child(_nodes(first, second), [wanted], "", 38)
        self.assertIsNone(chosen)
        self.assertTrue(why.startswith("不止一个目录"))

    def test_exact_episode_wins_over_batch(self):
        """整包里也有这一集，但单集那一包才是这一条要的。"""
        wanted = "[❀拨雪寻春❀] 葬送的芙莉莲 / 葬送のフリーレン / Sousou no Frieren - 14 [BDRip][HEVC-10bit 1080p][简繁日内封]"
        batch = "[UHA-Wings][Sousou no Frieren][01-28(v2)][x264 1080P][CHS]"
        single = "[Haruhana] Sousou no Frieren - 14 [HEVC-10bit 1080p][CHI_JPN].mkv"
        chosen, why = _choose_child(_nodes(batch, single), [wanted], "", 14)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, single)

    def test_batch_pack_is_found_by_its_range(self):
        """整包标题认得整包目录。"""
        wanted = "【悠哈璃羽字幕社】[葬送的芙莉莲_Sousou no Frieren][01-28 END（v2）][x264 1080p][CHS]"
        batch = "[UHA-Wings][Sousou no Frieren][01-28(v2)][x264 1080P][CHS]"
        chosen, why = _choose_child(_nodes(batch), [wanted], "", 0)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, batch)

    def test_partial_range_is_not_the_whole_batch(self):
        """只有一部分的包不能当成整包。"""
        wanted = "[LoliHouse] 杜鹃的婚约 第二季 / Kakkou no Iinazuke S2 [01-12 合集][WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
        part = "[LoliHouse] Kakkou no Iinazuke S2 [01-04][WebRip 1080p HEVC-10bit AAC]"
        chosen, why = _choose_child(_nodes(part), [wanted], "", 0)
        self.assertIsNone(chosen)
        self.assertEqual(why, "")

    def test_chinese_only_title_still_matches(self):
        """115 上只有中文剧名时按中文认。"""
        wanted = "[NC-Raws] 弱势角色友崎君 - 12 [WEB-DL][1080p][AVC AAC][CHS_CHT_ENG_TH_SRT][MKV].mkv"
        pack = "[NC-Raws] 弱势角色友崎君 - 12 [WEB-DL][1080p][AVC AAC][CHS_CHT_ENG_TH_SRT][MKV].mkv"
        chosen, why = _choose_child(_nodes(pack), [wanted], "", 12)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, pack)

    def test_pure_chinese_title_does_not_match_other_show(self):
        """只有中文剧名时不能把别的剧认走。"""
        wanted = "[NC-Raws] 弱势角色友崎君 - 12 [WEB-DL][1080p][AVC AAC].mkv"
        other = "[NC-Raws] 精灵幻想记 - 12 [B-Global][WEB-DL][1080p][AVC AAC].mkv"
        chosen, why = _choose_child(_nodes(other), [wanted], "", 12)
        self.assertIsNone(chosen)
        self.assertEqual(why, "")

    def test_other_season_is_not_taken(self):
        """第二季的标题不能认第一季的包。"""
        wanted = "[orion origin] Goblin Slayer II [11v2] [1080p] [H265 AAC] [CHT＆JPN].mp4"
        other = "[JYFanSub][Goblin Slayer][11][GB][1080P][MP4].mp4"
        chosen, why = _choose_child(_nodes(other), [wanted], "", 11)
        self.assertIsNone(chosen)
        self.assertEqual(why, "")

    def test_common_word_alone_is_not_evidence(self):
        """只共有一个 no、the 这种常用词，认不出是哪部剧。"""
        wanted = "[Sakurato] Koori no Jouheki [13][AVC-8bit 1080P AAC][CHS].mp4"
        other = "[Maou Gakuin no Futekigousha][13][BIG5][1080P][END].mp4"
        chosen, why = _choose_child(_nodes(other), [wanted], "", 13)
        self.assertIsNone(chosen)
        self.assertEqual(why, "")

    def test_title_inside_brackets_is_not_skipped(self):
        """整包名字都在方括号里时，不能当成字幕组放过。"""
        wanted = "[Sakurato] Koori no Jouheki [13][AVC-8bit 1080P AAC][CHS].mp4"
        other = "[Koori no Jouheki][13]"
        chosen, why = _choose_child(_nodes(other), [wanted], "", 13)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.name, other)


class PackMatchUnitTest(unittest.TestCase):
    """匹配规则本身。"""

    def test_episodes_are_read_from_names(self):
        self.assertEqual(_pack_episodes("[UHA-Wing][Sousou no Frieren][27v2][x264 1080P][CHS].mp4"), {27})
        self.assertEqual(_pack_episodes("[UHA-Wings][Sousou no Frieren][01-28(v2)][x264 1080P][CHS]"), set(range(1, 29)))
        self.assertEqual(_pack_episodes("[千夏字幕组][葬送的芙莉莲_Sousou no Frieren][第32话][1080p_AVC][简体]"), {32})
        self.assertEqual(_pack_episodes("Sousou no Frieren - 38 [WebRip]"), {38})
        self.assertEqual(_pack_episodes("[Haruhana] Sousou no Frieren - 14 [HEVC-10bit 1080p].mkv"), {14})
        self.assertEqual(_pack_episodes("[UHA-Wing][Dungeon Meshi][20][x264 1080P][CHS].mp4"), {20})

    def test_resolution_is_not_an_episode(self):
        self.assertEqual(_pack_episodes("[NC-Raws] 弱势角色友崎君 - 12 [WEB-DL][1080p][AVC AAC][MKV].mkv"), {12})
        self.assertEqual(_pack_episodes("[LoliHouse] Magical Explorer - 01 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"), {1})

    def test_same_show_different_group_is_a_match(self):
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        self.assertEqual(_pack_match(wanted, "[Nekomoe kissaten&LoliHouse] Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv", 38), "exact")

    def test_other_show_is_not_a_match(self):
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        self.assertEqual(_pack_match(wanted, "[Nekomoe kissaten&LoliHouse] Dungeon Meshi - 38 [WebRip 1080p HEVC-10bit AAC].mkv", 38), "")
        self.assertEqual(_pack_match(wanted, "[Nekomoe kissaten&LoliHouse] Sousou no Frieren Isekai - 38 [WebRip 1080p HEVC-10bit AAC].mkv", 38), "")

    def test_batch_relation(self):
        wanted = "[❀拨雪寻春❀] 葬送的芙莉莲 / 葬送のフリーレン / Sousou no Frieren - 14 [BDRip][HEVC-10bit 1080p][简繁日内封]"
        self.assertEqual(_pack_match(wanted, "[UHA-Wings][Sousou no Frieren][01-28(v2)][x264 1080P][CHS]", 14), "batch")

    def test_candidates_report_ambiguity(self):
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        first = "[Nekomoe kissaten&LoliHouse] Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
        second = "[Haruhana] Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC].mkv"
        packs, many = _pack_candidates(_nodes(first, second), wanted, 38)
        self.assertEqual(len(packs), 2)
        self.assertTrue(many)


class OrganizePackTest(unittest.TestCase):
    """整条整理流程：找到包，搬走正片。"""

    def _run(self, wanted, pack_name, episode):
        pack = f"{DAY}/{pack_name}"
        video = f"{pack}/{pack_name}"
        session = _Fake(
            {
                DAY: _nodes(pack_name),
                pack: [Node(path=video, name=pack_name, is_dir=False, size=900 * 1024 * 1024, raw={"fid": "v1"})],
            },
            [{"info_hash": HASH, "status": 2, "name": wanted}],
        )
        tasks = [{
            "infohash": HASH,
            "show": "葬送的芙莉莲",
            "season": 1,
            "episode": episode,
            "title": wanted,
            "folder": DAY,
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        return result, session, pack

    def test_renamed_title_is_moved(self):
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        pack_name = "[Nekomoe kissaten&LoliHouse] Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
        result, session, pack = self._run(wanted, pack_name, 38)
        move = result["moves"][0]
        self.assertEqual(move["action"], "move")
        self.assertEqual(move["pack"], pack)
        self.assertEqual(move["dest_dir"], "/动漫/葬送的芙莉莲/Season 01")
        self.assertEqual([item[0] for item in session.moves], [f"{pack}/{pack_name}"])

    def test_missing_pack_stays_pending(self):
        """115 上根本没有这一包时，任务留着不动，也不搬别人的包。"""
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        other = "[Nekomoe kissaten&LoliHouse] Sousou no Frieren - 37 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
        pack = f"{DAY}/{other}"
        session = _Fake(
            {
                DAY: _nodes(other),
                pack: [Node(path=f"{pack}/{other}", name=other, is_dir=False, size=900 * 1024 * 1024, raw={"fid": "v1"})],
            },
            [{"info_hash": HASH, "status": 2, "name": wanted}],
        )
        tasks = [{
            "infohash": HASH, "show": "葬送的芙莉莲", "season": 1, "episode": 38,
            "title": wanted, "folder": DAY, "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "leave")
        self.assertTrue(result["moves"][0]["reason"].startswith("找不到这一包"))
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")

    def test_multi_episode_pack_is_not_moved(self):
        """整包里有多集时不动它，任务继续留着。"""
        wanted = "[喵萌奶茶屋&LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 38 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        pack_name = "[UHA-Wings][Sousou no Frieren][01-28(v2)][x264 1080P][CHS]"
        pack = f"{DAY}/{pack_name}"
        session = _Fake(
            {
                DAY: _nodes(pack_name),
                pack: [
                    Node(path=f"{pack}/a.mkv", name="a.mkv", is_dir=False, size=900 * 1024 * 1024, raw={"fid": "v1"}),
                    Node(path=f"{pack}/b.mkv", name="b.mkv", is_dir=False, size=900 * 1024 * 1024, raw={"fid": "v2"}),
                ],
            },
            [{"info_hash": HASH, "status": 2, "name": wanted}],
        )
        tasks = [{
            "infohash": HASH, "show": "葬送的芙莉莲", "season": 1, "episode": 38,
            "title": wanted, "folder": DAY, "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "leave")
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
