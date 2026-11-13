"""给 Emby 预热媒体信息的测试。"""

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

from anime115 import anime_emby  # noqa: E402


class PrewarmTest(unittest.TestCase):
    """预热：把新写的 strm 交给 Emby 先探测一遍。"""

    def setUp(self):
        self.gets = []
        self.posts = []

        def fake_get(host, apikey, path, params, timeout=60):
            self.gets.append((host, apikey, path, dict(params)))
            return {
                "Items": [
                    {"Id": "11", "Path": "/media/动漫/某剧/Season 01/S01E01.strm"},
                    {"Id": "22", "Path": "/media/动漫/某剧/Season 01/S01E02.strm"},
                    {"Id": "33", "Path": "/media/动漫/别的剧/Season 01/S01E01.strm"},
                ]
            }

        def fake_post(host, apikey, path, params, timeout=120):
            self.posts.append((host, apikey, path, dict(params)))
            return 200

        self.original_get = anime_emby._api_get
        self.original_post = anime_emby._api_post
        anime_emby._api_get = fake_get
        anime_emby._api_post = fake_post

    def tearDown(self):
        anime_emby._api_get = self.original_get
        anime_emby._api_post = self.original_post

    def test_probes_only_wanted_paths(self):
        done = anime_emby.probe_once(
            "http://emby:8096", "KEY", "4005",
            ["/media/动漫/某剧/Season 01/S01E01.strm", "/media/动漫/某剧/Season 01/S01E02.strm",
             "/media/动漫/没有的一集.strm"],
        )
        self.assertEqual(done, 2)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual({call[2] for call in self.posts}, {"/Items/11/PlaybackInfo", "/Items/22/PlaybackInfo"})

    def test_no_paths_does_nothing(self):
        self.assertEqual(anime_emby.probe_once("http://emby:8096", "KEY", "4005", []), 0)
        self.assertEqual(self.gets, [])
        self.assertEqual(self.posts, [])

    def test_no_library_id_does_nothing(self):
        self.assertEqual(anime_emby.probe_once("http://emby:8096", "KEY", "", ["/x.strm"]), 0)
        self.assertEqual(self.gets, [])


if __name__ == "__main__":
    unittest.main()
