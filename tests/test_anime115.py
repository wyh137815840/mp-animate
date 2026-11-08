"""动漫 115 的纯规则测试。不连动漫花园，也不连 115。"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import ProxyHandler

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
_pkg = types.ModuleType("anime115")
_pkg.__path__ = [str(_ROOT)]
_pkg.__package__ = "anime115"
sys.modules["anime115"] = _pkg

from anime115.anime_dmhy import (  # noqa: E402
    SearchOptions,
    Torrent,
    merge_sizes,
    moviepilot_proxy,
    normalize_proxy,
    parse_list_sizes,
    parse_rss,
    rss_opener,
    select_one,
)
from anime115.anime_flatten import Node, apply_proposals, propose  # noqa: E402
from anime115.anime_jobs import JobConfig, run_daily, run_flatten, run_organize  # noqa: E402
from anime115.anime_config import PLAY_BASE, offline_directory, usable_cookie  # noqa: E402
from anime115.anime_jobs import _finished_hashes  # noqa: E402
from anime115.anime_names import parse_episode, parse_watchlist, strip_mount_prefix, title_matches  # noqa: E402
from anime115.anime_pan import CookieSession, Pan115  # noqa: E402
from anime115.anime_tmdb import match_tv_id, progress_from_season, resolve_tv_id  # noqa: E402


NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40


class NamesTest(unittest.TestCase):
    def test_watchlist_and_prefix(self):
        shows = parse_watchlist("冷淡的佐藤同学只对我撒娇 | 佐藤同学 | group=喵萌奶茶屋\n# 注释\n")
        self.assertEqual(shows[0].keyword, "冷淡的佐藤同学只对我撒娇")
        self.assertEqual(shows[0].aliases, ["佐藤同学"])
        self.assertEqual(shows[0].groups, ["喵萌奶茶屋"])
        self.assertEqual(strip_mount_prefix("/115open/离线下载/动漫", "/115open"), "/离线下载/动漫")

    def test_episode(self):
        self.assertEqual(parse_episode("冷淡的佐藤同学只对我撒娇 - 03 [1080p]"), (1, 3))
        self.assertEqual(parse_episode("Show S02E04"), (2, 4))
        self.assertIsNone(parse_episode("Show OVA"))
        self.assertIsNone(parse_episode("Show - 03 - 04"))
        self.assertIsNone(parse_episode("Show [1080P][8bit]"))
        girlfriend = "【今晚月色真美】[女友的朋友 / 彼女の友達 / Kanojo no Tomodachi][05][WebRip][1080P][AVC-8bit][AAC]"
        self.assertEqual(parse_episode(girlfriend), (1, 5))
        self.assertEqual(parse_episode("【今晚月色真美】[女友的朋友][01v2][WebRip][1080P]"), (1, 1))
        self.assertIsNone(parse_episode("女友的朋友 - 05 [06]"))

    def test_watchlist_tmdb_and_airing(self):
        shows = parse_watchlist("女友的朋友 | 彼女の友達 | tmdb_id=336654 | 连载中 | group=今晚月色真美\n")
        self.assertEqual(shows[0].tmdb_id, 336654)
        self.assertTrue(shows[0].airing)
        self.assertEqual(shows[0].aliases, ["彼女の友達"])
        self.assertEqual(shows[0].groups, ["今晚月色真美"])

    def test_magical_explorer_traditional_title_matches(self):
        show = parse_watchlist("魔法★探险家 | マジカル★エクスプローラー | Magical Explorer | tmdb_id=335194 | 连载中")[0]
        ani = "[ANi] Magical Explorer /  魔法★探險家 - 07 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"
        loli = "[LoliHouse] 魔法★探险家 / Magical Explorer - 06 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
        self.assertTrue(title_matches(ani, show))
        self.assertTrue(title_matches(loli, show))
        keyword_only = parse_watchlist("魔法★探险家 | tmdb_id=335194 | 连载中")[0]
        self.assertTrue(title_matches(ani, keyword_only))
        self.assertTrue(title_matches(loli, keyword_only))
        self.assertFalse(title_matches("[ANi] 别的节目 - 01 [1080P]", keyword_only))


class ScoreTest(unittest.TestCase):
    def test_prefers_fansub_and_drops_batch(self):
        show = parse_watchlist("佐藤同学 | group=喵萌")[0]
        options = SearchOptions(fansub_prefer=[])
        good = Torrent(f"[喵萌] 佐藤同学 - 03 [1080p][简体]", HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW)
        raw = Torrent("佐藤同学 - 03 RAW", "b" * 40, "magnet:?xt=urn:btih:" + "b" * 40, 900 * 1024 * 1024, NOW)
        batch = Torrent("佐藤同学 全集 [1080p]", "c" * 40, "magnet:?xt=urn:btih:" + "c" * 40, 900 * 1024 * 1024, NOW)
        picks, notes = select_one(show, [good, raw, batch], options, NOW)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].score, 170)
        self.assertEqual({item["reason"] for item in notes}, {"生肉", "合集"})

    def test_rss_item(self):
        xml = """<?xml version="1.0"?>
        <rss><channel><item>
          <title>佐藤同学 - 03</title>
          <pubDate>Sun, 08 Nov 2026 01:00:00 GMT</pubDate>
          <enclosure url="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" length="900000000"/>
        </item></channel></rss>"""
        torrents = parse_rss(xml)
        self.assertEqual(torrents[0].infohash, HASH)
        self.assertEqual(torrents[0].size, 900000000)

    def test_rss_base32_btih(self):
        xml = """<?xml version="1.0"?>
        <rss><channel>
          <item>
            <title>佐藤同学 - 03</title>
            <enclosure url="magnet:?xt=urn:btih:J4Q3TVSKPLVJYLI3NP5EUNXWP736FSBF&amp;dn=show"/>
          </item>
          <item>
            <title>佐藤同学 - 04</title>
            <enclosure url="magnet:?xt=urn:btih:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"/>
          </item>
        </channel></rss>"""
        torrents = parse_rss(xml)
        self.assertEqual(
            [item.infohash for item in torrents],
            ["4f21b9d64a7aea9c2d1b6bfa4a36f67ff7e2c825", "b" * 40],
        )

    def test_rss_opener_uses_proxy_then_direct(self):
        proxied = rss_opener("http://proxy.example:7900")
        handlers = [item for item in proxied.handlers if isinstance(item, ProxyHandler)]
        self.assertEqual(handlers[0].proxies.get("https"), "http://proxy.example:7900")
        self.assertEqual(handlers[0].proxies.get("http"), "http://proxy.example:7900")
        direct = rss_opener("")
        direct_handlers = [item for item in direct.handlers if isinstance(item, ProxyHandler)]
        self.assertTrue(all(not item.proxies for item in direct_handlers))
        self.assertEqual(normalize_proxy("proxy.example:7900"), "http://proxy.example:7900")
        self.assertEqual(moviepilot_proxy(), "")

    def test_rss_placeholder_length_uses_labeled_size(self):
        xml = """<?xml version="1.0"?>
        <rss><channel>
          <item>
            <title>[ANi] 魔法★探险家 - 07 [1080P]</title>
            <description>Overall bit rate : 2 561 kb/s
