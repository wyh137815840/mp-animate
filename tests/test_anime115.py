"""动漫 115 的纯规则测试。不连动漫花园，也不连 115。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import threading
import time
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
    score_torrent,
    select_one,
)
from anime115.anime_flatten import Node, apply_proposals, propose  # noqa: E402
from anime115.anime_jobs import JobConfig, resolve_show_folder, run_daily, run_flatten, run_organize, run_strm  # noqa: E402
from anime115.anime_strm import build_play_url, episode_strm_name  # noqa: E402
from anime115.anime_config import (  # noqa: E402
    PLAY_BASE,
    PLAY_BASE_LAN,
    offline_directory,
    organize_schedule,
    scheduled_jobs,
    usable_cookie,
)
from anime115.anime_jobs import _finished_hashes  # noqa: E402
from anime115.anime_names import parse_episode, parse_watchlist, strip_mount_prefix, title_matches  # noqa: E402
from anime115.anime_pan import CookieSession, Pan115  # noqa: E402
from anime115.anime_progress import advance, format_progress, format_state  # noqa: E402
from anime115.anime_subscribe import shows_from_subscriptions  # noqa: E402
from anime115.anime_tmdb import genre_ids_in, match_tv_id, progress_from_season, resolve_tv_id  # noqa: E402


NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 40


def tasks_for_lan(video: str) -> list[dict]:
    return [{
        "infohash": HASH,
        "show": "冷淡的佐藤同学只对我撒娇",
        "season": 1,
        "episode": 5,
        "title": video,
        "folder": "/box/loli",
        "status": "submitted",
    }]


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

    def test_1080_floor_keeps_4k_and_drops_720(self):
        show = parse_watchlist("佐藤同学")[0]
        options = SearchOptions(resolution="1080")
        uhd = Torrent(
            "[Shiniori-Raws] 佐藤同学 - 03 [4K 2160p][简体]",
            HASH,
            f"magnet:?xt=urn:btih:{HASH}",
            2 * 1024 ** 3,
            NOW,
        )
        hd = Torrent("佐藤同学 - 03 [1080p][简体]", "b" * 40, "magnet:?xt=urn:btih:" + "b" * 40, 900 * 1024 * 1024, NOW)
        low = Torrent("佐藤同学 - 03 [720p][简体]", "c" * 40, "magnet:?xt=urn:btih:" + "c" * 40, 400 * 1024 * 1024, NOW)
        plain = Torrent("佐藤同学 - 04 [简体]", "d" * 40, "magnet:?xt=urn:btih:" + "d" * 40, 900 * 1024 * 1024, NOW)
        other = Torrent("[Shiniori-Raws] 别的节目 - 03 [4k 2160p]", "e" * 40, "magnet:?xt=urn:btih:" + "e" * 40, 2 * 1024 ** 3, NOW)
        batch = Torrent("佐藤同学 BDRip 全集 [2160p]", "f" * 40, "magnet:?xt=urn:btih:" + "f" * 40, 2 * 1024 ** 3, NOW)
        kept, why = score_torrent(show, uhd, options, NOW)
        self.assertIsNotNone(kept)
        self.assertEqual(why, "")
        picks, notes = select_one(show, [uhd, hd, low, plain, other, batch], options, NOW)
        by_episode = {item.episode: item.torrent.title for item in picks}
        self.assertEqual(by_episode[3], hd.title)
        self.assertEqual(by_episode[4], plain.title)
        reasons = {item["title"]: item["reason"] for item in notes}
        self.assertEqual(reasons[low.title], "分辨率不足")
        self.assertEqual(reasons[other.title], "标题对不上")
        self.assertEqual(reasons[batch.title], "合集")
        self.assertNotIn(uhd.title, reasons)
        self.assertNotIn("4K", reasons.values())
        hd_score, _ = score_torrent(show, hd, options, NOW)
        uhd_score, _ = score_torrent(show, uhd, options, NOW)
        self.assertGreater(hd_score, uhd_score)
        only_4k = SearchOptions(resolution="4k")
        kept, _ = score_torrent(show, uhd, only_4k, NOW)
        rejected, why = score_torrent(show, hd, only_4k, NOW)
        self.assertIsNotNone(kept)
        self.assertIsNone(rejected)
        self.assertEqual(why, "4K")

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
            Node("/box/佐藤同学 - 01 [a].mkv", "佐藤同学 - 01 [a].mkv", False, 900 * 1024 * 1024),
            Node("/box/佐藤同学 - 01 [b].mkv", "佐藤同学 - 01 [b].mkv", False, 900 * 1024 * 1024),
        ]
        tasks = [{"infohash": HASH, "show": "佐藤同学", "season": 1, "episode": 1, "title": "x", "folder": "/box", "status": "submitted"}]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertTrue(result["moves"][0]["reason"].startswith("不止一个视频："))
        self.assertIn("佐藤同学 - 01 [a].mkv", result["moves"][0]["reason"])
        self.assertIn("佐藤同学 - 01 [b].mkv", result["moves"][0]["reason"])
        self.assertEqual(session.moves, [])

    def test_organize_unwraps_same_name_pack(self):
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2}, {"info_hash": "b" * 40, "status": 2}]
        pack6 = "/inbox/魔法★探险家 - 06.mkv"
        pack7 = "/inbox/魔法★探险家 - 07.mkv"
        video6 = "魔法★探险家 - 06 [WebRip].mkv"
        sub6 = "魔法★探险家 - 06 [WebRip].ass"
        video7 = "魔法★探险家 - 07 [WebRip].mkv"
        session.dirs["/inbox"] = [
            Node(pack6, "魔法★探险家 - 06.mkv", True),
            Node(pack7, "魔法★探险家 - 07.mkv", True),
        ]
        session.dirs[pack6] = [
            Node(pack6 + "/" + video6, video6, False, 900 * 1024 * 1024),
            Node(pack6 + "/" + sub6, sub6, False, 1000),
        ]
        session.dirs[pack7] = [
            Node(pack7 + "/" + video7, video7, False, 900 * 1024 * 1024),
        ]
        tasks = [{
            "infohash": HASH,
            "show": "魔法★探险家",
            "season": 1,
            "episode": 6,
            "title": "魔法★探险家 - 06",
            "folder": "/inbox",
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "move")
        self.assertEqual(result["moves"][0]["name"], video6)
        self.assertEqual(result["moves"][0]["dest_dir"], "/动漫/魔法★探险家/Season 01")
        moved_names = [item[2] for item in session.moves]
        self.assertEqual(moved_names, [video6, sub6])

    def test_aps_video_is_not_a_directory(self):
        from anime115.anime_pan import _from_aps, _is_directory

        folder = _from_aps({"cid": "10", "fid": "", "fc": 0, "n": "魔法★探险家 - 06.mkv", "pid": "1"}, "1")
        video = _from_aps({"fid": "81", "cid": "10", "fc": 1, "n": "魔法★探险家 - 06.mkv", "s": 900, "pick_code": "magiccode"}, "10")
        season = _from_aps({"fid": "30", "cid": "20", "pid": "20", "fc": 0, "n": "Season 02", "pc": "seasonpick"}, "20")
        self.assertTrue(_is_directory(folder))
        self.assertEqual(folder["fid"], "10")
        self.assertFalse(_is_directory(video))
        self.assertEqual(video["fn"], "魔法★探险家 - 06.mkv")
        self.assertEqual(video["fid"], "81")
        self.assertEqual(video["pc"], "magiccode")
        self.assertTrue(_is_directory(season))
        self.assertEqual(season["fid"], "30")
        self.assertEqual(season["pc"], "seasonpick")

    def test_show_folder_prefers_exact_then_unique_year(self):
        names = ["冷淡的佐藤同学只对我撒娇 (2026)", "魔法★探险家 (2026)", "冷淡的佐藤同学只对我撒娇"]
        self.assertEqual(resolve_show_folder(names, "冷淡的佐藤同学只对我撒娇"), ("冷淡的佐藤同学只对我撒娇", ""))
        self.assertEqual(resolve_show_folder(["魔法★探险家 (2026)"], "魔法★探险家"), ("魔法★探险家 (2026)", ""))
        self.assertEqual(
            resolve_show_folder(["魔法★探险家 (2025)", "魔法★探险家 (2026)"], "魔法★探险家"),
            ("", "剧名目录不唯一"),
        )
        self.assertEqual(resolve_show_folder([], "新番"), ("新番", ""))

    def test_organize_reuses_year_folder_and_continues_after_create_failure(self):
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2}, {"info_hash": "b" * 40, "status": 2}]
        session.dirs["/动漫"] = [
            Node("/动漫/冷淡的佐藤同学只对我撒娇 (2026)", "冷淡的佐藤同学只对我撒娇 (2026)", True),
            Node("/动漫/魔法★探险家 (2026)", "魔法★探险家 (2026)", True),
        ]
        session.dirs["/inbox"] = [
            Node("/inbox/佐藤同学 - 03 [1080p].mkv", "佐藤同学 - 03 [1080p].mkv", False, 900 * 1024 * 1024),
            Node("/inbox/魔法★探险家 - 06 [1080p].mkv", "魔法★探险家 - 06 [1080p].mkv", False, 900 * 1024 * 1024),
        ]
        session.fail_create = "魔法"

        def ensure_dir(path):
            session.made.append(path)
            if session.fail_create and session.fail_create in path:
                raise RuntimeError("无法创建 115 目录 " + path)
            return type("Folder", (), {"fileid": "99", "path": path})()

        session.ensure_dir = ensure_dir
        session.made = []
        tasks = [
            {"infohash": HASH, "show": "冷淡的佐藤同学只对我撒娇", "season": 1, "episode": 3, "title": "佐藤同学 - 03", "folder": "/inbox", "status": "submitted"},
            {"infohash": "b" * 40, "show": "魔法★探险家", "season": 1, "episode": 6, "title": "魔法★探险家 - 06", "folder": "/inbox", "status": "submitted"},
        ]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["moves"][0]["action"], "move")
        self.assertEqual(result["moves"][0]["dest_dir"], "/动漫/冷淡的佐藤同学只对我撒娇 (2026)/Season 01")
        self.assertEqual(result["moves"][1]["action"], "leave")
        self.assertEqual(result["moves"][1]["reason"], "建不了目标目录")
        self.assertIn("无法创建 115 目录", result["moves"][1]["error"])
        self.assertIn("/动漫/魔法★探险家 (2026)/Season 01", result["moves"][1]["error"])
        self.assertEqual(tasks[0]["status"], "moved")
        self.assertEqual(tasks[1]["status"], "submitted")

    def test_sidecars_are_not_extra_videos(self):
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2}, {"info_hash": "b" * 40, "status": 2}]
        pack = "/inbox/佐藤同学 - 02.mkv"
        other = "/inbox/佐藤同学 - 03.mkv"
        video = "佐藤同学 - 02 [WebRip].mkv"
        session.dirs["/inbox"] = [
            Node(pack, "佐藤同学 - 02.mkv", True),
            Node(other, "佐藤同学 - 03.mkv", True),
        ]
        session.dirs[pack] = [
            Node(f"{pack}/{video}", video, False, 900 * 1024 * 1024),
            Node(f"{pack}/佐藤同学 - 02 [WebRip].ass", "佐藤同学 - 02 [WebRip].ass", False, 2000),
            Node(f"{pack}/佐藤同学 - 02 [WebRip].chs.srt", "佐藤同学 - 02 [WebRip].chs.srt", False, 2000),
            Node(f"{pack}/NCOP.mkv", "NCOP.mkv", False, 80 * 1024 * 1024),
        ]
        session.dirs[other] = [
            Node(f"{other}/佐藤同学 - 03 [WebRip].mkv", "佐藤同学 - 03 [WebRip].mkv", False, 900 * 1024 * 1024),
        ]
        tasks = [{
            "infohash": HASH,
            "show": "佐藤同学",
            "season": 1,
            "episode": 2,
            "title": "佐藤同学 - 02",
            "folder": "/inbox",
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "move")
        self.assertEqual(result["moves"][0]["name"], video)
        self.assertEqual(result["moves"][0]["reason"], "ok")

    def test_mkv_shell_listed_as_file_still_uses_inner_video(self):
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2}]
        shell = "/inbox/[LoliHouse] 佐藤同学 - 02 [WebRip].mkv"
        video = "[LoliHouse] 佐藤同学 - 02 [WebRip].mkv"
        session.dirs["/inbox"] = [
            Node(shell, video, True),
            Node(shell, video, False, 100),
            Node("/inbox/[LoliHouse] 佐藤同学 - 04 [WebRip].mkv", "[LoliHouse] 佐藤同学 - 04 [WebRip].mkv", True),
        ]
        session.dirs[shell] = [
            Node(f"{shell}/{video}", video, False, 900 * 1024 * 1024),
            Node(f"{shell}/[LoliHouse] 佐藤同学 - 02 [WebRip].chs.ass", "[LoliHouse] 佐藤同学 - 02 [WebRip].chs.ass", False, 1000),
            Node(f"{shell}/[LoliHouse] 佐藤同学 - 02 [WebRip].cht.srt", "[LoliHouse] 佐藤同学 - 02 [WebRip].cht.srt", False, 1000),
        ]
        session.dirs["/inbox/[LoliHouse] 佐藤同学 - 04 [WebRip].mkv"] = [
            Node("/inbox/[LoliHouse] 佐藤同学 - 04 [WebRip].mkv/[LoliHouse] 佐藤同学 - 04 [WebRip].mkv", "[LoliHouse] 佐藤同学 - 04 [WebRip].mkv", False, 900 * 1024 * 1024),
        ]
        tasks = [{
            "infohash": HASH,
            "show": "佐藤同学",
            "season": 1,
            "episode": 2,
            "title": "[LoliHouse] 佐藤同学 - 02 [WebRip]",
            "folder": "/inbox",
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "move")
        self.assertEqual(result["moves"][0]["name"], video)
        self.assertNotIn("不止一个视频", result["moves"][0]["reason"])

    def test_same_episode_stays_inside_its_own_pack(self):
        day = "/离线下载/动漫/20261108"
        sato_pack = day + "/[LoliHouse] Shiotaiou no Sato-san - 05 [WebRip].mkv"
        magic_pack = day + "/[LoliHouse] Magical Explorer - 05 [WebRip].mkv"
        sato_video = "Shiotaiou no Sato-san ga Ore ni dake Amaesasete Kureru - 05 [WebRip].mkv"
        magic_video = "Magical Explorer - 05 [WebRip].mkv"
        sato_sub = "Shiotaiou no Sato-san ga Ore ni dake Amaesasete Kureru - 05 [WebRip].chs.srt"
        session = _Fake()
        session.offline = [
            {"info_hash": HASH, "status": 2, "name": "[LoliHouse] Shiotaiou no Sato-san - 05 [WebRip].mkv", "file_id": "sato-pack"},
            {"info_hash": "b" * 40, "status": 2, "name": "[LoliHouse] Magical Explorer - 05 [WebRip].mkv", "file_id": "magic-pack"},
        ]
        session.dirs["/动漫"] = [
            Node("/动漫/冷淡的佐藤同学只对我撒娇 (2026)", "冷淡的佐藤同学只对我撒娇 (2026)", True),
            Node("/动漫/魔法★探险家 (2026)", "魔法★探险家 (2026)", True),
        ]
        session.dirs[day] = [
            Node(sato_pack, "[LoliHouse] Shiotaiou no Sato-san - 05 [WebRip].mkv", True, raw={"fid": "sato-pack"}),
            Node(magic_pack, "[LoliHouse] Magical Explorer - 05 [WebRip].mkv", True, raw={"fid": "magic-pack"}),
        ]
        session.dirs[sato_pack] = [
            Node(f"{sato_pack}/{sato_video}", sato_video, False, 900 * 1024 * 1024, raw={"fid": "sato-file"}),
            Node(f"{sato_pack}/{sato_sub}", sato_sub, False, 1000),
        ]
        session.dirs[magic_pack] = [
            Node(f"{magic_pack}/{magic_video}", magic_video, False, 900 * 1024 * 1024, raw={"fid": "magic-file"}),
        ]
        tasks = [
            {
                "infohash": HASH,
                "show": "冷淡的佐藤同学只对我撒娇",
                "season": 1,
                "episode": 5,
                "title": "[LoliHouse] 冷淡的佐藤同学只对我撒娇 - 05 [WebRip]",
                "folder": day,
                "status": "submitted",
            },
            {
                "infohash": "b" * 40,
                "show": "魔法★探险家",
                "season": 1,
                "episode": 5,
                "title": "[LoliHouse] 魔法★探险家 - 05 [WebRip]",
                "folder": day,
                "status": "submitted",
            },
        ]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "move")
        self.assertEqual(result["moves"][0]["name"], sato_video)
        self.assertEqual(result["moves"][0]["pack"], sato_pack)
        self.assertEqual(result["moves"][0]["dest_dir"], "/动漫/冷淡的佐藤同学只对我撒娇 (2026)/Season 01")
        self.assertEqual(result["moves"][1]["action"], "move")
        self.assertEqual(result["moves"][1]["name"], magic_video)
        self.assertEqual(result["moves"][1]["pack"], magic_pack)
        self.assertEqual(result["moves"][1]["dest_dir"], "/动漫/魔法★探险家 (2026)/Season 01")
        moved_names = [item[2] for item in session.moves]
        self.assertEqual(moved_names, [sato_video, sato_sub, magic_video])
        self.assertNotIn("不止一个视频", result["moves"][0]["reason"])
        self.assertNotIn("不止一个视频", result["moves"][1]["reason"])

    def test_same_episode_without_pack_name_is_not_merged(self):
        day = "/离线下载/动漫/20261108"
        sato_pack = day + "/[LoliHouse] Shiotaiou no Sato-san - 02 [WebRip].mkv"
        magic_pack = day + "/[LoliHouse] Magical Explorer - 02 [WebRip].mkv"
        session = _Fake()
        session.offline = [
            {"info_hash": HASH, "status": 2},
            {"info_hash": "b" * 40, "status": 2},
        ]
        session.dirs[day] = [
            Node(sato_pack, "[LoliHouse] Shiotaiou no Sato-san - 02 [WebRip].mkv", True),
            Node(magic_pack, "[LoliHouse] Magical Explorer - 02 [WebRip].mkv", True),
        ]
        session.dirs[sato_pack] = [
            Node(f"{sato_pack}/Shiotaiou - 02.mkv", "Shiotaiou - 02.mkv", False, 900 * 1024 * 1024),
        ]
        session.dirs[magic_pack] = [
            Node(f"{magic_pack}/Magical Explorer - 02.mkv", "Magical Explorer - 02.mkv", False, 900 * 1024 * 1024),
        ]
        tasks = [
            {
                "infohash": HASH,
                "show": "冷淡的佐藤同学只对我撒娇",
                "season": 1,
                "episode": 2,
                "title": "[LoliHouse] 冷淡的佐藤同学只对我撒娇 - 02 [WebRip]",
                "folder": day,
                "status": "submitted",
            },
            {
                "infohash": "b" * 40,
                "show": "魔法★探险家",
                "season": 1,
                "episode": 2,
                "title": "[LoliHouse] 魔法★探险家 - 02 [WebRip]",
                "folder": day,
                "status": "submitted",
            },
        ]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertTrue(result["moves"][0]["reason"].startswith("找不到这一包"))
        self.assertTrue(result["moves"][1]["reason"].startswith("找不到这一包"))
        self.assertNotIn("不止一个视频", result["moves"][0]["reason"])
        self.assertNotIn("不止一个视频", result["moves"][1]["reason"])
        self.assertIn("Shiotaiou", result["moves"][0]["reason"])
        self.assertIn("Magical Explorer", result["moves"][0]["reason"])
        self.assertEqual(session.moves, [])

    def test_inner_file_id_selects_that_shell(self):
        day = "/离线下载/动漫/20261108"
        sato_pack = day + "/[LoliHouse] Shiotaiou no Sato-san - 04 [WebRip].mkv"
        magic_pack = day + "/[LoliHouse] Magical Explorer - 04 [WebRip].mkv"
        video = "Shiotaiou no Sato-san - 04 [WebRip].mkv"
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2, "file_id": "sato-file"}]
        session.dirs[day] = [
            Node(sato_pack, "[LoliHouse] Shiotaiou no Sato-san - 04 [WebRip].mkv", True, raw={"fid": "sato-pack"}),
            Node(magic_pack, "[LoliHouse] Magical Explorer - 04 [WebRip].mkv", True, raw={"fid": "magic-pack"}),
        ]
        session.dirs[sato_pack] = [
            Node(f"{sato_pack}/{video}", video, False, 900 * 1024 * 1024, raw={"fid": "sato-file"}),
        ]
        session.dirs[magic_pack] = [
            Node(f"{magic_pack}/Magical Explorer - 04.mkv", "Magical Explorer - 04.mkv", False, 900 * 1024 * 1024, raw={"fid": "magic-file"}),
        ]
        tasks = [{
            "infohash": HASH,
            "show": "冷淡的佐藤同学只对我撒娇",
            "season": 1,
            "episode": 4,
            "title": "[LoliHouse] 冷淡的佐藤同学只对我撒娇 - 04 [WebRip]",
            "folder": day,
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "move")
        self.assertEqual(result["moves"][0]["pack"], sato_pack)
        self.assertEqual(result["moves"][0]["name"], video)

    def test_organize_writes_versioned_strm_and_skips_when_root_empty(self):
        show = "冷淡的佐藤同学只对我撒娇 (2026)"
        loli = "[LoliHouse] Shiotaiou - 05 [WebRip].mkv"
        ani = "[ANi] Shiotaiou - 05 [1080P].mkv"
        with tempfile.TemporaryDirectory() as tmp:
            session = _Fake()
            session.offline = [{"info_hash": HASH, "status": 2}, {"info_hash": "b" * 40, "status": 2}]
            session.dirs["/动漫"] = [Node("/动漫/" + show, show, True)]
            session.dirs["/box/loli"] = [
                Node("/box/loli/" + loli, loli, False, 900 * 1024 * 1024, raw={"pc": "lolihousecode0001"}),
            ]
            session.dirs["/box/ani"] = [
                Node("/box/ani/" + ani, ani, False, 900 * 1024 * 1024, raw={"pc": "anicode000000002"}),
            ]
            tasks = [
                {"infohash": HASH, "show": "冷淡的佐藤同学只对我撒娇", "season": 1, "episode": 5, "title": loli, "folder": "/box/loli", "status": "submitted"},
                {"infohash": "b" * 40, "show": "冷淡的佐藤同学只对我撒娇", "season": 1, "episode": 5, "title": ani, "folder": "/box/ani", "status": "submitted"},
            ]
            result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫", strm_root=tmp, play_base=PLAY_BASE))
            self.assertEqual(result["moves"][0]["action"], "move")
            self.assertEqual(result["moves"][1]["action"], "move")
            paths = sorted(Path(tmp).rglob("*.strm"))
            self.assertEqual(len(paths), 2)
            names = sorted(path.name for path in paths)
            prefix = f"{show} - S01E05 - "
            self.assertTrue(all(name.startswith(prefix) for name in names))
            self.assertNotEqual(names[0], names[1])
            text = paths[0].read_text(encoding="utf-8").strip()
            self.assertIn("pickcode=", text)
            self.assertIn("file_name=", text)
            self.assertTrue(text.startswith("https://mp.yuanhot.top/api/v1/plugin/P115StrmHelper/redirect_url"))
            self.assertFalse(list(Path(tmp).rglob("*.nfo")))
            self.assertFalse(list(Path(tmp).rglob("*.jpg")))
        with tempfile.TemporaryDirectory() as preview_root:
            session = _Fake()
            session.offline = [{"info_hash": HASH, "status": 2}]
            session.dirs["/动漫"] = [Node("/动漫/" + show, show, True)]
            session.dirs["/box/loli"] = [
                Node("/box/loli/" + loli, loli, False, 900 * 1024 * 1024, raw={"pc": "lolihousecode0001"}),
            ]
            preview = run_organize(
                [{"infohash": HASH, "show": "冷淡的佐藤同学只对我撒娇", "season": 1, "episode": 5, "title": loli, "folder": "/box/loli", "status": "submitted"}],
                session,
                JobConfig(dry_run=True, library_root="/动漫", strm_root=preview_root, play_base=PLAY_BASE),
            )
            self.assertEqual(preview["moves"][0]["action"], "move")
            self.assertTrue(preview["moves"][0]["strm"].endswith(".strm"))
            self.assertEqual(list(Path(preview_root).rglob("*.strm")), [])
            self.assertEqual(session.moves, [])
        with tempfile.TemporaryDirectory() as tmp:
            session = _Fake()
            session.offline = [{"info_hash": HASH, "status": 2}]
            session.dirs["/box"] = [Node("/box/E03.mkv", "E03.mkv", False, 900 * 1024 * 1024)]
            bare = run_organize(
                [{**tasks[0], "status": "submitted", "infohash": HASH}],
                session,
                JobConfig(dry_run=True, library_root="/动漫"),
            )
            self.assertNotIn("strm", bare["moves"][0])

    def test_missing_pickcode_still_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _Fake()
            session.offline = [{"info_hash": HASH, "status": 2}]
            session.dirs["/box"] = [Node("/box/E03.mkv", "E03.mkv", False, 900 * 1024 * 1024)]
            tasks = [{
                "infohash": HASH,
                "show": "佐藤同学",
                "season": 1,
                "episode": 3,
                "title": "佐藤同学 - 03",
                "folder": "/box",
                "status": "submitted",
            }]
            result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫", strm_root=tmp, play_base=PLAY_BASE))
            self.assertEqual(result["moves"][0]["action"], "move")
            self.assertEqual(result["moves"][0]["strm_error"], "缺少 pickcode")
            self.assertEqual(list(Path(tmp).rglob("*.strm")), [])
            self.assertEqual(tasks[0]["status"], "moved")

    def test_strm_backfill_does_not_move(self):
        show = "魔法★探险家 (2026)"
        video = "[LoliHouse] Magical Explorer - 02 [WebRip].mkv"
        with tempfile.TemporaryDirectory() as tmp:
            session = _Fake()
            session.dirs["/动漫"] = [Node("/动漫/" + show, show, True)]
            session.dirs["/动漫/" + show] = [Node(f"/动漫/{show}/Season 01", "Season 01", True)]
            session.dirs[f"/动漫/{show}/Season 01"] = [
                Node(f"/动漫/{show}/Season 01/{video}", video, False, 900 * 1024 * 1024, raw={"pc": "magiccode0000001"}),
                Node(f"/动漫/{show}/Season 01/NCOP.mkv", "NCOP.mkv", False, 80 * 1024 * 1024, raw={"pc": "ncopcode00000001"}),
            ]
            result = run_strm(session, JobConfig(dry_run=False, library_root="/动漫", strm_root=tmp, play_base=PLAY_BASE), f"/动漫/{show}")
            self.assertTrue(result["ok"])
            self.assertEqual(result["written"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(session.moves, [])
            written = list(Path(tmp).rglob("*.strm"))
            self.assertEqual(len(written), 1)
            self.assertIn("S01E02", written[0].name)
            self.assertTrue(written[0].name.startswith(f"{show} - S01E02 - "))
            self.assertIn("magiccode0000001", written[0].read_text(encoding="utf-8"))
            self.assertFalse(list(Path(tmp).rglob("*.nfo")))

    def test_play_url_matches_av_template(self):
        url = build_play_url(PLAY_BASE, "abcDEF1234567890x", "佐藤 05.mkv")
        self.assertIn("pickcode=abcDEF1234567890x", url)
        self.assertIn("file_name=", url)
        self.assertNotIn("{pickcode}", url)
        self.assertNotIn("{filename}", url)
        self.assertTrue(episode_strm_name("剧 (2026)", 1, 5, "剧 (2026) - S01E05 - 已有.mkv").startswith("剧 (2026) - S01E05"))

    def test_lan_strm_mode_does_not_write_public_domain(self):
        show = "冷淡的佐藤同学只对我撒娇 (2026)"
        video = "[LoliHouse] Shiotaiou - 05 [WebRip].mkv"
        with tempfile.TemporaryDirectory() as tmp:
            session = _Fake()
            session.offline = [{"info_hash": HASH, "status": 2}]
            session.dirs["/动漫"] = [Node("/动漫/" + show, show, True)]
            session.dirs["/box/loli"] = [
                Node("/box/loli/" + video, video, False, 900 * 1024 * 1024, raw={"pc": "lolihousecode0001"}),
            ]
            result = run_organize(tasks_for_lan(video), session, JobConfig(
                dry_run=False,
                library_root="/动漫",
                strm_root=tmp,
                play_base=PLAY_BASE,
                play_base_lan=PLAY_BASE_LAN,
                strm_for="lan",
            ))
            self.assertEqual(result["moves"][0]["action"], "move")
            written = list(Path(tmp).rglob("*.strm"))
            self.assertEqual(len(written), 1)
            text = written[0].read_text(encoding="utf-8")
            self.assertTrue(text.startswith("http://192.168.1.9:3000/"))
            self.assertNotIn("mp.yuanhot.top", text)
            self.assertIn("pickcode=lolihousecode0001", text)

    def test_organize_past_the_fifth_offline_page_and_clears_the_inbox(self):
        day10 = "/离线下载/动漫/20261110"
        day09 = "/离线下载/动漫/20261109"
        day08 = "/离线下载/动漫/20261108"
        spy = "[LoliHouse] 间谍教室 - 05 [1080p].mkv"
        dungeon = "[LoliHouse] 迷宫饭 - 03 [1080p].mkv"
        frieren = "[LoliHouse] 葬送的芙莉莲 - 08 [1080p].mkv"
        failed = "[LoliHouse] 为了养老金去异世界存八万金 - 01 [1080p].mkv"
        pension = "[LoliHouse] 为了养老金去异世界存八万金 - 02 [1080p].mkv"
        isekai = "[LoliHouse] 异世界召唤是第二次了 - 02 [1080p].mkv"
        session = _PagedOffline(_full_offline_pages(spy))
        session.dirs[day10] = [
            Node(f"{day10}/{spy}", spy, False, 900 * 1024 * 1024, raw={"fid": "spy-file", "pc": "spyclasscode00001"}),
            Node(f"{day10}/{frieren}", frieren, False, 900 * 1024 * 1024, raw={"pc": "frierencode000001"}),
            Node(f"{day10}/{failed}", failed, False, 900 * 1024 * 1024, raw={"pc": "failedcode0000001"}),
            Node(f"{day10}/{pension}", pension, False, 900 * 1024 * 1024, raw={"pc": "pensioncode000001"}),
        ]
        session.dirs[day09] = [
            Node(f"{day09}/{dungeon}", dungeon, False, 900 * 1024 * 1024, raw={"pc": "dungeonmesh000001"}),
        ]
        session.dirs[day08] = [
            Node(f"{day08}/{isekai}", isekai, False, 900 * 1024 * 1024, raw={"pc": "isekaicode0000001"}),
        ]
        tasks = [
            {"infohash": HASH, "show": "间谍教室", "season": 1, "episode": 5, "title": spy, "folder": day10, "status": "submitted"},
            {"infohash": "b" * 40, "show": "迷宫饭", "season": 1, "episode": 3, "title": dungeon, "folder": day09, "status": "submitted"},
            {"infohash": "d" * 40, "show": "葬送的芙莉莲", "season": 1, "episode": 8, "title": frieren, "folder": day10, "status": "submitted"},
            {"infohash": "e" * 40, "show": "为了养老金去异世界存八万金", "season": 1, "episode": 2, "title": pension, "folder": day10, "status": "moved"},
            {"infohash": "c" * 40, "show": "为了养老金去异世界存八万金", "season": 1, "episode": 1, "title": failed, "folder": day10, "status": "submitted"},
        ]
        seen = {"hashes": {
            HASH: {"show": "间谍教室", "season": 1, "episode": 5, "status": "submitted", "folder": day10, "title": spy},
            "f" * 40: {"show": "异世界召唤是第二次了", "season": 1, "episode": 2, "status": "submitted", "folder": day08, "title": isekai},
        }}
        with tempfile.TemporaryDirectory() as tmp:
            config = JobConfig(
                dry_run=False,
                library_root="/动漫",
                offline_root="/离线下载/动漫",
                strm_root=tmp,
                play_base=PLAY_BASE,
                play_base_lan=PLAY_BASE_LAN,
                strm_for="lan",
            )
            result = run_organize(tasks, session, config, seen=seen)
            self.assertEqual(session.asked, [1, 2, 3, 4, 5, 6])
            self.assertEqual([item["name"] for item in result["moves"]], [spy, dungeon, isekai])
            self.assertTrue(all(item["action"] == "move" for item in result["moves"]))
            self.assertEqual(result["moves"][0]["dest_dir"], "/动漫/间谍教室/Season 01")
            self.assertEqual([item[2] for item in session.moves], [spy, dungeon, isekai])
            self.assertEqual(tasks[0]["status"], "moved")
            self.assertEqual(tasks[1]["status"], "moved")
            self.assertEqual(tasks[2]["status"], "submitted")
            self.assertEqual(tasks[3]["status"], "moved")
            self.assertEqual(tasks[4]["status"], "submitted")
            self.assertEqual(tasks[5]["infohash"], "f" * 40)
            self.assertEqual(tasks[5]["status"], "moved")
            self.assertEqual(seen["hashes"][HASH]["status"], "moved")
            self.assertEqual(seen["hashes"]["f" * 40]["status"], "moved")
            written = list(Path(tmp).rglob("*.strm"))
            self.assertEqual(len(written), 3)
            for path in written:
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("http://192.168.1.9:3000/"))
                self.assertNotIn("mp.yuanhot.top", text)
            self.assertFalse(list(Path(tmp).rglob("*.nfo")))
            self.assertFalse(list(Path(tmp).rglob("*.jpg")))
            session.moves.clear()
            again = run_organize(tasks, session, config, seen=seen)
            self.assertEqual(again["moves"], [])
            self.assertEqual(session.moves, [])
            self.assertEqual(len(list(Path(tmp).rglob("*.strm"))), 3)

    def test_empty_offline_list_still_moves_the_dated_inbox(self):
        day = "/离线下载/动漫/20261109"
        video = "[LoliHouse] 迷宫饭 - 03 [1080p].mkv"
        session = _CountingOffline()
        session.dirs[day] = [
            Node(f"{day}/{video}", video, False, 900 * 1024 * 1024, raw={"pc": "dungeonmesh000001"}),
        ]
        tasks = [{
            "infohash": HASH,
            "show": "迷宫饭",
            "season": 1,
            "episode": 3,
            "title": video,
            "folder": day,
            "status": "submitted",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            result = run_organize(tasks, session, JobConfig(
                dry_run=False,
                library_root="/动漫",
                strm_root=tmp,
                play_base=PLAY_BASE,
                play_base_lan=PLAY_BASE_LAN,
                strm_for="lan",
            ))
            self.assertEqual(session.asked, [1])
            self.assertEqual(result["moves"][0]["action"], "move")
            self.assertEqual(result["moves"][0]["name"], video)
            self.assertEqual(tasks[0]["status"], "moved")
            written = list(Path(tmp).rglob("*.strm"))
            self.assertEqual(len(written), 1)
            self.assertTrue(written[0].read_text(encoding="utf-8").startswith("http://192.168.1.9:3000/"))
            self.assertFalse(list(Path(tmp).rglob("*.nfo")))

    def test_downloading_task_stays_in_the_inbox(self):
        day = "/离线下载/动漫/20261110"
        video = "[LoliHouse] 葬送的芙莉莲 - 08 [1080p].mkv"
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 1, "percentDone": 40, "name": video}]
        session.dirs[day] = [Node(f"{day}/{video}", video, False, 900 * 1024 * 1024, raw={"pc": "frierencode000001"})]
        tasks = [{
            "infohash": HASH,
            "show": "葬送的芙莉莲",
            "season": 1,
            "episode": 8,
            "title": video,
            "folder": day,
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"], [])
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")

    def test_page_sized_count_still_reads_the_next_offline_page(self):
        day = "/离线下载/动漫/20261110"
        video = "[LoliHouse] 间谍教室 - 05 [1080p].mkv"
        session = _CountLooksLikePageSize()
        session.dirs[day] = [Node(f"{day}/{video}", video, False, 900 * 1024 * 1024)]
        tasks = [{
            "infohash": HASH,
            "show": "间谍教室",
            "season": 1,
            "episode": 5,
            "title": video,
            "folder": day,
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertIn(2, session.asked)
        self.assertEqual(result["moves"], [])
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")

    def test_repeated_full_offline_page_does_not_move_unseen_tasks(self):
        day = "/离线下载/动漫/20261110"
        video = "[LoliHouse] 间谍教室 - 05 [1080p].mkv"
        session = _RepeatingOffline()
        session.dirs[day] = [Node(f"{day}/{video}", video, False, 900 * 1024 * 1024)]
        tasks = [{
            "infohash": HASH,
            "show": "间谍教室",
            "season": 1,
            "episode": 5,
            "title": video,
            "folder": day,
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(session.asked, [1, 2])
        self.assertEqual(result["moves"], [])
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")

    def test_organize_collision_does_not_move_again(self):
        video = "E03.mkv"
        dest = "/动漫/佐藤同学/Season 01"
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2}]
        session.dirs["/box"] = [Node("/box/" + video, video, False, 900 * 1024 * 1024)]
        session.dirs[dest] = [Node(dest + "/" + video, video, False)]
        tasks = [{
            "infohash": HASH,
            "show": "佐藤同学",
            "season": 1,
            "episode": 3,
            "title": "佐藤同学 - 03",
            "folder": "/box",
            "status": "submitted",
        }]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(result["moves"][0]["action"], "leave")
        self.assertEqual(result["moves"][0]["reason"], "collision")
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "submitted")

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


class SubscribeTest(unittest.TestCase):
    def test_only_anime_subscriptions_are_searched(self):
        rows = [
            {"name": "权力的游戏", "type": "电视剧", "tmdbid": 1399, "state": "R", "genre_ids": [18, 10765]},
            {"name": "魔法★探险家", "type": "电视剧", "tmdbid": 335194, "state": "R", "genre_ids": [16, 35]},
            {"name": "某电影", "type": "电影", "tmdbid": 11, "state": "R", "genre_ids": [16]},
            {"name": "来自 Bangumi", "type": "电视剧", "bangumiid": 123, "media_source": "bangumi", "state": "N"},
            {"name": "分类日番", "type": "电视剧", "media_category": "日番", "state": "R"},
            {"name": "暂停的番", "type": "电视剧", "bangumiid": 9, "state": "S"},
            {"name": "没有类型", "type": "电视剧", "tmdbid": 42, "state": "R"},
        ]

        def genres_of(tmdb_id):
            if tmdb_id == 42:
                return None
            raise AssertionError(tmdb_id)

        shows, notes = shows_from_subscriptions(rows, genres_of)
        keywords = [show.keyword for show in shows]
        self.assertEqual(keywords, ["魔法★探险家", "来自 Bangumi", "分类日番"])
        reasons = {item["show"]: item["reason"] for item in notes}
        self.assertEqual(reasons["权力的游戏"], "不是动画")
        self.assertEqual(reasons["某电影"], "电影")
        self.assertEqual(reasons["暂停的番"], "未在订阅")
        self.assertEqual(reasons["没有类型"], "无法确认是动画")
        looked = []

        def entries(show):
            looked.append(show.keyword)
            title = f"{show.keyword} - 03 [1080p][简体]"
            return [Torrent(title, HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW)]

        result = run_daily(shows, _Fake(), entries, {}, NOW, JobConfig(dry_run=True))
        self.assertTrue(result["dry_run"])
        self.assertEqual(looked, keywords)
        self.assertEqual({item["show"] for item in result["picks"]}, set(keywords))
        self.assertNotIn("权力的游戏", looked)

    def test_tmdb_lookup_keeps_animation_and_drops_drama(self):
        rows = [
            {"name": "冷淡的佐藤同学只对我撒娇", "type": "电视剧", "tmdbid": 316891, "state": "R", "keyword": "佐藤同学"},
            {"name": "普通美剧", "type": "电视剧", "tmdbid": 1399, "state": "R"},
        ]

        def genres_of(tmdb_id):
            return [16] if tmdb_id == 316891 else [18]

        shows, notes = shows_from_subscriptions(rows, genres_of)
        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0].keyword, "佐藤同学")
        self.assertEqual(shows[0].aliases, ["冷淡的佐藤同学只对我撒娇"])
        self.assertTrue(shows[0].airing)
        self.assertEqual(notes[0]["reason"], "不是动画")
        self.assertEqual(genre_ids_in({"genres": [{"id": 16, "name": "动画"}]}), [16])

    def test_subscribed_season_is_not_mixed(self):
        show = shows_from_subscriptions([
            {"name": "日番剧", "type": "电视剧", "media_category": "日番", "season": 2, "state": "R"},
        ])[0][0]
        torrents = [
            Torrent("日番剧 - 03 [1080p][简体]", HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW),
            Torrent("日番剧 S02E03 [1080p][简体]", "b" * 40, "magnet:?xt=urn:btih:" + "b" * 40, 900 * 1024 * 1024, NOW),
        ]
        picks, notes = select_one(show, torrents, SearchOptions(), NOW)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].season, 2)
        self.assertEqual(notes[0]["reason"], "季数不对")


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
        self.assertEqual(organize_schedule(""), "*/30 * * * *")
        self.assertEqual(organize_schedule(None), "*/30 * * * *")
        self.assertEqual(organize_schedule("  "), "*/30 * * * *")
        self.assertEqual(organize_schedule("15 1 * * *"), "15 1 * * *")
        jobs = scheduled_jobs(True, "15 */4 * * *", "", "")
        self.assertEqual(
            [(item["id"], item["name"], item["cron"]) for item in jobs],
            [
                ("Anime115", "动漫每日补磁力", "15 */4 * * *"),
                ("Anime115.Organize", "动漫离线整理", "*/30 * * * *"),
            ],
        )
        self.assertEqual(scheduled_jobs(False, "15 */4 * * *", "", "15 */2 * * *"), [])
        custom = scheduled_jobs(True, "", "15 2 * * *", "15 */2 * * *")
        self.assertEqual([item["name"] for item in custom], ["动漫离线整理", "动漫整包摊平"])
        self.assertEqual(custom[0]["cron"], "15 2 * * *")


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

    def test_task_list_keeps_page_count(self):
        class Client:
            def clouddownload_task_list(self, payload):
                return {
                    "page": payload["page"],
                    "page_count": 6,
                    "count": 120,
                    "tasks": [{"info_hash": "ab" * 20, "status": 2}],
                }

        pan = Pan115("UID=9_1", http=object(), gap=0, client_factory=lambda _cookie: Client())
        found = pan.list_tasks(6)
        self.assertEqual(found["data"]["page"], 6)
        self.assertEqual(found["data"]["page_count"], 6)
        self.assertEqual(found["data"]["count"], 120)
        self.assertEqual(found["data"]["tasks"][0]["status"], 2)

    def test_directory_walk_uses_aps_and_app_folder_api(self):
        http = _ScriptedHttp([
            ("0", [{"cid": "10", "n": "离线下载", "pid": "0"}]),
            ("10", [{"cid": "11", "n": "动漫", "pid": "10"}]),
        ])
        pan = Pan115("UID=9_1; CID=2", http=http, gap=0, client_factory=lambda _cookie: _FakeClient())
        self.assertEqual(pan.ensure_dir("/离线下载/动漫"), "11")
        self.assertTrue(any(call[1].startswith("https://aps.115.com/natsort/files.php") for call in http.calls))
        self.assertFalse(any("/open/" in call[1] for call in http.calls))
        self.assertFalse(any("/folder/update" in call[1] for call in http.calls))

    def test_existing_season_id_is_not_the_parent_and_skips_mkdir(self):
        pages = {
            "0": [{"cid": "2", "fid": "", "fc": 0, "n": "动漫", "pid": "0"}],
            "2": [{"fid": "5", "cid": "2", "pid": "2", "fc": 0, "n": "精灵幻想记 (2021)"}],
            "5": [{"fid": "6", "cid": "5", "pid": "5", "fc": 0, "n": "Season 02", "pc": "seasonpick"}],
        }
        http = _PageHttp(pages)
        pan = Pan115("UID=9_1", http=http, gap=0, client_factory=lambda _cookie: _FakeClient())
        self.assertEqual(pan.ensure_dir("/动漫/精灵幻想记 (2021)/Season 02"), "6")
        self.assertFalse(any(call[0] == "POST" for call in http.calls))
        self.assertFalse(any("/folder/update" in call[1] for call in http.calls))

    def test_missing_season_uses_fs_mkdir_and_existing_name_is_reused(self):
        pages = {
            "0": [{"cid": "2", "fid": "", "fc": 0, "n": "动漫", "pid": "0"}],
            "2": [{"fid": "5", "cid": "2", "pid": "2", "fc": 0, "n": "精灵幻想记 (2021)"}],
            "5": [{"fid": "6", "cid": "5", "pid": "5", "fc": 0, "n": "Season 02"}],
        }
        http = _PageHttp(pages)
        http.add_after = ("5", {"fid": "7", "cid": "5", "pid": "5", "fc": 0, "n": "Season 01"})
        pan = Pan115("UID=9_1", http=http, gap=0, client_factory=lambda _cookie: _FakeClient())
        self.assertEqual(pan.ensure_dir("/动漫/精灵幻想记 (2021)/Season 01"), "7")
        adds = [call for call in http.calls if call[1] == "https://webapi.115.com/files/add"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0][2]["data"], {"pid": "5", "cname": "Season 01"})
        self.assertIn("Chrome", adds[0][2]["headers"]["User-Agent"])
        self.assertFalse(any("/folder/update" in call[1] for call in http.calls))
        http.posts = [{"state": True, "errno": "", "cid": "8", "cname": "Season 03", "file_id": "8"}]
        self.assertEqual(pan.ensure_dir("/动漫/精灵幻想记 (2021)/Season 03"), "8")
        self.assertEqual(http.calls[-1][2]["data"], {"pid": "5", "cname": "Season 03"})

    def test_mkdir_failure_keeps_115_body_and_does_not_use_folder_update(self):
        pages = {
            "0": [{"cid": "2", "fid": "", "fc": 0, "n": "动漫", "pid": "0"}],
            "2": [],
        }
        http = _PageHttp(pages)
        http.posts = [{"state": False, "errno": 20009, "error": "父目录不存在。"}]
        pan = Pan115("UID=9_1", http=http, gap=0, client_factory=lambda _cookie: _FakeClient())
        with self.assertRaises(RuntimeError) as caught:
            pan.ensure_dir("/动漫/精灵幻想记")
        text = str(caught.exception)
        self.assertIn("/动漫/精灵幻想记", text)
        self.assertIn("父目录不存在", text)
        self.assertIn("20009", text)
        self.assertFalse(any("/folder/update" in call[1] for call in http.calls))
        self.assertTrue(any(call[1] == "https://webapi.115.com/files/add" for call in http.calls))

    def test_unreadable_parent_is_not_created(self):
        http = _PageHttp({})
        http.status = 400
        pan = Pan115("UID=9_1", http=http, gap=0, client_factory=lambda _cookie: _FakeClient())
        with self.assertRaises(RuntimeError) as caught:
            pan.ensure_dir("/动漫/Season 01")
        self.assertIn("列目录失败", str(caught.exception))
        self.assertIn("400", str(caught.exception))
        self.assertFalse(any(call[0] == "POST" for call in http.calls))

    def test_successful_move_still_writes_lan_strm(self):
        show = "精灵幻想记 (2021)"
        s2 = "[LoliHouse] 精灵幻想记 S02E08 [1080p].mkv"
        s1 = "[LoliHouse] 精灵幻想记 S01E11 [1080p].mkv"
        day = "20261110"
        pages = {
            "0": [
                {"cid": "1", "fid": "", "fc": 0, "n": "离线下载", "pid": "0"},
                {"cid": "2", "fid": "", "fc": 0, "n": "动漫", "pid": "0"},
            ],
            "1": [{"cid": "3", "fid": "", "fc": 0, "n": "动漫", "pid": "1"}],
            "3": [{"cid": "4", "fid": "", "fc": 0, "n": day, "pid": "3"}],
            "4": [
                {"fid": "81", "cid": "4", "pid": "4", "fc": 1, "n": s2, "s": 900 * 1024 * 1024, "pick_code": "seirei-s2"},
                {"fid": "82", "cid": "4", "pid": "4", "fc": 1, "n": s1, "s": 900 * 1024 * 1024, "pc": "seirei-s1"},
            ],
            "2": [{"fid": "5", "cid": "2", "pid": "2", "fc": 0, "n": show}],
            "5": [{"fid": "6", "cid": "5", "pid": "5", "fc": 0, "n": "Season 02"}],
        }
        http = _PageHttp(pages)
        http.posts = [{"state": True, "errno": "", "cid": "7", "file_name": "Season 01"}]
        pan = Pan115("UID=9_1", http=http, gap=0, client_factory=lambda _cookie: _FinishedClient())
        session = CookieSession("UID=9_1; CID=2; SEID=3; KID=4", None, pan)
        tasks = [
            {"infohash": HASH, "show": "精灵幻想记", "season": 2, "episode": 8, "title": s2, "folder": f"/离线下载/动漫/{day}", "status": "submitted"},
            {"infohash": "b" * 40, "show": "精灵幻想记", "season": 1, "episode": 11, "title": s1, "folder": f"/离线下载/动漫/{day}", "status": "submitted"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = run_organize(tasks, session, JobConfig(
                dry_run=False,
                library_root="/动漫",
                strm_root=tmp,
                play_base=PLAY_BASE,
                play_base_lan=PLAY_BASE_LAN,
                strm_for="lan",
            ))
            self.assertEqual([item["action"] for item in result["moves"]], ["move", "move"])
            self.assertEqual(result["moves"][0]["dest_dir"], f"/动漫/{show}/Season 02")
            self.assertEqual(result["moves"][1]["dest_dir"], f"/动漫/{show}/Season 01")
            adds = [call for call in http.calls if call[1] == "https://webapi.115.com/files/add"]
            self.assertEqual([call[2]["data"]["cname"] for call in adds], ["Season 01"])
            self.assertFalse(any("/folder/update" in call[1] for call in http.calls))
            moves = [call for call in http.calls if call[1].endswith("/files/move")]
            self.assertEqual([call[2]["data"]["to_cid"] for call in moves], ["6", "7"])
            written = sorted(Path(tmp).rglob("*.strm"))
            self.assertEqual(len(written), 2)
            texts = [path.read_text(encoding="utf-8") for path in written]
            self.assertTrue(all(text.startswith("http://192.168.1.9:3000/") for text in texts))
            self.assertTrue(all("mp.yuanhot.top" not in text for text in texts))
            joined = "\n".join(texts)
            self.assertIn("pickcode=seirei-s2", joined)
            self.assertIn("pickcode=seirei-s1", joined)
            self.assertFalse(list(Path(tmp).rglob("*.nfo")))
            self.assertEqual(tasks[0]["status"], "moved")
            self.assertEqual(tasks[1]["status"], "moved")


class ProgressTest(unittest.TestCase):
    def test_overview_lines_for_preview_and_live(self):
        preview = advance({}, {
            "job": "daily",
            "phase": "running",
            "dry_run": True,
            "index": 2,
            "total": 5,
            "current": "女友的朋友 05",
            "result": "跳过",
            "queued": 3,
            "submitted": 0,
            "skipped": 2,
            "failed": 0,
        })
        self.assertEqual(format_state(preview), "状态：进行中（预览）")
        self.assertEqual(
            format_progress(preview),
            "进度：每日 2/5：女友的朋友 05，跳过。排队 3，已提交 0，跳过 2，失败 0",
        )
        done = advance(preview, {"phase": "done"})
        self.assertEqual(done["state"], "状态：已完成（预览）")
        live = advance({}, {
            "job": "organize",
            "phase": "running",
            "dry_run": False,
            "index": 4,
            "total": 4,
            "current": "佐藤同学 - 03",
            "result": "ok",
            "organized": 3,
            "skipped": 1,
            "failed": 0,
        })
        self.assertEqual(format_state(live), "状态：进行中（正式）")
        self.assertEqual(
            format_progress(live),
            "进度：整理 4/4：佐藤同学 - 03，ok。已整理 3，跳过 1，失败 0",
        )
        self.assertEqual(format_state({}), "状态：空闲")
        self.assertEqual(format_progress({}), "进度：还没有运行记录")


def _install_fastapi_stub() -> None:
    """宿主才有 fastapi。单测里缺它时补一个空模块，导入插件不会去连网。"""
    if "fastapi" in sys.modules and hasattr(sys.modules["fastapi"], "Request"):
        return
    fastapi = types.ModuleType("fastapi")
    fastapi.Request = object
    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = object
    responses.RedirectResponse = object
    responses.Response = object
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses


class ImportTest(unittest.TestCase):
    def test_package_import_leaves_no_bare_module(self):
        _install_fastapi_stub()
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
        self.assertEqual(plugin.Anime115.plugin_version, "1.0.26")
        self.assertIs(sys.modules["anime_dmhy"], stale)
        self.assertIsNot(plugin.parse_rss, getattr(stale, "parse_rss", None))
        for name in ("anime_names", "anime_jobs", "anime_flatten", "anime_tmdb", "anime_config", "anime_pan", "anime_progress", "anime_strm", "anime_subscribe", "anime_playurl"):
            self.assertNotIn(name, sys.modules)
        for name in ("anime_dmhy", "anime_names", "anime_jobs", "anime_flatten", "anime_tmdb", "anime_config", "anime_pan", "anime_progress", "anime_strm", "anime_subscribe", "anime_playurl"):
            self.assertIn(f"app.plugins.anime115.{name}", sys.modules)
        _form, defaults = plugin.Anime115.get_form(None)
        for key in ("cookie", "offline_dir", "strm_root", "play_base", "play_base_lan", "strm_for", "storage", "subscribe_source"):
            self.assertIn(key, defaults)
        self.assertEqual(defaults["strm_for"], "lan")
        self.assertNotIn("mp.yuanhot.top", defaults["play_base_lan"])
        self.assertIn("mp.yuanhot.top", defaults["play_base"])
        self.assertEqual(defaults["subscribe_source"], "mp")
        self.assertEqual(defaults["organize_cron"], "*/30 * * * *")
        self.assertEqual(defaults["daily_cron"], "")
        self.assertEqual(defaults["flatten_cron"], "")
        saved = []
        host = plugin.Anime115()

        def remember(config):
            saved.append({
                "daily_cron": config.get("daily_cron"),
                "organize_cron": config.get("organize_cron"),
                "flatten_cron": config.get("flatten_cron"),
            })

        host.update_config = remember
        host.init_plugin({
            "enabled": True,
            "daily_cron": "15 */4 * * *",
            "organize_cron": "",
            "flatten_cron": "",
        })
        self.assertEqual(host._daily_cron, "15 */4 * * *")
        self.assertEqual(host._flatten_cron, "")
        self.assertEqual(host._organize_cron, "*/30 * * * *")
        self.assertEqual(saved, [{
            "daily_cron": "15 */4 * * *",
            "organize_cron": "*/30 * * * *",
            "flatten_cron": "",
        }])
        host.init_plugin({
            "enabled": True,
            "daily_cron": "15 */4 * * *",
            "organize_cron": "15 2 * * *",
            "flatten_cron": "",
        })
        self.assertEqual(host._organize_cron, "15 2 * * *")
        self.assertEqual(len(saved), 1)
        fresh = plugin.Anime115()
        fresh.update_config = remember
        fresh.init_plugin(None)
        self.assertEqual(fresh._organize_cron, "*/30 * * * *")
        self.assertEqual(len(saved), 1)
        daily = (_ROOT / "__init__.py").read_text().split("def _daily", 1)[1].split("def _prepare_shows", 1)[0]
        self.assertIn("_mp_session", daily)
        self.assertNotIn("P115Client", daily)
        self.assertNotIn("CookieSession", daily)
        for key in ("javbus_host", "proxy", "rename_folder", "overwrite"):
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


class _PageHttp:
    """按 cid 返回目录行。files/add 依次用 posts；没有 posts 时把 add_after 当成已存在。"""

    def __init__(self, pages):
        self.pages = {key: list(rows) for key, rows in pages.items()}
        self.calls = []
        self.posts = []
        self.add_after = None
        self.status = 200

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.status != 200 and "natsort/files.php" in url:
            return _Resp({"state": False, "error": "列目录失败"}, status=self.status)
        if method == "POST" and url.endswith("/files/add"):
            if self.add_after and not self.posts:
                cid, row = self.add_after
                self.pages.setdefault(cid, []).append(row)
                self.add_after = None
                return _Resp({"state": False, "errno": 20004, "error": "该目录名称已存在。"})
            payload = self.posts.pop(0) if self.posts else {"state": True, "cid": "99"}
            return _Resp(payload)
        if "natsort/files.php" in url:
            cid = str((kwargs.get("params") or {}).get("cid") or "")
            return _Resp({"state": True, "data": list(self.pages.get(cid, []))})
        return _Resp({"state": True, "data": []})


class _FinishedClient:
    def clouddownload_task_list(self, _payload):
        return {
            "page": 1,
            "page_count": 1,
            "count": 2,
            "tasks": [
                {"info_hash": HASH, "status": 2},
                {"info_hash": "b" * 40, "status": 2},
            ],
        }


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


def _filler(page: int, index: int) -> str:
    return f"{page:02d}{index:02d}{'cd' * 18}"


def _full_offline_pages(spy_name: str) -> dict:
    """6 页、每页 20 条。间谍教室在第 6 页，芙莉莲还在下，养老金那条失败。"""
    pages = {}
    for page in range(1, 7):
        pages[page] = [{"info_hash": _filler(page, index), "status": 2} for index in range(20)]
    pages[1][0] = {"info_hash": "d" * 40, "status": 1, "percentDone": 40, "name": "葬送的芙莉莲"}
    pages[1][1] = {"info_hash": "c" * 40, "status": -1, "name": "为了养老金去异世界存八万金"}
    pages[6][-1] = {"info_hash": HASH, "status": 2, "name": spy_name, "file_id": "spy-file"}
    return pages


class _Fake:
    def __init__(self):
        self.added = []
        self.moves = []
        self.dirs = {}
        self.offline = []
        self.deleted = []
        self.listed = []

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
        self.listed.append(path)
        return list(self.dirs.get(path, []))

    def move(self, node, dest_dir, new_name):
        self.moves.append((node.path, dest_dir, new_name))
        return True

    def delete_dir(self, path):
        self.deleted.append(path)
        return True


class _PagedOffline(_Fake):
    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.asked = []

    def list_offline(self, page):
        self.asked.append(page)
        rows = self.pages.get(page, [])
        return {"data": {"tasks": rows, "page": page, "page_count": 6, "count": 120}}


class _CountingOffline(_Fake):
    def __init__(self):
        super().__init__()
        self.asked = []

    def list_offline(self, page):
        self.asked.append(page)
        return super().list_offline(page)


class _CountLooksLikePageSize(_Fake):
    """count 等于这一页的 20 条时，后面一页仍要读。那里还在下载的不能当已落地。"""

    def __init__(self):
        super().__init__()
        self.asked = []

    def list_offline(self, page):
        self.asked.append(page)
        if page == 1:
            rows = [{"info_hash": f"{index:02d}{'ab' * 19}", "status": 2} for index in range(20)]
            return {"data": {"tasks": rows, "count": 20}}
        if page == 2:
            return {"data": {"tasks": [{"info_hash": HASH, "status": 1, "percentDone": 10}], "count": 20}}
        return {"data": {"tasks": []}}


class _RepeatingOffline(_Fake):
    def __init__(self):
        super().__init__()
        self.asked = []

    def list_offline(self, page):
        self.asked.append(page)
        return {"data": {"tasks": [
            {"info_hash": f"{index:02d}{'ab' * 19}", "status": 2}
            for index in range(20)
        ]}}


class ClosedLoopTest(unittest.TestCase):
    def test_empty_leftover_packs_are_not_walked_on_the_next_organize(self):
        """295 条已完成、包里已经没有视频的提交，整理完成后下一轮不再扫。"""
        day = "/离线下载/动漫/20261110"
        count = 295
        session = _Fake()
        session.dirs[day] = []
        tasks = []
        seen = {"hashes": {}}
        for index in range(count):
            infohash = f"{index:040x}"
            pack_name = f"Show{index:03d} - 01.mkv"
            pack = f"{day}/{pack_name}"
            title = f"Show{index:03d} - 01"
            session.dirs[day].append(Node(pack, pack_name, True, raw={"fid": f"pack-{index}"}))
            if index % 2:
                session.dirs[pack] = []
            else:
                sub = title + ".ass"
                session.dirs[pack] = [Node(f"{pack}/{sub}", sub, False, 100)]
            session.offline.append({
                "info_hash": infohash,
                "status": 2,
                "name": pack_name,
                "file_id": f"pack-{index}",
            })
            tasks.append({
                "infohash": infohash,
                "show": f"剧{index:03d}",
                "season": 1,
                "episode": 1,
                "title": title,
                "folder": day,
                "status": "submitted",
            })
            seen["hashes"][infohash] = {
                "status": "submitted",
                "folder": day,
                "show": f"剧{index:03d}",
                "season": 1,
                "episode": 1,
                "title": title,
                "at": NOW.isoformat(),
            }

        def list_offline(page, rows=session.offline):
            if page == 1:
                return {"data": {"tasks": rows, "page": 1, "page_count": 1, "count": len(rows)}}
            return {"data": {"tasks": [], "page_count": 1}}

        session.list_offline = list_offline
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"), seen=seen)
        self.assertTrue(result["ok"])
        self.assertNotIn("error", result)
        self.assertNotIn("strm_error", result)
        self.assertEqual(len(result["moves"]), count)
        self.assertEqual(session.moves, [])
        self.assertTrue(all(item["action"] == "leave" and item["reason"] == "没有视频" for item in result["moves"]))
        self.assertTrue(all("建不了目标目录" not in (item.get("error") or item["reason"]) for item in result["moves"]))
        self.assertTrue(all(task["status"] == "no_video" and task["reason"] == "没有视频" for task in tasks))
        self.assertTrue(all(row["status"] == "no_video" for row in seen["hashes"].values()))
        self.assertNotIn("moved", {row["status"] for row in seen["hashes"].values()})
        session.listed.clear()
        again = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"), seen=seen)
        self.assertTrue(again["ok"])
        self.assertEqual(again["moves"], [])
        self.assertEqual(session.moves, [])
        self.assertEqual(session.listed, [])
        self.assertTrue(all(task["status"] == "no_video" for task in tasks))

    def test_unconfirmed_missing_video_stays_pending(self):
        day = "/离线下载/动漫/20261110"
        leftover = f"{day}/Show000 - 01.mkv"
        video_pack = f"{day}/Show001 - 02.mkv"
        video = "Show001 - 02.mkv"
        mismatch = f"{day}/佐藤同学 - 03.mkv"
        mismatch_video = "佐藤同学 - 03.mkv"
        missing = f"{day}/不存在的包"
        session = _Fake()
        session.offline = [
            {"info_hash": f"{index:040x}", "status": 2, "name": name, "file_id": fid}
            for index, name, fid in (
                (0, "Show000 - 01.mkv", "pack-0"),
                (1, video, "pack-1"),
                (2, "[LoliHouse] 别的剧 - 02.mkv", "other"),
                (3, mismatch_video, "pack-3"),
                (4, "不存在的包", "missing"),
            )
        ]
        session.dirs[day] = [
            Node(leftover, "Show000 - 01.mkv", True, raw={"fid": "pack-0"}),
            Node(video_pack, video, True, raw={"fid": "pack-1"}),
            Node(f"{day}/别的目录A", "别的目录A", True),
            Node(f"{day}/别的目录B", "别的目录B", True),
            Node(mismatch, mismatch_video, True, raw={"fid": "pack-3"}),
        ]
        session.dirs[leftover] = [Node(f"{leftover}/Show000 - 01.ass", "Show000 - 01.ass", False, 100)]
        session.dirs[video_pack] = [Node(f"{video_pack}/{video}", video, False, 900 * 1024 * 1024)]
        session.dirs[mismatch] = [
            Node(f"{mismatch}/{mismatch_video}", mismatch_video, False, 900 * 1024 * 1024),
            Node(f"{mismatch}/{mismatch_video}/inside.nfo", "inside.nfo", False, 10),
        ]
        tasks = [
            {"infohash": f"{0:040x}", "show": "剧000", "season": 1, "episode": 1, "title": "Show000 - 01", "folder": day, "status": "submitted"},
            {"infohash": f"{1:040x}", "show": "剧001", "season": 1, "episode": 2, "title": "Show001 - 02", "folder": day, "status": "submitted"},
            {"infohash": f"{2:040x}", "show": "对不上的剧", "season": 1, "episode": 2, "title": "对不上的剧 - 02", "folder": day, "status": "submitted"},
            {"infohash": f"{3:040x}", "show": "佐藤同学", "season": 1, "episode": 3, "title": "佐藤同学 - 03", "folder": day, "status": "submitted"},
            {"infohash": f"{4:040x}", "show": "不存在的包", "season": 1, "episode": 1, "title": "不存在的包", "folder": missing, "status": "submitted"},
        ]
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertTrue(result["ok"])
        reasons = [item["reason"] for item in result["moves"]]
        self.assertEqual(reasons[0], "没有视频")
        self.assertEqual(result["moves"][1]["action"], "move")
        self.assertTrue(reasons[2].startswith("找不到这一包"))
        self.assertEqual(reasons[3], "集数对不上")
        self.assertEqual(reasons[4], "没有视频")
        self.assertEqual(tasks[0]["status"], "no_video")
        self.assertEqual(tasks[1]["status"], "moved")
        self.assertEqual(tasks[2]["status"], "submitted")
        self.assertEqual(tasks[3]["status"], "submitted")
        self.assertEqual(tasks[4]["status"], "submitted")
        self.assertNotIn("建不了目标目录", reasons[0])
        self.assertNotIn("strm_error", result)
        self.assertEqual([item[2] for item in session.moves], [video])
        session.listed.clear()
        session.moves.clear()
        again = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual([item["task"] for item in again["moves"]], [f"{2:040x}", f"{3:040x}", f"{4:040x}"])
        self.assertTrue(again["moves"][0]["reason"].startswith("找不到这一包"))
        self.assertEqual(again["moves"][1]["reason"], "集数对不上")
        self.assertEqual(again["moves"][2]["reason"], "没有视频")
        self.assertEqual(session.moves, [])
        self.assertEqual(tasks[0]["status"], "no_video")
        self.assertEqual(tasks[2]["status"], "submitted")
        self.assertEqual(tasks[3]["status"], "submitted")
        self.assertEqual(tasks[4]["status"], "submitted")

    def test_dry_run_leaves_empty_leftover_pending(self):
        day = "/离线下载/动漫/20261110"
        pack = f"{day}/Show000 - 01.mkv"
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2, "name": "Show000 - 01.mkv", "file_id": "pack-0"}]
        session.dirs[day] = [Node(pack, "Show000 - 01.mkv", True, raw={"fid": "pack-0"})]
        session.dirs[pack] = [Node(f"{pack}/Show000 - 01.ass", "Show000 - 01.ass", False, 100)]
        tasks = [{
            "infohash": HASH,
            "show": "剧000",
            "season": 1,
            "episode": 1,
            "title": "Show000 - 01",
            "folder": day,
            "status": "submitted",
        }]
        seen = {"hashes": {HASH: {"status": "submitted", "folder": day, "at": NOW.isoformat()}}}
        preview = run_organize(tasks, session, JobConfig(dry_run=True, library_root="/动漫"), seen=seen)
        self.assertEqual(preview["moves"][0]["reason"], "没有视频")
        self.assertEqual(tasks[0]["status"], "submitted")
        self.assertEqual(seen["hashes"][HASH]["status"], "submitted")
        self.assertEqual(session.moves, [])
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"), seen=seen)
        self.assertEqual(result["moves"][0]["reason"], "没有视频")
        self.assertEqual(tasks[0]["status"], "no_video")
        self.assertEqual(seen["hashes"][HASH]["status"], "no_video")

    def test_no_video_does_not_change_the_three_day_resubmit(self):
        show = parse_watchlist("佐藤同学")[0]
        torrent = Torrent("佐藤同学 - 03 [1080p][简体]", HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW)
        recent = {
            "hashes": {"b" * 40: {"at": NOW.isoformat(), "status": "no_video"}},
            "episodes": {"佐藤同学|1|3": "b" * 40},
        }
        blocked = run_daily([show], _Fake(), lambda _show: [torrent], recent, NOW, JobConfig(dry_run=False))
        self.assertEqual(blocked["submitted"], [])
        stale = {
            "hashes": {"b" * 40: {"at": (NOW - timedelta(days=4)).isoformat(), "status": "no_video"}},
            "episodes": {"佐藤同学|1|3": "b" * 40},
        }
        result = run_daily([show], _Fake(), lambda _show: [torrent], stale, NOW, JobConfig(dry_run=False))
        self.assertEqual(len(result["submitted"]), 1)

    def test_offline_list_timeout_logs_and_the_next_run_still_moves(self):
        from anime115 import anime_jobs

        release = threading.Event()

        class Hang(_Fake):
            def list_offline(self, _page):
                release.wait(5)
                return {"data": {"tasks": []}}

        session = Hang()
        video = "佐藤同学 - 03.mkv"
        node = Node("/box/" + video, video, False, 900 * 1024 * 1024, raw={"pc": "satocode"})
        session.dirs["/box"] = [node]
        tasks = [{
            "infohash": HASH,
            "show": "佐藤同学",
            "season": 1,
            "episode": 3,
            "title": "佐藤同学 - 03",
            "folder": "/box",
            "status": "submitted",
        }]
        logs = []
        previous = anime_jobs._OFFLINE_CALL_TIMEOUT
        previous_log = anime_jobs._job_log
        anime_jobs._OFFLINE_CALL_TIMEOUT = 0.2
        anime_jobs._job_log = lambda message, level="info": logs.append(message)
        started = time.monotonic()
        try:
            result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        finally:
            release.set()
            anime_jobs._OFFLINE_CALL_TIMEOUT = previous
            anime_jobs._job_log = previous_log
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(result["ok"])
        self.assertIn("离线列表超时", result["error"])
        self.assertEqual(tasks[0]["status"], "submitted")
        self.assertEqual(session.moves, [])
        self.assertTrue(any("第 1 页" in line for line in logs))
        follow = _Fake()
        follow.offline = [{"info_hash": HASH, "status": 2}]
        follow.dirs["/box"] = [node]
        again = run_organize(tasks, follow, JobConfig(dry_run=False, library_root="/动漫"))
        self.assertEqual(again["moves"][0]["action"], "move")
        self.assertEqual(tasks[0]["status"], "moved")

    def test_stop_keeps_the_finished_move_and_leaves_the_rest(self):
        class StopAfterSleep(_Fake):
            def __init__(self):
                super().__init__()
                self.cancel = threading.Event()

            def sleep(self, _seconds):
                self.cancel.set()

        session = StopAfterSleep()
        first = "佐藤同学 - 03.mkv"
        second = "佐藤同学 - 04.mkv"
        session.offline = [
            {"info_hash": HASH, "status": 2},
            {"info_hash": "b" * 40, "status": 2},
        ]
        session.dirs["/box/a"] = [Node("/box/a/" + first, first, False, 900 * 1024 * 1024)]
        session.dirs["/box/b"] = [Node("/box/b/" + second, second, False, 900 * 1024 * 1024)]
        tasks = [
            {"infohash": HASH, "show": "佐藤同学", "season": 1, "episode": 3, "title": "佐藤同学 - 03", "folder": "/box/a", "status": "submitted"},
            {"infohash": "b" * 40, "show": "佐藤同学", "season": 1, "episode": 4, "title": "佐藤同学 - 04", "folder": "/box/b", "status": "submitted"},
        ]
        seen = {"hashes": {
            HASH: {"status": "submitted"},
            "b" * 40: {"status": "submitted"},
        }}
        result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"), seen=seen)
        self.assertEqual(result["error"], "已停止")
        self.assertTrue(result["persist"])
        self.assertEqual(tasks[0]["status"], "moved")
        self.assertEqual(tasks[1]["status"], "submitted")
        self.assertEqual(seen["hashes"][HASH]["status"], "moved")
        self.assertEqual(seen["hashes"]["b" * 40]["status"], "submitted")
        self.assertEqual([item[2] for item in session.moves], [first])
        follow = _Fake()
        follow.offline = session.offline
        follow.dirs = session.dirs
        again = run_organize(tasks, follow, JobConfig(dry_run=False, library_root="/动漫"), seen=seen)
        self.assertTrue(again["ok"])
        self.assertEqual([item[2] for item in follow.moves], [second])
        self.assertEqual(tasks[1]["status"], "moved")

    def test_stop_during_offline_list_does_not_drop_the_title(self):
        release = threading.Event()

        class Hang(_Fake):
            def __init__(self):
                super().__init__()
                self.cancel = threading.Event()

            def list_offline(self, _page):
                release.wait(5)
                return {"data": {"tasks": []}}

        session = Hang()
        tasks = [{
            "infohash": HASH,
            "show": "佐藤同学",
            "season": 1,
            "episode": 3,
            "title": "佐藤同学 - 03",
            "folder": "/box",
            "status": "submitted",
        }]
        threading.Thread(target=lambda: (time.sleep(0.05), session.cancel.set()), daemon=True).start()
        started = time.monotonic()
        try:
            result = run_organize(tasks, session, JobConfig(dry_run=False, library_root="/动漫"))
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["error"], "已停止")
        self.assertEqual(tasks[0]["status"], "submitted")
        self.assertEqual(session.moves, [])

    def test_daily_stop_keeps_the_submit_already_done(self):
        show = parse_watchlist("佐藤同学")[0]
        first = Torrent("佐藤同学 - 03 [1080p][简体]", HASH, f"magnet:?xt=urn:btih:{HASH}", 900 * 1024 * 1024, NOW)
        second = Torrent("佐藤同学 - 04 [1080p][简体]", "b" * 40, "magnet:?xt=urn:btih:" + "b" * 40, 900 * 1024 * 1024, NOW)

        class StopAfterAdd(_Fake):
            def __init__(self):
                super().__init__()
                self.cancel = threading.Event()

            def add_offline(self, magnet, fileid):
                added = super().add_offline(magnet, fileid)
                self.cancel.set()
                return added

        session = StopAfterAdd()
        seen = {}
        result = run_daily([show], session, lambda _show: [first, second], seen, NOW, JobConfig(dry_run=False))
        self.assertEqual(result["error"], "已停止")
        self.assertTrue(result["persist"])
        self.assertEqual(len(result["submitted"]), 1)
        self.assertEqual(len(session.added), 1)
        self.assertEqual(len(seen["hashes"]), 1)

    def test_flatten_stop_moves_nothing(self):
        session = _Fake()
        session.cancel = threading.Event()
        session.cancel.set()
        session.dirs["/inbox"] = [Node("/inbox/show", "show", True)]
        session.dirs["/inbox/show"] = [Node("/inbox/show/extra", "extra", True)]
        session.dirs["/inbox/show/extra"] = [Node("/inbox/show/extra/E01.mkv", "E01.mkv", False, 900 * 1024 * 1024)]
        result = run_flatten(session, JobConfig(dry_run=False, flatten_apply=True, flatten_roots=["/inbox"]))
        self.assertEqual(result["error"], "已停止")
        self.assertEqual(result["moved"], 0)
        self.assertEqual(session.moves, [])

    def test_stale_reservation_is_not_disk_full_and_retries_next_organize(self):
        from anime115 import anime_strm

        calls = {"n": 0}
        logged = []
        real_write = anime_strm.write_strm
        real_free = anime_strm.free_bytes
        real_log = anime_strm._strm_log
        real_pause = anime_strm._STRM_RETRY_PAUSE

        def flaky(root, relative, url):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise OSError(28, "No space left on device")
            return real_write(root, relative, url)

        anime_strm.write_strm = flaky
        anime_strm.free_bytes = lambda _path: 200 * 1024 ** 3
        anime_strm._strm_log = logged.append
        anime_strm._STRM_RETRY_PAUSE = 0
        video = "佐藤同学 - 03.mkv"
        session = _Fake()
        session.offline = [{"info_hash": HASH, "status": 2}]
        session.dirs["/box"] = [Node("/box/" + video, video, False, 900 * 1024 * 1024, raw={"pc": "satocode"})]
        tasks = [{
            "infohash": HASH,
            "show": "佐藤同学",
            "season": 1,
            "episode": 3,
            "title": "佐藤同学 - 03",
            "folder": "/box",
            "status": "submitted",
        }]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = JobConfig(
                    dry_run=False,
                    library_root="/动漫",
                    strm_root=tmp,
                    play_base=PLAY_BASE,
                    play_base_lan=PLAY_BASE_LAN,
                    strm_for="lan",
                )
                result = run_organize(tasks, session, config)
                self.assertEqual(result["moves"][0]["action"], "move")
                self.assertEqual(tasks[0]["status"], "moved")
                self.assertEqual(tasks[0]["strm_status"], "failed")
                self.assertIn("共享预留", result["strm_error"])
                self.assertNotIn("磁盘已满", result["strm_error"])
                self.assertEqual(list(Path(tmp).rglob("*.strm")), [])
                self.assertTrue(any("共享预留" in line for line in logged))
                self.assertEqual(len(session.moves), 1)
                again = run_organize(tasks, session, config)
                written = list(Path(tmp).rglob("*.strm"))
                self.assertEqual(len(written), 1)
                self.assertTrue(written[0].read_text(encoding="utf-8").startswith("http://192.168.1.9:3000/"))
                self.assertEqual(tasks[0]["strm_status"], "written")
                self.assertEqual(len(session.moves), 1)
                self.assertNotIn("磁盘已满", again.get("strm_error", ""))
        finally:
            anime_strm.write_strm = real_write
            anime_strm.free_bytes = real_free
            anime_strm._strm_log = real_log
            anime_strm._STRM_RETRY_PAUSE = real_pause

    def test_real_enospc_is_disk_full_without_retry(self):
        from anime115 import anime_strm
        from anime115.anime_strm import commit_strm

        calls = {"n": 0}
        logged = []
        real_write = anime_strm.write_strm
        real_free = anime_strm.free_bytes
        real_log = anime_strm._strm_log
        real_pause = anime_strm._STRM_RETRY_PAUSE

        def fail(_root, _relative, _url):
            calls["n"] += 1
            raise OSError(28, "No space left on device")

        anime_strm.write_strm = fail
        anime_strm.free_bytes = lambda _path: 0
        anime_strm._strm_log = logged.append
        anime_strm._STRM_RETRY_PAUSE = 0
        try:
            with self.assertRaises(OSError) as caught:
                commit_strm("/media/动漫", "剧/Season 01/a.strm", "http://192.168.1.9:3000/a")
        finally:
            anime_strm.write_strm = real_write
            anime_strm.free_bytes = real_free
            anime_strm._strm_log = real_log
            anime_strm._STRM_RETRY_PAUSE = real_pause
        self.assertEqual(calls["n"], 1)
        self.assertIn("磁盘已满", str(caught.exception))
        self.assertNotIn("共享预留", str(caught.exception))
        self.assertTrue(any("磁盘已满" in line for line in logged))

    def test_other_strm_error_is_not_called_disk_full(self):
        from anime115 import anime_strm
        from anime115.anime_strm import commit_strm

        calls = {"n": 0}
        logged = []
        real_write = anime_strm.write_strm
        real_log = anime_strm._strm_log

        def fail(_root, _relative, _url):
            calls["n"] += 1
            raise OSError(13, "Permission denied")

        anime_strm.write_strm = fail
        anime_strm._strm_log = logged.append
        try:
            with self.assertRaises(OSError) as caught:
                commit_strm("/media/动漫", "剧/Season 01/a.strm", "http://192.168.1.9:3000/a")
        finally:
            anime_strm.write_strm = real_write
            anime_strm._strm_log = real_log
        self.assertEqual(calls["n"], 1)
        self.assertNotIn("磁盘已满", str(caught.exception))
        self.assertNotIn("共享预留", str(caught.exception))
        self.assertTrue(logged)
        self.assertNotIn("磁盘已满", logged[0])

    def test_reload_releases_the_lock_and_logs_the_end(self):
        plugin = _load_plugin_module()
        logs = []

        def record(message, *args, **kwargs):
            logs.append(str(message))

        plugin.logger = types.SimpleNamespace(info=record, warning=record, error=record)
        host = plugin.Anime115()
        host.update_config = lambda _config: None
        host.get_data = lambda _key=None: []
        host.save_data = lambda *_args, **_kwargs: None
        config = {"enabled": True, "organize_cron": "*/30 * * * *", "dry_run": True}
        host.init_plugin(config)
        entered = threading.Event()

        def blocked():
            cancel = host._cancel
            entered.set()
            cancel.wait(3)
            if cancel.is_set():
                return {"ok": False, "error": "已停止", "dry_run": True}
            return {"ok": True, "dry_run": True}

        outcome = {}

        def run():
            outcome["value"] = host._guard("organize", blocked)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(2))
        overlap = host._guard("organize", lambda: {"ok": True, "dry_run": True})
        self.assertEqual(overlap["error"], "已有任务在运行")
        host.init_plugin(config)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["value"]["error"], "已停止")
        self.assertTrue(any("organize 结束：已停止" in line for line in logs))
        self.assertTrue(any("已有任务在运行" in line for line in logs))
        again = host._guard("organize", lambda: {"ok": True, "dry_run": True})
        self.assertTrue(again["ok"])
        self.assertFalse(host._lock.locked())
        self.assertTrue(any("organize 结束：完成" in line for line in logs))


def _load_plugin_module():
    _install_fastapi_stub()
    app = sys.modules.get("app")
    if app is None:
        app = types.ModuleType("app")
        app.__path__ = []
        sys.modules["app"] = app
    if "app.chain" not in sys.modules:
        chain = types.ModuleType("app.chain")
        chain.__path__ = []
        storage = types.ModuleType("app.chain.storage")

        class StorageChain:
            pass

        storage.StorageChain = StorageChain
        sys.modules["app.chain"] = chain
        sys.modules["app.chain.storage"] = storage
    if "app.log" not in sys.modules:
        log = types.ModuleType("app.log")
        log.logger = types.SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None, error=lambda *_a, **_k: None)
        sys.modules["app.log"] = log
    if "app.plugins" not in sys.modules:
        plugins = types.ModuleType("app.plugins")
        plugins.__path__ = []

        class _PluginBase:
            pass

        plugins._PluginBase = _PluginBase
        sys.modules["app.plugins"] = plugins
    name = "app.plugins.anime115"
    spec = importlib.util.spec_from_file_location(
        name,
        _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
