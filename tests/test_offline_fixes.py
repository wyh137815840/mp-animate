"""离线列表查询失败与 add_task_urls 逐任务结果的回归测试。

覆盖两处数据面修复：
1. list_offline 返回 None / 非 dict/list 时，不能再当成「列表已翻完」。
2. _accepted 在顶层通过后，还要校验 data 里的逐任务 state/code。
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
_pkg = types.ModuleType("anime115")
_pkg.__path__ = [str(_ROOT)]
_pkg.__package__ = "anime115"
sys.modules["anime115"] = _pkg

from anime115.anime_dmhy import Torrent  # noqa: E402
from anime115.anime_flatten import Node  # noqa: E402
from anime115.anime_jobs import JobConfig, _accepted, run_daily, run_organize  # noqa: E402
from anime115.anime_names import parse_watchlist  # noqa: E402


NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40
DAY = "/离线下载/动漫/20261110"
VIDEO = "[LoliHouse] 间谍教室 - 05 [1080p].mkv"


def _task() -> dict:
    return {
        "infohash": HASH,
        "show": "间谍教室",
        "season": 1,
        "episode": 5,
        "title": VIDEO,
        "folder": DAY,
        "status": "submitted",
    }


class _FakeSession:
    """低配 115 会话：只实现 run_daily / run_organize 用得到的部分。"""

    def __init__(self):
        self.cancel = None
        self.added = []
        self.moves = []
        self.dirs = {}
        self.asked = []

    def check_login(self):
        return True

    def sleep(self, _seconds):
        return None

    def quota(self):
        return {"data": {"surplus": 3}}

    def ensure_dir(self, path):
        return type("Folder", (), {"fileid": "99", "path": path})()

    def add_offline(self, magnet, _fileid):
        self.added.append(magnet.split("btih:")[1][:40])
        return {"state": True, "code": 0, "data": {"wp_path_id": "99"}}

    def list_offline(self, page):
        self.asked.append(page)
        return {"data": {"tasks": [], "page": page, "page_count": 1}}

    def list_dir(self, path):
        return list(self.dirs.get(path, []))

    def move(self, node, dest_dir, new_name):
        self.moves.append((node.path, dest_dir, new_name))
        return True

    def delete_dir(self, path):
        return True


class _NoneOffline(_FakeSession):
    """list_offline 永远返回 None（宿主 _request_api 失败时的样子）。"""

    def list_offline(self, page):
        self.asked.append(page)
        return None


class _FirstPageNone(_NoneOffline):
    pass


class _SecondPageNone(_FakeSession):
    """第一页满 20 条且没有总数，第二页请求失败。"""

    def list_offline(self, page):
        self.asked.append(page)
        if page == 1:
            rows = [{"info_hash": f"{index:02d}{'ab' * 19}", "status": 2} for index in range(20)]
            return {"data": {"tasks": rows}}
        return None


class _WeirdPayload(_FakeSession):
    def list_offline(self, page):
        self.asked.append(page)
        return "not-a-payload"


class _EmptyOffline(_FakeSession):
    def list_offline(self, page):
        self.asked.append(page)
        return {"data": {"tasks": [], "page": 1, "page_count": 1}}


class _FailedItemSession(_FakeSession):
    """add_task_urls 顶层成功，但逐任务 state=false。"""

    def add_offline(self, magnet, _fileid):
        self.added.append(magnet.split("btih:")[1][:40])
        return {
            "state": True,
            "code": 0,
            "data": [{"state": False, "code": 20001, "message": "离线任务已存在"}],
        }


def _config(**kwargs) -> JobConfig:
    base = dict(dry_run=False, library_root="/动漫", offline_root="/离线下载/动漫")
    base.update(kwargs)
    return JobConfig(**base)


class OfflineListFailureTest(unittest.TestCase):
    """查询失败不能被当成「列表已翻完」。"""

    def test_first_page_none_does_not_move(self):
        session = _FirstPageNone()
        session.dirs[DAY] = [Node(f"{DAY}/{VIDEO}", VIDEO, False, 900 * 1024 * 1024)]
        tasks = [_task()]
        result = run_organize(tasks, session, _config())
        self.assertEqual(session.asked, [1])
        self.assertEqual(result["moves"], [])
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")

    def test_second_page_none_does_not_move(self):
        session = _SecondPageNone()
        session.dirs[DAY] = [Node(f"{DAY}/{VIDEO}", VIDEO, False, 900 * 1024 * 1024)]
        tasks = [_task()]
        result = run_organize(tasks, session, _config())
        self.assertEqual(session.asked, [1, 2])
        self.assertEqual(result["moves"], [])
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")

    def test_unknown_payload_type_does_not_move(self):
        session = _WeirdPayload()
        session.dirs[DAY] = [Node(f"{DAY}/{VIDEO}", VIDEO, False, 900 * 1024 * 1024)]
        tasks = [_task()]
        result = run_organize(tasks, session, _config())
        self.assertEqual(result["moves"], [])
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")

    def test_empty_task_list_still_completes(self):
        """空列表仍是「列表已翻完」，按日期收件箱整理的老语义不变。"""
        from anime115.anime_config import PLAY_BASE, PLAY_BASE_LAN

        session = _EmptyOffline()
        session.dirs[DAY] = [
            Node(f"{DAY}/{VIDEO}", VIDEO, False, 900 * 1024 * 1024, raw={"pc": "spyclasscode00001"}),
        ]
        tasks = [_task()]
        with tempfile.TemporaryDirectory() as tmp:
            result = run_organize(tasks, session, _config(
                strm_root=tmp,
                play_base=PLAY_BASE,
                play_base_lan=PLAY_BASE_LAN,
                strm_for="lan",
            ))
            self.assertEqual(session.asked, [1])
            self.assertEqual(len(result["moves"]), 1)
            self.assertEqual(result["moves"][0]["action"], "move")
            self.assertEqual(result["moves"][0]["name"], VIDEO)
            self.assertEqual(tasks[0]["status"], "moved")
            self.assertEqual(len(session.moves), 1)


class AcceptedTest(unittest.TestCase):
    """add_task_urls 逐任务结果校验。"""

    def test_top_success_with_failed_item_is_rejected(self):
        payload = {"state": True, "code": 0, "data": [{"state": False, "code": 0}]}
        self.assertFalse(_accepted(payload))

    def test_top_success_with_all_ok_items_is_accepted(self):
        payload = {"state": True, "code": 0, "data": [{"state": True, "code": 0}]}
        self.assertTrue(_accepted(payload))
        repeated = {"state": True, "code": 0, "data": [{"state": True, "code": 20004}]}
        self.assertTrue(_accepted(repeated))

    def test_top_success_with_error_code_item_is_rejected(self):
        payload = {"code": 0, "data": [{"state": True, "code": 20001, "message": "出错"}]}
        self.assertFalse(_accepted(payload))

    def test_dict_data_with_failed_state_is_rejected(self):
        payload = {"state": True, "code": 0, "data": {"state": False, "code": 0}}
        self.assertFalse(_accepted(payload))

    def test_dict_data_without_state_or_code_is_accepted(self):
        payload = {"state": True, "code": 0, "data": {"wp_path_id": "99"}}
        self.assertTrue(_accepted(payload))
        self.assertTrue(_accepted({"data": {"wp_path_id": "99"}}))

    def test_without_data_keeps_old_semantics(self):
        self.assertTrue(_accepted({"state": True, "code": 0}))
        self.assertTrue(_accepted({"state": True}))
        self.assertTrue(_accepted({"code": 0, "data": {}}))
        # 原来就拒绝的 {} 仍要拒绝。
        self.assertFalse(_accepted({}))

    def test_empty_data_list_is_accepted(self):
        self.assertTrue(_accepted({"state": True, "code": 0, "data": []}))


class DailyAcceptedTest(unittest.TestCase):
    def test_failed_item_is_not_written_to_seen(self):
        show = parse_watchlist("佐藤同学")[0]
        torrent = Torrent(
            "佐藤同学 - 03 [1080p][简体]",
            HASH,
            f"magnet:?xt=urn:btih:{HASH}",
            900 * 1024 * 1024,
            NOW,
        )
        session = _FailedItemSession()
        seen = {}
        result = run_daily([show], session, lambda _show: [torrent], seen, NOW, _config(dry_run=False))
        self.assertEqual(session.added, [HASH])
        self.assertEqual(result["submitted"], [])
        self.assertNotIn(HASH, seen.get("hashes", {}))
        self.assertEqual(result["notes"][-1]["reason"], "离线提交失败")
        # 下一轮不应该被 3 天窗口挡掉，仍会重新提交。
        again = run_daily([show], session, lambda _show: [torrent], seen, NOW, _config(dry_run=False))
        self.assertEqual(len(session.added), 2)
        self.assertEqual(again["submitted"], [])


if __name__ == "__main__":
    unittest.main()
