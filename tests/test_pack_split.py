"""整包入库：把一个包里的多集拆成一集一集搬进季目录，每集一个 strm。"""

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

from anime115.anime_flatten import Node  # noqa: E402
from anime115.anime_jobs import JobConfig, _pack_items, run_organize, run_split  # noqa: E402

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40
PACK = "/离线下载/动漫/20261109/[LoliHouse] Kakkou no Iinazuke S2 [01-12][WebRip 1080p HEVC-10bit AAC]"


class _Session:
    """只记动作的假会话。"""

    def __init__(self, dirs=None):
        self.dirs = dirs or {}
        self.moves = []
        self.deleted = []
        self.cancel = None
        self.offline = [{"info_hash": HASH, "status": 2}]

    def check_login(self):
        return True

    def sleep(self, _seconds):
        return None

    def ensure_dir(self, path):
        return type("F", (), {"fileid": "99", "path": path})()

    def list_dir(self, path):
        return list(self.dirs.get(path, []))

    def move(self, node, dest_dir, new_name):
        self.moves.append((node.name, dest_dir, new_name))
        return True

    def delete_dir(self, path):
        self.deleted.append(path)
        return True

    def list_offline(self, page=1):
        return {"data": {"tasks": self.offline}}


def _video(folder, name, size=900 * 1024 * 1024):
    return Node(f"{folder}/{name}", name, False, size, raw={"pc": "pc-" + name})


def _pack_session(files):
    session = _Session()
    session.dirs[PACK] = files
    # 收件箱里放着这个包目录
    session.dirs["/离线下载/动漫/20261109"] = [
        Node(PACK, PACK.rsplit("/", 1)[-1], True, 0, raw={"cid": "pack"})
    ]
    return session


def _task():
    return [{
        "infohash": HASH,
        "show": "杜鹃的婚约",
        "season": 2,
        "episode": 1,
        "title": "[LoliHouse] 杜鹃的婚约 / Kakkou no Iinazuke S2 [01-12][WebRip 1080p HEVC-10bit AAC]",
        "folder": "/离线下载/动漫/20261109",
        "status": "submitted",
    }]


class PackItemTest(unittest.TestCase):
    def _files(self):
        return [
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 01 [WebRip 1080p HEVC-10bit AAC].mkv"),
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 02 [WebRip 1080p HEVC-10bit AAC].mkv"),
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 03 [WebRip 1080p HEVC-10bit AAC].mkv"),
        ]

    def test_split_by_each_file_episode(self):
        items = _pack_items(self._files(), {"season": 2, "episode": 1}, PACK, self._files())
        self.assertEqual([item["episode"] for item in items], [1, 2, 3])
        self.assertTrue(all(item["season"] == 2 for item in items))

    def test_single_video_is_not_a_pack(self):
        files = self._files()[:1]
        self.assertEqual(_pack_items(files, {"season": 2}, PACK, files), [])

    def test_same_episode_twice_is_not_a_pack(self):
        files = [
            _video(PACK, "Show - 01 [1080p].mkv"),
            _video(PACK, "Show - 01 [1080p][简体].mkv"),
        ]
        self.assertEqual(_pack_items(files, {"season": 1}, "Show 01-02", files), [])

    def test_gaps_are_not_a_pack(self):
        files = [
            _video(PACK, "Show - 01 [1080p].mkv"),
            _video(PACK, "Show - 03 [1080p].mkv"),
        ]
        self.assertEqual(_pack_items(files, {"season": 1}, "Show 01-03", files), [])

    def test_file_without_episode_is_not_a_pack(self):
        files = [
            _video(PACK, "Show - 01 [1080p].mkv"),
            _video(PACK, "Show Special [1080p].mkv"),
        ]
        self.assertEqual(_pack_items(files, {"season": 1}, "Show 01-02", files), [])

    def test_file_stated_season_wins(self):
        """文件自己写明是第 2 季就按第 2 季拆，订阅写的是第 1 季也不拦。"""
        files = [
            _video(PACK, "Show S2 - 01 [1080p].mkv"),
            _video(PACK, "Show S2 - 02 [1080p].mkv"),
        ]
        items = _pack_items(files, {"season": 1}, "Show", files)
        self.assertEqual([item["season"] for item in items], [2, 2])
        self.assertEqual([item["episode"] for item in items], [1, 2])

    def test_conflicting_seasons_are_refused(self):
        """文件里写着不同的季，不敢拆。"""
        files = [
            _video(PACK, "Show S01E01 [1080p].mkv"),
            _video(PACK, "Show S02E02 [1080p].mkv"),
        ]
        self.assertEqual(_pack_items(files, {"season": 1}, "Show", files), [])


