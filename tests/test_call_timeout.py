"""115 调用超时兜底的测试。挂住的调用不能把整轮任务拖死。"""

from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
_pkg = types.ModuleType("anime115")
_pkg.__path__ = [str(_ROOT)]
_pkg.__package__ = "anime115"
sys.modules.setdefault("anime115", _pkg)

from anime115 import anime_jobs  # noqa: E402
from anime115.anime_call import Stopped, stopped, system_stopped, wait_call  # noqa: E402
from anime115.anime_flatten import Node  # noqa: E402
from anime115.anime_jobs import JobConfig, _list_dir, run_organize  # noqa: E402

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40


class HangingSession:
    """某些方法会一直挂着，直到测试放行。"""

    def __init__(self, hang_on=("list_dir",)):
        self.hang_on = set(hang_on)
        self.release = threading.Event()
        self.cancel = None
        self.moves = []
        self.offline = [{"info_hash": HASH, "status": 2}]
        self.dirs = {}

    def _maybe_hang(self, name):
        if name in self.hang_on:
            self.release.wait(30)

    def list_dir(self, path):
        self._maybe_hang("list_dir")
        return list(self.dirs.get(path, []))

    def ensure_dir(self, path):
        self._maybe_hang("ensure_dir")
        return type("F", (), {"fileid": "99", "path": path})()

    def move(self, node, dest_dir, new_name):
        self._maybe_hang("move")
        self.moves.append((node.path, dest_dir, new_name))
        return True

    def delete_dir(self, path):
        self._maybe_hang("delete_dir")
        return True

    def check_login(self):
        return True

    def quota(self):
        return {"data": {"surplus": 10}}

    def list_offline(self, page=1):
        return {"data": {"tasks": self.offline}}

    def sleep(self, _seconds):
        return None


class WaitCallTest(unittest.TestCase):
    def test_timeout_raises_and_does_not_hang(self):
        release = threading.Event()

        def slow():
            release.wait(30)
            return "late"

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            wait_call(None, slow, 0.3)
        self.assertLess(time.monotonic() - started, 3)
        release.set()

    def test_cancel_event_stops_the_wait(self):
        cancel = threading.Event()
        release = threading.Event()
        threading.Timer(0.2, cancel.set).start()

        def slow():
            release.wait(30)
            return "late"

        with self.assertRaises(Stopped):
            wait_call(cancel, slow, 30)
        release.set()

    def test_error_from_worker_is_reraised(self):
        def boom():
            raise RuntimeError("115 挂了")

        with self.assertRaises(RuntimeError):
            wait_call(None, boom, 5)

    def test_system_stopped_is_false_without_host(self):
        self.assertFalse(system_stopped())
        self.assertFalse(stopped(None))


class ListDirTimeoutTest(unittest.TestCase):
    def test_list_dir_timeout_raises(self):
        session = HangingSession()
        previous = anime_jobs._STORAGE_CALL_TIMEOUT
        anime_jobs._STORAGE_CALL_TIMEOUT = 0.3
        try:
            with self.assertRaises(TimeoutError):
                _list_dir(session, "/动漫")
        finally:
            anime_jobs._STORAGE_CALL_TIMEOUT = previous
            session.release.set()


class OrganizeTimeoutTest(unittest.TestCase):
    def _config(self):
        return JobConfig(dry_run=False, library_root="/动漫")

    def _task(self):
        return [{
            "infohash": HASH,
            "show": "佐藤同学",
            "season": 1,
            "episode": 3,
            "title": "佐藤同学 - 03",
            "folder": "/box",
            "status": "submitted",
        }]

    def test_hanging_list_dir_does_not_block_the_round(self):
        session = HangingSession()
        previous = anime_jobs._STORAGE_CALL_TIMEOUT
        anime_jobs._STORAGE_CALL_TIMEOUT = 0.3
        started = time.monotonic()
        try:
            result = run_organize(self._task(), session, self._config())
        finally:
            anime_jobs._STORAGE_CALL_TIMEOUT = previous
            session.release.set()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(session.moves, [])
        self.assertEqual(self._task()[0]["status"], "submitted")
        reasons = [item.get("reason") for item in (result.get("moves") or [])]
        self.assertTrue(any("失败" in (reason or "") for reason in reasons), reasons)

    def test_hanging_move_is_recorded_as_failure(self):
        video = "佐藤同学 - 03.mkv"
        node = Node("/box/" + video, video, False, 900 * 1024 * 1024, raw={"pc": "code"})
        session = HangingSession(hang_on=("move",))
        session.dirs["/box"] = [node]
        session.dirs["/动漫"] = []
        previous = anime_jobs._STORAGE_CALL_TIMEOUT
        anime_jobs._STORAGE_CALL_TIMEOUT = 0.3
        tasks = self._task()
        started = time.monotonic()
        try:
            result = run_organize(tasks, session, self._config())
        finally:
            anime_jobs._STORAGE_CALL_TIMEOUT = previous
            session.release.set()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")
        self.assertTrue(result["moves"])

    def test_hanging_ensure_dir_does_not_block_the_round(self):
        session = HangingSession(hang_on=("ensure_dir",))
        video = "佐藤同学 - 03.mkv"
        node = Node("/box/" + video, video, False, 900 * 1024 * 1024, raw={"pc": "code"})
        session.dirs["/box"] = [node]
        session.dirs["/动漫"] = []
        previous = anime_jobs._STORAGE_CALL_TIMEOUT
        anime_jobs._STORAGE_CALL_TIMEOUT = 0.3
        started = time.monotonic()
        try:
            result = run_organize(self._task(), session, self._config())
        finally:
            anime_jobs._STORAGE_CALL_TIMEOUT = previous
            session.release.set()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(session.moves, [])


if __name__ == "__main__":
    unittest.main()
