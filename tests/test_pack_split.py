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

    def test_other_season_file_is_not_a_pack(self):
        files = [
            _video(PACK, "Show S2 - 01 [1080p].mkv"),
            _video(PACK, "Show S2 - 02 [1080p].mkv"),
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

    def test_nested_folder_with_one_video_is_kept(self):
        session = self._session()
        session.dirs[self.NESTED] = session.dirs[self.NESTED][:1]
        result = run_split(session, JobConfig(dry_run=False, library_root="/动漫", strm_root="/strm"))
        self.assertEqual(session.moves, [])
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