class OrganizePackTest(unittest.TestCase):
    def _config(self):
        return JobConfig(dry_run=False, library_root="/动漫", strm_root="/strm")

    def test_pack_is_split_into_episodes(self):
        files = [
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 01 [1080p].mkv"),
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 02 [1080p].mkv"),
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 03 [1080p].mkv"),
        ]
        session = _pack_session(files)
        tasks = _task()
        result = run_organize(tasks, session, self._config())
        moved = [name for name, _, _ in session.moves]
        self.assertEqual(len(moved), 3)
        for name in moved:
            self.assertIn("/动漫/杜鹃的婚约/Season 02", [dest for _, dest, _ in session.moves if _ == name][0])
        self.assertEqual(tasks[0]["status"], "moved")
        self.assertEqual(result["moves"][0]["moved_episodes"], 3)
        self.assertEqual(result["moves"][0]["pack_episodes"], 3)

    def test_pack_with_collision_skips_that_episode(self):
        files = [
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 01 [1080p].mkv"),
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 02 [1080p].mkv"),
        ]
        session = _pack_session(files)
        session.dirs["/动漫/杜鹃的婚约/Season 02"] = [
            Node("/动漫/杜鹃的婚约/Season 02/[LoliHouse] Kakkou no Iinazuke S2 - 01 [1080p].mkv",
                 "[LoliHouse] Kakkou no Iinazuke S2 - 01 [1080p].mkv", False, 1),
        ]
        tasks = _task()
        run_organize(tasks, session, self._config())
        moved = [name for name, _, _ in session.moves]
        self.assertEqual(len(moved), 1)
        self.assertIn("- 02", moved[0])

    def test_ambiguous_pack_is_left_alone(self):
        """一集有多个版本时不拆，按原来的规矩留在原地。"""
        files = [
            _video(PACK, "Show - 01 [1080p][A].mkv"),
            _video(PACK, "Show - 01 [1080p][B].mkv"),
        ]
        session = _pack_session(files)
        tasks = _task()
        result = run_organize(tasks, session, self._config())
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")
        self.assertEqual(result["moves"][0]["action"], "leave")


class NestedPackSplitTest(unittest.TestCase):
    """季目录里多出来的一层包目录，要拆开。"""

    SHOW = "/动漫/杜鹃的婚约"
    SEASON = "/动漫/杜鹃的婚约/Season 02"
    NESTED = "/动漫/杜鹃的婚约/Season 02/[LoliHouse] Kakkou no Iinazuke S2 [01-12][WebRip 1080p HEVC-10bit AAC]"

    def _session(self):
        files = [
            _video(self.NESTED, "[LoliHouse] Kakkou no Iinazuke S2 - 01 [1080p].mkv"),
            _video(self.NESTED, "[LoliHouse] Kakkou no Iinazuke S2 - 02 [1080p].mkv"),
        ]
        session = _Session({
            "/动漫": [Node(self.SHOW, "杜鹃的婚约", True, 0, raw={"cid": "s"})],
            self.SHOW: [Node(self.SEASON, "Season 02", True, 0, raw={"cid": "se"})],
            self.SEASON: [Node(self.NESTED, self.NESTED.rsplit("/", 1)[-1], True, 0, raw={"cid": "n"})],
            self.NESTED: files,
        })
        return session

    def test_nested_pack_is_split(self):
        session = self._session()
        tasks = []
        result = run_split(session, JobConfig(dry_run=False, library_root="/动漫", strm_root="/strm"))
        moved = [name for name, _, _ in session.moves]
        self.assertEqual(len(moved), 2)
        self.assertTrue(all(dest == self.SEASON for _, dest, _ in session.moves))
        self.assertEqual(result["moved"], 2)
        self.assertEqual(len(result["items"]), 1)

    def test_nested_pack_with_existing_file_is_skipped(self):
        """季目录里已经有同名文件，这一集跳过，不覆盖。"""
        session = self._session()
        session.dirs[self.SEASON] = session.dirs[self.SEASON] + [
            Node(f"{self.SEASON}/[LoliHouse] Kakkou no Iinazuke S2 - 01 [1080p].mkv",
                 "[LoliHouse] Kakkou no Iinazuke S2 - 01 [1080p].mkv", False, 1),
        ]
        result = run_split(session, JobConfig(dry_run=False, library_root="/动漫", strm_root="/strm"))
        moved = [name for name, _, _ in session.moves]
        self.assertEqual(len(moved), 1)
        self.assertIn("- 02", moved[0])
        self.assertEqual(result["moved"], 1)

    def test_nested_folder_with_one_video_is_kept(self):
        session = self._session()
        session.dirs[self.NESTED] = session.dirs[self.NESTED][:1]
        result = run_split(session, JobConfig(dry_run=False, library_root="/动漫", strm_root="/strm"))
        self.assertEqual(session.moves, [])
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()