File size : 435 MiB</description>
            <enclosure url="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" length="1"/>
          </item>
          <item>
            <title>佐藤同学 - 04</title>
            <description>檔案大小：1.2GB</description>
            <enclosure url="magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" length="1"/>
          </item>
          <item>
            <title>佐藤同学 - 03</title>
            <description>Overall bit rate : 2 561 kb/s</description>
            <enclosure url="magnet:?xt=urn:btih:cccccccccccccccccccccccccccccccccccccccc" length="1"/>
          </item>
        </channel></rss>"""
        torrents = parse_rss(xml)
        self.assertEqual(
            [item.size for item in torrents],
            [435 * 1024 ** 2, int(round(1.2 * 1024 ** 3)), 0],
        )

    def test_list_page_fills_missing_size(self):
        html = """
        <table>
          <tr class="">
            <td class="title"><a href="/topics/view/1.html">【今晚月色真美】女友的朋友 [05] 8Gb</a></td>
            <td nowrap="nowrap" align="center">
              <a href="magnet:?xt=urn:btih:J4Q3TVSKPLVJYLI3NP5EUNXWP736FSBF&amp;dn=">&nbsp;</a>
            </td>
            <td nowrap="nowrap" align="center">121.5MB</td>
            <td nowrap="nowrap" align="center"><span class="btl_1">20</span></td>
            <td nowrap="nowrap" align="center">1919</td>
          </tr>
          <tr>
            <td nowrap="nowrap" align="center">
              <a href="magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"></a>
            </td>
            <td nowrap="nowrap" align="center">9.5GB</td>
          </tr>
        </table>
        """
        infohash = "4f21b9d64a7aea9c2d1b6bfa4a36f67ff7e2c825"
        sizes = parse_list_sizes(html)
        self.assertEqual(sizes[infohash], int(round(121.5 * 1024 ** 2)))
        self.assertEqual(sizes["b" * 40], int(round(9.5 * 1024 ** 3)))
        missing = Torrent("女友的朋友 - 05", infohash, "magnet:?xt=urn:btih:" + infohash, 0, NOW)
        kept = Torrent("佐藤同学 - 03", HASH, "magnet:?xt=urn:btih:" + HASH, 900 * 1024 * 1024, NOW)
        merge_sizes([missing, kept], sizes)
        self.assertEqual(missing.size, sizes[infohash])
        self.assertEqual(kept.size, 900 * 1024 * 1024)

    def test_size_floor_and_cap(self):
        show = parse_watchlist("魔法★探险家")[0]
        options = SearchOptions(min_episode_mb=20, max_episode_gb=8, require_sub=False)

        def item(title, size, digest):
            return Torrent(title, digest, "magnet:?xt=urn:btih:" + digest, size, NOW)

        real = item("魔法★探险家 - 07 [1080p]", 435 * 1024 ** 2, "d" * 40)
        webrip = item("魔法★探险家 - 06 [1080p]", int(round(121.5 * 1024 ** 2)), "e" * 40)
        tiny = item("魔法★探险家 - 05 [1080p]", 15 * 1024 ** 2, "f" * 40)
        huge = item("魔法★探险家 - 04 [1080p]", int(round(9.5 * 1024 ** 3)), "1" * 40)
        unknown = item("魔法★探险家 - 03 [1080p]", 0, "2" * 40)
        picks, notes = select_one(show, [real, webrip, tiny, huge, unknown], options, NOW)
        self.assertEqual({pick.episode for pick in picks}, {7, 6, 3})
        reasons = {note["title"]: note["reason"] for note in notes}
        self.assertEqual(reasons[tiny.title], "体积过小")
        self.assertEqual(reasons[huge.title], "体积过大")

    def test_bracket_episode_and_airing_cap(self):
        show = parse_watchlist("女友的朋友 | tmdb_id=336654 | 连载中")[0]
        options = SearchOptions(
            min_episode_mb=20,
            max_episode_gb=8,
            require_sub=False,
            bounds_of=lambda _show, _season: (12, 5),
        )
        ready = "【今晚月色真美】[女友的朋友][05][WebRip][1080P]"
        future = "【今晚月色真美】[女友的朋友][06][WebRip][1080P]"
        beyond = "【今晚月色真美】[女友的朋友][14][WebRip][1080P]"

        def item(title, digest):
            return Torrent(title, digest, "magnet:?xt=urn:btih:" + digest, int(round(121.5 * 1024 ** 2)), NOW)

        picks, notes = select_one(show, [item(ready, "d" * 40), item(future, "e" * 40), item(beyond, "f" * 40)], options, NOW)
        self.assertEqual([pick.episode for pick in picks], [5])
        reasons = {note["title"]: note["reason"] for note in notes}
        self.assertEqual(reasons[future], "尚未播出")
        self.assertEqual(reasons[beyond], "超出季集数")


class FlattenTest(unittest.TestCase):
    def test_same_name_lift_and_collision_gate(self):
        nodes = [
            Node("/inbox/Show", "Show", True),
            Node("/inbox/Show/[喵] Show", "[喵] Show", True),
            Node("/inbox/Show/[喵] Show/E01.mkv", "E01.mkv", False, 900 * 1024 * 1024),
        ]
        proposals = propose(nodes, ["/inbox"], 80 * 1024 * 1024)
        self.assertEqual(proposals[0].kind, "ok_eligible_non_collision")
        self.assertEqual(proposals[0].dest, "/inbox/Show/E01.mkv")
        blocked = proposals + []
        blocked[0].kind = "collision"
        self.assertEqual(apply_proposals(blocked, None, True, 30), 0)

    def test_existing_dest_is_collision(self):
        nodes = [
            Node("/show", "show", True),
            Node("/show/extra", "extra", True),
            Node("/show/extra/E01.mkv", "E01.mkv", False, 900 * 1024 * 1024),
            Node("/show/Season 01", "Season 01", True),
            Node("/show/Season 01/E01.mkv", "E01.mkv", False, 900 * 1024 * 1024),
        ]
        proposals = propose(nodes, ["/show"], 80 * 1024 * 1024)
        self.assertTrue(any(item.kind == "collision" for item in proposals))
        self.assertFalse(any(item.kind == "ok_eligible_non_collision" for item in proposals))


class JobTest(unittest.TestCase):
    def test_daily_dry_run_does_not_submit(self):
        show = parse_watchlist("佐藤同学")[0]
        torrent = Torrent("佐藤同学 - 03 [1080p][简体]", HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW)
        session = _Fake()
        result = run_daily([show], session, lambda _show: [torrent], {}, NOW, JobConfig(dry_run=True))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["picks"][0]["episode"], 3)
        self.assertEqual(session.added, [])

    def test_daily_submit_uses_session(self):
        show = parse_watchlist("佐藤同学")[0]
        torrent = Torrent("佐藤同学 - 03 [1080p][简体]", HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW)
        session = _Fake()
        seen = {}
        result = run_daily([show], session, lambda _show: [torrent], seen, NOW, JobConfig(dry_run=False))
        self.assertEqual(len(result["submitted"]), 1)
        self.assertEqual(session.added, [HASH])
        self.assertIn(HASH, seen["hashes"])
        again = run_daily([show], session, lambda _show: [torrent], seen, NOW, JobConfig(dry_run=False))
        self.assertEqual(again["submitted"], [])

    def test_old_failure_can_be_replaced(self):
        show = parse_watchlist("佐藤同学")[0]
        torrent = Torrent("佐藤同学 - 03 [1080p]", HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW)
        seen = {
            "hashes": {"b" * 40: {"at": (NOW - timedelta(days=4)).isoformat(), "status": "submitted"}},
            "episodes": {"佐藤同学|1|3": "b" * 40},
        }
        result = run_daily([show], _Fake(), lambda _show: [torrent], seen, NOW, JobConfig(dry_run=False))
        self.assertEqual(len(result["submitted"]), 1)

    def test_organize_single_episode(self):
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2}]
        session.dirs["/离线下载/动漫/20261108"] = [
            Node("/离线下载/动漫/20261108/E03.mkv", "E03.mkv", False, 900 * 1024 * 1024),
            Node("/离线下载/动漫/20261108/E03.ass", "E03.ass", False, 1000),
        ]
        tasks = [{
            "infohash": HASH,
            "show": "佐藤同学",
            "season": 1,
            "episode": 3,
            "title": "佐藤同学 - 03",
            "folder": "/离线下载/动漫/20261108",
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "move")
        self.assertEqual(result["moves"][0]["dest_dir"], "/动漫/佐藤同学/Season 01")
        self.assertEqual(tasks[0]["status"], "moved")
        self.assertEqual(len(session.moves), 2)

    def test_organize_two_videos_stay(self):
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2}]
        session.dirs["/box"] = [
            Node("/box/E01.mkv", "E01.mkv", False, 900 * 1024 * 1024),
            Node("/box/E02.mkv", "E02.mkv", False, 900 * 1024 * 1024),
        ]
        tasks = [{"infohash": HASH, "show": "佐藤同学", "season": 1, "episode": 1, "title": "x", "folder": "/box", "status": "submitted"}]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["reason"], "uncertain")
        self.assertEqual(session.moves, [])

    def test_flatten_does_nothing_when_all_collision(self):
        session = _Fake()
        session.dirs["/inbox"] = [Node("/inbox/show", "show", True)]
        session.dirs["/inbox/show"] = [
            Node("/inbox/show/extra", "extra", True),
            Node("/inbox/show/Season 01", "Season 01", True),
        ]
        session.dirs["/inbox/show/extra"] = [Node("/inbox/show/extra/E01.mkv", "E01.mkv", False, 900 * 1024 * 1024)]
        session.dirs["/inbox/show/Season 01"] = [Node("/inbox/show/Season 01/E01.mkv", "E01.mkv", False, 900 * 1024 * 1024)]
        result = run_flatten(session, JobConfig(dry_run=False, flatten_apply=True, flatten_roots=["/inbox"]))
        self.assertEqual(result["counts"]["ok_eligible_non_collision"], 0)
        self.assertEqual(result["moved"], 0)
        self.assertEqual(session.moves, [])


class TmdbTest(unittest.TestCase):
    def test_season_progress_uses_air_dates(self):
        payload = {
            "episodes": [
                {"episode_number": number, "air_date": f"2026-10-{number:02d}"}
                for number in range(1, 6)
            ] + [
                {"episode_number": 6, "air_date": "2026-11-09"},
                {"episode_number": 12, "air_date": "2026-12-21"},
            ]
        }
        progress = progress_from_season(payload, date(2026, 11, 8))
        self.assertEqual(progress.episode_count, 12)
        self.assertEqual(progress.last_aired, 5)

    def test_name_search_requires_exact_title(self):
        results = [
            {"id": 1, "name": "别的朋友", "original_name": "Other"},
            {"id": 336654, "name": "女友的朋友", "original_name": "彼女の友達"},
        ]
        self.assertEqual(match_tv_id(results, ["女友的朋友"]), 336654)
        self.assertIsNone(match_tv_id(results, ["朋友"]))
        show = parse_watchlist("女友的朋友 | 连载中")[0]
        self.assertEqual(resolve_tv_id(show, lambda _name: results), 336654)
        pinned = parse_watchlist("女友的朋友 | tmdb_id=316891")[0]
        self.assertEqual(resolve_tv_id(pinned, lambda _name: []), 316891)


class ConfigTest(unittest.TestCase):
    def test_cookie_and_offline_dir_follow_av_names(self):
        self.assertEqual(usable_cookie("UID=1; CID=2; SEID=3; KID=4"), "UID=1; CID=2; SEID=3; KID=4")
        self.assertEqual(usable_cookie("not a cookie"), "")
        self.assertEqual(usable_cookie(""), "")
        self.assertEqual(offline_directory({"offline_dir": "/离线下载/动漫"}), "/离线下载/动漫")
        self.assertEqual(offline_directory({"offline_root": "/旧目录"}), "/旧目录")
        self.assertEqual(offline_directory({}), "/离线下载/动漫")
        self.assertIn("pickcode={pickcode}", PLAY_BASE)
        self.assertNotIn("127.0.0.1", PLAY_BASE)


class CookiePanTest(unittest.TestCase):
    def test_cookie_session_lists_moves_and_adds_without_open_api(self):
        pan = _FakePan()
        session = CookieSession("UID=9_1; CID=2; SEID=3; KID=4", None, pan)
        self.assertTrue(session.check_login())
        folder = session.ensure_dir("/离线下载/动漫")
        self.assertEqual(folder.fileid, "11")
        nodes = session.list_dir("/离线下载/动漫")
        self.assertEqual(nodes[0].name, "E01.mkv")
        self.assertTrue(session.move(nodes[0], "/动漫/佐藤同学/Season 01", "E01.mkv"))
        self.assertEqual(pan.moved, [("81", "21")])
        added = session.add_offline("magnet:?xt=urn:btih:" + "a" * 40, "11")
        self.assertTrue(added["state"])
        self.assertEqual(pan.client.added["wp_path_id"], "11")
        self.assertEqual(_finished_hashes(session), {"ab" * 20})
        self.assertTrue(session.delete_dir("/离线下载/动漫/empty"))
        self.assertEqual(pan.deleted, [("31", "11")])
        self.assertEqual(session.quota(), {"data": {}})

    def test_directory_walk_uses_aps_and_app_folder_api(self):
        http = _ScriptedHttp([
            ("0", [{"cid": "10", "n": "离线下载", "pid": "0"}]),
            ("10", [{"cid": "11", "n": "动漫", "pid": "10"}]),
        ])
        pan = Pan115("UID=9_1; CID=2", http=http, gap=0, client_factory=lambda _cookie: _FakeClient())
        self.assertEqual(pan.ensure_dir("/离线下载/动漫"), "11")
        self.assertTrue(any(call[1].startswith("https://aps.115.com/natsort/files.php") for call in http.calls))
        self.assertFalse(any("/open/" in call[1] for call in http.calls))


class ImportTest(unittest.TestCase):
    def test_package_import_leaves_no_bare_module(self):
        app = types.ModuleType("app")
        app.__path__ = []
        chain = types.ModuleType("app.chain")
        chain.__path__ = []
        storage = types.ModuleType("app.chain.storage")

        class StorageChain:
            pass

        storage.StorageChain = StorageChain
        log = types.ModuleType("app.log")
        log.logger = types.SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None, error=lambda *_a, **_k: None)
        plugins = types.ModuleType("app.plugins")
        plugins.__path__ = []

        class _PluginBase:
            pass

        plugins._PluginBase = _PluginBase
        stale = types.ModuleType("anime_dmhy")
        sys.modules["anime_dmhy"] = stale
        for name, module in (
            ("app", app),
            ("app.chain", chain),
            ("app.chain.storage", storage),
            ("app.log", log),
            ("app.plugins", plugins),
        ):
            sys.modules.setdefault(name, module)
        spec = importlib.util.spec_from_file_location(
            "app.plugins.anime115",
            _ROOT / "__init__.py",
            submodule_search_locations=[str(_ROOT)],
        )
        plugin = importlib.util.module_from_spec(spec)
        sys.modules["app.plugins.anime115"] = plugin
        spec.loader.exec_module(plugin)
        self.assertEqual(plugin.Anime115.plugin_version, "1.0.8")
        self.assertIs(sys.modules["anime_dmhy"], stale)
        self.assertIsNot(plugin.parse_rss, getattr(stale, "parse_rss", None))
        for name in ("anime_names", "anime_jobs", "anime_flatten", "anime_tmdb", "anime_config", "anime_pan"):
            self.assertNotIn(name, sys.modules)
        for name in ("anime_dmhy", "anime_names", "anime_jobs", "anime_flatten", "anime_tmdb", "anime_config", "anime_pan"):
            self.assertIn(f"app.plugins.anime115.{name}", sys.modules)
        _form, defaults = plugin.Anime115.get_form(None)
        for key in ("cookie", "offline_dir", "strm_root", "play_base", "storage"):
            self.assertIn(key, defaults)
        for key in ("javbus_host", "proxy", "rename_folder", "overwrite", "refresh_emby", "emby_library"):
            self.assertNotIn(key, defaults)
        self.assertIs(plugin.parse_rss, sys.modules["app.plugins.anime115.anime_dmhy"].parse_rss)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _ScriptedHttp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        cid = str((kwargs.get("params") or {}).get("cid") or "")
        for key, rows in self.pages:
            if key == cid:
                return _Resp({"state": True, "data": rows})
        return _Resp({"state": True, "data": []})


class _FakeClient:
    def __init__(self):
        self.added = {}

    def clouddownload_task_add_url(self, payload):
        self.added = payload
        return {"state": True, "code": 0, "data": {"wp_path_id": payload["wp_path_id"]}}

    def clouddownload_task_list(self, _payload):
        return {"tasks": [{"info_hash": "ab" * 20, "status": 2}]}


class _FakePan:
    def __init__(self):
        self.moved = []
        self.deleted = []
        self.client = _FakeClient()

    def ensure_dir(self, path):
        if path.endswith("Season 01"):
            return "21"
        if path.endswith("empty"):
            return "31"
        return "11"

    def dir_id(self, path):
        if path.endswith("empty"):
            return "31"
        if path.rstrip("/").endswith("动漫"):
            return "11"
        return "10"

    def list_children(self, directory_id):
        if directory_id == "11":
            return [{"fid": "81", "fc": "1", "fn": "E01.mkv", "fs": 900 * 1024 * 1024, "pid": "11"}]
        return []

    def rename(self, _file_id, _new_name):
        return None

    def move(self, file_id, directory_id):
        self.moved.append((file_id, directory_id))

    def delete(self, file_id, parent_id):
        self.deleted.append((file_id, parent_id))

    def add_magnet(self, magnet, directory_id):
        return self.client.clouddownload_task_add_url({"url": magnet, "wp_path_id": directory_id})

    def list_tasks(self, _page):
        return {"data": {"tasks": self.client.clouddownload_task_list({})["tasks"]}}


class _Fake:
    def __init__(self):
        self.added = []
        self.moves = []
        self.dirs = {}
        self.offline = []
        self.deleted = []

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

    def list_offline(self, _page):
        return {"data": {"tasks": self.offline}}

    def list_dir(self, path):
        return list(self.dirs.get(path, []))

    def move(self, node, dest_dir, new_name):
        self.moves.append((node.path, dest_dir, new_name))
        return True

    def delete_dir(self, path):
        self.deleted.append(path)
        return True


if __name__ == "__main__":
    unittest.main()
