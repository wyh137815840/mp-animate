"""每日任务：开放平台被 115 风控时，改用 Cookie 通道提交。"""

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

from anime115.anime_dmhy import Torrent  # noqa: E402
from anime115.anime_jobs import JobConfig, run_daily  # noqa: E402
from anime115.anime_names import parse_watchlist  # noqa: E402

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40


class _Blocked:
    """开放平台：配额查询一直超时（115 风控）。"""

    def __init__(self):
        self.added = []

    def check_login(self):
        return True

    def sleep(self, _seconds):
        return None

    def quota(self):
        raise TimeoutError("115 请求超过 45 秒")

    def ensure_dir(self, path):
        return type("Folder", (), {"fileid": "99", "path": path})()

    def add_offline(self, magnet, _fileid):
        self.added.append(magnet)
        return {"state": True, "code": 0, "data": {"wp_path_id": "99"}}

    def list_dir(self, _path):
        return []


class _Cookie:
    """Cookie 通道：能提交。"""

    def __init__(self):
        self.added = []

    def check_login(self):
        return True

    def sleep(self, _seconds):
        return None

    def quota(self):
        return {"data": {"surplus": 3}}

    def ensure_dir(self, path):
        return type("Folder", (), {"fileid": "77", "path": path})()

    def add_offline(self, magnet, _fileid):
        self.added.append(magnet)
        return {"state": True, "code": 0, "data": {"wp_path_id": "77"}}

    def list_dir(self, _path):
        return []


class _LoggedOut:
    """开放平台：access_token 失效，登录检查直接说没登录。"""

    def __init__(self):
        self.added = []

    def check_login(self):
        return False

    def sleep(self, _seconds):
        return None

    def quota(self):
        return {"data": {"surplus": 3}}

    def ensure_dir(self, path):
        return type("Folder", (), {"fileid": "99", "path": path})()

    def add_offline(self, magnet, _fileid):
        self.added.append(magnet)
        return {"state": True, "code": 0, "data": {"wp_path_id": "99"}}

    def list_dir(self, _path):
        return []


class FairOrderTest(unittest.TestCase):
    """候选按剧轮流排，剧多了也不会全被前面那几部占满额度。"""

    def test_round_robin_by_show(self):
        from anime115.anime_jobs import _fair_order

        class _Pick:
            def __init__(self, show):
                self.show = show

        picks = [_Pick("A")] * 5 + [_Pick("B")] * 2 + [_Pick("C")]
        ordered = [pick.show for pick in _fair_order(picks)]
        self.assertEqual(ordered[:3], ["A", "B", "C"])
        self.assertEqual(ordered[3:5], ["A", "B"])
        self.assertEqual(len(ordered), 8)


class DailyFallbackTest(unittest.TestCase):
    def _torrent(self):
        return Torrent("佐藤同学 - 03 [1080p][简体]", HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW)

    def test_falls_back_to_cookie_when_quota_times_out(self):
        show = parse_watchlist("佐藤同学")[0]
        blocked = _Blocked()
        cookie = _Cookie()
        result = run_daily([show], blocked, lambda _show: [self._torrent()], {}, NOW,
                           JobConfig(dry_run=False), fallback_session=cookie)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["submitted"]), 1)
        self.assertEqual(len(cookie.added), 1)
        self.assertEqual(blocked.added, [])

    def test_falls_back_to_cookie_when_open_platform_logged_out(self):
        """开放平台 refresh_token 失效时也要能用 Cookie 通道提交，不能整轮不做。"""
        show = parse_watchlist("佐藤同学")[0]
        out = _LoggedOut()
        cookie = _Cookie()
        result = run_daily([show], out, lambda _show: [self._torrent()], {}, NOW,
                           JobConfig(dry_run=False), fallback_session=cookie)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["submitted"]), 1)
        self.assertEqual(len(cookie.added), 1)
        self.assertEqual(out.added, [])

    def test_without_fallback_reports_error(self):
        show = parse_watchlist("佐藤同学")[0]
        blocked = _Blocked()
        result = run_daily([show], blocked, lambda _show: [self._torrent()], {}, NOW,
                           JobConfig(dry_run=False))
        self.assertFalse(result["ok"])
        self.assertIn("配额查询超时", result["error"])


class SeenKeyTest(unittest.TestCase):
    """整包的「已经提交过」按整段记；之前那份是内封、这次是外挂的，放行。"""

    def _pick(self, title, infohash, first=1, second=24):
        from anime115.anime_dmhy import Pick
        from anime115.anime_names import parse_watchlist

        show = parse_watchlist("杜鹃的婚约")[0]
        torrent = Torrent(title, infohash, f"magnet:?xt=urn:btih:{infohash}", 10 * 1024 ** 3, NOW)
        return Pick(show.keyword, 1, first, torrent, 100, pack=(first, second))

    def test_pack_with_external_sub_is_not_blocked(self):
        from anime115.anime_jobs import _episode_blocked, _remember

        seen = {}
        embedded = self._pick("【喵萌奶茶屋】[杜鹃的婚约][01-24][BDRip][1080p][简体]", "a" * 40)
        _remember(seen, embedded, NOW, "/离线下载/动漫/20261113")
        external = self._pick("【喵萌奶茶屋】[杜鹃的婚约][01-24][BDRip][1080p][简繁外挂]", "b" * 40)
        self.assertFalse(_episode_blocked(seen, external, NOW))

    def test_same_pack_without_external_sub_stays_blocked(self):
        from anime115.anime_jobs import _episode_blocked, _remember

        seen = {}
        first = self._pick("【喵萌奶茶屋】[杜鹃的婚约][01-24][BDRip][1080p][简体]", "a" * 40)
        _remember(seen, first, NOW, "/离线下载/动漫/20261113")
        again = self._pick("【喵萌奶茶屋】[杜鹃的婚约][01-24][BDRip][1080p][繁体]", "c" * 40)
        self.assertTrue(_episode_blocked(seen, again, NOW))

    def test_single_episode_still_blocked(self):
        from anime115.anime_jobs import _episode_blocked, _remember

        seen = {}
        one = self._pick("【喵萌奶茶屋】[杜鹃的婚约][05][1080p][简体]", "a" * 40, 5, 0)
        _remember(seen, one, NOW, "/离线下载/动漫/20261113")
        other = self._pick("【喵萌奶茶屋】[杜鹃的婚约][05][1080p][繁體]", "d" * 40, 5, 0)
        self.assertTrue(_episode_blocked(seen, other, NOW))


if __name__ == "__main__":
    unittest.main()
