"""字幕：整包里按集数找字幕，下载后写到 .strm 旁边。"""

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
from anime115.anime_subs import (  # noqa: E402
    episode_of,
    is_subtitle_name,
    match_subtitles,
    subtitle_relative,
    unpack,
    write_sidecar,
)


def node(path: str, name: str, is_dir: bool = False) -> Node:
    return Node(path=path, name=name, is_dir=is_dir)


class SubtitleNameTest(unittest.TestCase):
    def test_suffix_check(self):
        self.assertTrue(is_subtitle_name("01.ass"))
        self.assertTrue(is_subtitle_name("某剧 05.srt"))
        self.assertFalse(is_subtitle_name("某剧 05.mkv"))
        self.assertFalse(is_subtitle_name("readme.txt"))

    def test_episode_from_name(self):
        self.assertEqual(episode_of("01.ass"), 1)
        self.assertEqual(episode_of("[字幕组][某剧][05][简体].ass"), 5)
        self.assertEqual(episode_of("某剧 - 07 [简体].ass"), 7)
        self.assertEqual(episode_of("某剧 第03话.ass"), 3)
        self.assertEqual(episode_of("说明.txt"), 0)


class MatchSubtitleTest(unittest.TestCase):
    def test_same_stem_wins(self):
        video = node("/离线/某剧/某剧 - 01 [1080p].mkv", "某剧 - 01 [1080p].mkv")
        same = node("/离线/某剧/某剧 - 01 [1080p].ass", "某剧 - 01 [1080p].ass")
        other = node("/离线/某剧/某剧 - 02 [1080p].ass", "某剧 - 02 [1080p].ass")
        found = match_subtitles([video, same, other], video, 1, 1)
        self.assertEqual([item.name for item in found], [same.name])

    def test_subtitle_dir_by_episode(self):
        video = node("/离线/某剧/某剧 - 03 [1080p].mkv", "某剧 - 03 [1080p].mkv")
        sub = node("/离线/某剧/Subs/某剧 03.ass", "某剧 03.ass")
        other = node("/离线/某剧/Subs/某剧 04.ass", "某剧 04.ass")
        found = match_subtitles([video, sub, other], video, 3, 1)
        self.assertEqual([item.name for item in found], [sub.name])

    def test_no_subtitle_returns_empty(self):
        video = node("/离线/某剧/某剧 - 03 [1080p].mkv", "某剧 - 03 [1080p].mkv")
        self.assertEqual(match_subtitles([video], video, 3, 1), [])


class SidecarTest(unittest.TestCase):
    def test_relative_keeps_folder(self):
        """Emby 认的是同目录同主干名，语言码让客户端自动选中文。"""
        relative = subtitle_relative("某剧/Season 01/某剧 - S01E01 - 原名.strm", "某剧 - S01E01 - 原名.ass")
        self.assertEqual(relative, "某剧/Season 01/某剧 - S01E01 - 原名.zh-CN.ass")

    def test_relative_traditional_subtitle(self):
        relative = subtitle_relative("某剧/Season 01/原名.strm", "原名 [繁体].ass")
        self.assertTrue(relative.endswith(".zh-TW.ass"))

    def test_write_sidecar(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_sidecar(tmp, "某剧/Season 01/某剧 - S01E01 - 原名.zh-CN.ass", b"text")
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).read_bytes(), b"text")

    def test_unpack_zip(self):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("某剧 01.ass", "content")
        data = unpack(buffer.getvalue(), "subs.zip")
        self.assertEqual(data, b"content")


if __name__ == "__main__":
    unittest.main()
