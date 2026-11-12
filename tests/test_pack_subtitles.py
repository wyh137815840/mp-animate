"""整包入库时把字幕认到每一集，并且搬进季目录时不会撞名。"""

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

from anime115.anime_flatten import Node  # noqa: E402
from anime115.anime_jobs import _decide_move, _move_name  # noqa: E402

PACK = "/离线下载/动漫/20261112/[Group] 某剧 [01-02][BDRip 1080p]"


def node(folder, name, size=900 * 1024 * 1024):
    return Node(f"{folder}/{name}", name, False, size, raw={"pc": "pc-" + name})


def _files():
    root = f"{PACK}/某剧"
    return [
        node(root, "某剧 - 01 [1080p].mkv"),
        node(root, "某剧 - 02 [1080p].mkv"),
        node(f"{root}/Subs", "某剧 01.ass", 4096),
        node(f"{root}/Subs", "某剧 02.ass", 4096),
    ]


def _task():
    return {"show": "某剧", "season": 1, "episode": 0, "title": "某剧 01-02", "infohash": "a" * 40}


class PackSubtitleTest(unittest.TestCase):
    def test_each_episode_gets_its_subtitle(self):
        decision = _decide_move(_task(), _files(), "/动漫", PACK)
        self.assertEqual(decision["action"], "move")
        self.assertEqual(len(decision["items"]), 2)
        for item in decision["items"]:
            self.assertEqual(len(item["subs"]), 1)
            self.assertEqual(item["subs"][0].name, f"某剧 {item['episode']:02d}.ass")

    def test_subtitle_moves_with_unique_name(self):
        decision = _decide_move(_task(), _files(), "/动漫", PACK)
        for item in decision["items"]:
            sub = item["subs"][0]
            name = _move_name(item, sub)
            self.assertEqual(name, f"{item['video'].name.rsplit('.', 1)[0]}.ass")
            # 视频本身不改名
            self.assertEqual(_move_name(item, item["video"]), item["video"].name)

    def test_public_record_has_no_node_objects(self):
        import json

        decision = _decide_move(_task(), _files(), "/动漫", PACK)
        json.dumps(decision["items"], default=str)
        from anime115.anime_jobs import _public_move

        json.dumps(_public_move(decision))


if __name__ == "__main__":
    unittest.main()