class RealPackNameTest(unittest.TestCase):
    """生产上真实出现过的整包文件名。"""

    def _files(self, pack, names):
        return [_video(pack, name) for name in names]

    def test_end_style_episode_numbers(self):
        """「24 END」「[12END]」这种写法也要认得出集数。"""
        pack = "/离线下载/动漫/20261109/Strike the Blood [BD 1920x1080 HEVC-10bit AAC]"
        files = self._files(pack, [
            "Strike the Blood 22 END [BD 1920x1080 HEVC-10bit AAC ASSx2].mkv",
            "Strike the Blood 23 END [BD 1920x1080 HEVC-10bit AAC ASSx2].mkv",
            "Strike the Blood 24 END [BD 1920x1080 HEVC-10bit AAC ASSx2].mkv",
        ])
        items = _pack_items(files, {"season": 1, "episode": 24}, pack, files)
        self.assertEqual([item["episode"] for item in items], [22, 23, 24])

    def test_special_episodes_are_not_split(self):
        """SP 特别篇不进正片。"""
        pack = "/离线下载/动漫/20261109/Re Zero [WebRip 1080p HEVC-10bit AAC ASSx2]"
        files = self._files(pack, [
            "[DMG] Re Zero kara Hajimeru Isekai Seikatsu - SP30 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
            "[DMG] Re Zero kara Hajimeru Isekai Seikatsu - SP31 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
        ])
        self.assertEqual(_pack_items(files, {"season": 1}, pack, files), [])

    def test_season_marked_pack(self):
        pack = "/离线下载/动漫/20261109/[BDrip] Strike The Blood Final [JP][7³ACG]"
        files = self._files(pack, [
            "Strike The Blood Final S05E01-[1080p][BDRIP][x265.FLAC].mkv",
            "Strike The Blood Final S05E02-[1080p][BDRIP][x265.FLAC].mkv",
            "Strike The Blood Final S05E03-[1080p][BDRIP][x265.FLAC].mkv",
            "Strike The Blood Final S05E04-[1080p][BDRIP][x265.FLAC].mkv",
        ])
        items = _pack_items(files, {"season": 5, "episode": 4}, pack, files)
        self.assertEqual([item["episode"] for item in items], [1, 2, 3, 4])
        self.assertTrue(all(item["season"] == 5 for item in items))

    def test_pack_without_season_in_file_names(self):
        """文件名不带季号时，按订阅的季算，不能当成第 1 季就拒掉。"""
        pack = "/离线下载/动漫/20261109/[Nekomoe kissaten&LoliHouse] Megami no Cafe Terrace [01-12][WebRip 1080p HEVC-10bit AAC]"
        files = self._files(pack, [
            "[Nekomoe kissaten&LoliHouse] Megami no Cafe Terrace - 05 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
            "[Nekomoe kissaten&LoliHouse] Megami no Cafe Terrace - 06 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
        ])
        items = _pack_items(files, {"season": 1, "episode": 5}, pack, files)
        self.assertEqual([item["episode"] for item in items], [5, 6])

    def test_season_from_pack_name(self):
        """包名写着 S2 时，季以包名为准。"""
        pack = "/离线下载/动漫/20261109/[LoliHouse] Kakkou no Iinazuke S2 [01-12][WebRip 1080p HEVC-10bit AAC]"
        files = self._files(pack, [
            "[LoliHouse] Kakkou no Iinazuke S2 - 01 [WebRip 1080p HEVC-10bit AAC].mkv",
            "[LoliHouse] Kakkou no Iinazuke S2 - 02 [WebRip 1080p HEVC-10bit AAC].mkv",
        ])
        items = _pack_items(files, {"season": 2, "episode": 1}, pack, files)
        self.assertEqual([item["episode"] for item in items], [1, 2])
        self.assertTrue(all(item["season"] == 2 for item in items))

    def test_file_of_other_season_uses_its_own_season(self):
        """文件写的是第 2 季，就进第 2 季，不会塞进第 1 季。"""
        pack = "/离线下载/动漫/20261109/Show [01-12]"
        files = self._files(pack, [
            "Show S02E01 [1080p].mkv",
            "Show S02E02 [1080p].mkv",
        ])
        items = _pack_items(files, {"season": 1}, pack, files)
        self.assertEqual([item["season"] for item in items], [2, 2])


class PublicRecordTest(unittest.TestCase):
    """对外记录不能带文件对象，否则存库会失败。"""

    def test_pack_record_is_json_serialisable(self):
        import json

        from anime115.anime_jobs import _public_move

        files = [
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 01 [1080p].mkv"),
            _video(PACK, "[LoliHouse] Kakkou no Iinazuke S2 - 02 [1080p].mkv"),
        ]
        session = _pack_session(files)
        tasks = _task()
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫", strm_root="/strm"))
        json.dumps(result)
        record = _public_move(result["moves"][0])
        json.dumps(record)
        self.assertNotIn("items", record)
        self.assertNotIn("nodes", record)
        self.assertEqual([item["episode"] for item in record["episodes"]], [1, 2])
