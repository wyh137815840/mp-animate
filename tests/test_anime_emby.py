"""Emby 刷新模块的纯规则测试。不连 MoviePilot，也不连 Emby。"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
_pkg = types.ModuleType("anime115")
_pkg.__path__ = [str(_ROOT)]
_pkg.__package__ = "anime115"
sys.modules["anime115"] = _pkg

from anime115.anime_emby import refresh_library  # noqa: E402


class FakeLibrary:
    """宿主 MediaServerLibrary 的最小替身。"""

    def __init__(self, name, library_id):
        self.name = name
        self.id = library_id


class FakeServer:
    """Emby 服务实例的替身。私有刷新方法挂在实例上。"""

    def __init__(self, libraries, refresh_result=True, can_refresh=True, library_error=None):
        self.libraries = libraries
        self.refresh_result = refresh_result
        self.library_error = library_error
        self.refreshed = []
        self.root_refreshed = 0
        if can_refresh:
            self._Emby__refresh_emby_library_by_id = self._refresh

    def _refresh(self, library_id):
        self.refreshed.append(library_id)
        return self.refresh_result

    def get_librarys(self):
        if self.library_error is not None:
            raise self.library_error
        return self.libraries

    def refresh_root_library(self):
        self.root_refreshed += 1
        return True


class _FakeModule:
    def __init__(self, instances):
        self._instances = instances

    def get_instances(self):
        return self._instances


def emby_module(instances):
    """类名必须叫 EmbyModule：实现按 __class__.__name__ 识别媒体服务器。"""
    return type("EmbyModule", (_FakeModule,), {})(instances)


def jellyfin_module(instances):
    return type("JellyfinModule", (_FakeModule,), {})(instances)


class _FakeModuleManager:
    """替身 ModuleManager，记录宿主传来的 ModuleType。"""

    seen_types = []
    instances = []

    def get_running_type_modules(self, module_type):
        type(self).seen_types.append(module_type)
        return iter(type(self).instances)


class RefreshLibraryTest(unittest.TestCase):
    def test_hit_same_name_calls_exact_library_id(self):
        first = FakeServer([FakeLibrary("电影", "movie-lib")])
        second = FakeServer([FakeLibrary("动漫", "anime-lib")])
        result = refresh_library(
            "动漫",
            modules=[emby_module({"客厅 Emby": first, "卧室 Emby": second})],
        )
        self.assertEqual(result, "已通知 卧室 Emby 刷新媒体库 动漫")
        self.assertEqual(first.refreshed, [])
        self.assertEqual(second.refreshed, ["anime-lib"])
        self.assertEqual(second.root_refreshed, 0)

    def test_case_insensitive_and_chinese_name_match(self):
        server = FakeServer([FakeLibrary("电影", "movie-lib"), FakeLibrary("ANIME", "anime-lib")])
        result = refresh_library("anime", modules=[emby_module({"主 Emby": server})])
        self.assertEqual(result, "已通知 主 Emby 刷新媒体库 ANIME")
        self.assertEqual(server.refreshed, ["anime-lib"])

    def test_chinese_name_matches_with_surrounding_spaces(self):
        server = FakeServer([FakeLibrary("动漫", "anime-lib")])
        result = refresh_library("  动漫 ", modules=[emby_module({"主 Emby": server})])
        self.assertEqual(result, "已通知 主 Emby 刷新媒体库 动漫")
        self.assertEqual(server.refreshed, ["anime-lib"])

    def test_missing_library_never_refreshes_all(self):
        server = FakeServer([FakeLibrary("电影", "movie-lib")])
        result = refresh_library("动漫", modules=[emby_module({"主 Emby": server})])
        self.assertEqual(result, "没有找到名为 动漫 的 Emby 媒体库")
        self.assertEqual(server.refreshed, [])
        self.assertEqual(server.root_refreshed, 0)

    def test_no_modules_at_all(self):
        self.assertEqual(refresh_library("动漫", modules=[]), "MoviePilot 里没有可用的 Emby")
        self.assertEqual(refresh_library("动漫", modules=()), "MoviePilot 里没有可用的 Emby")

    def test_only_jellyfin_module_is_skipped(self):
        server = FakeServer([FakeLibrary("动漫", "anime-lib")])
        result = refresh_library("动漫", modules=[jellyfin_module({"Jellyfin": server})])
        self.assertEqual(result, "MoviePilot 里没有可用的 Emby")
        self.assertEqual(server.refreshed, [])
        self.assertEqual(server.root_refreshed, 0)

    def test_missing_private_refresh_method(self):
        server = FakeServer([FakeLibrary("动漫", "anime-lib")], can_refresh=False)
        result = refresh_library("动漫", modules=[emby_module({"主 Emby": server})])
        self.assertEqual(result, "主 Emby 不支持按库刷新")
        self.assertEqual(server.refreshed, [])
        self.assertEqual(server.root_refreshed, 0)

    def test_refresh_returns_false(self):
        server = FakeServer([FakeLibrary("动漫", "anime-lib")], refresh_result=False)
        result = refresh_library("动漫", modules=[emby_module({"主 Emby": server})])
        self.assertEqual(result, "主 Emby 刷新媒体库 动漫 失败")
        self.assertEqual(server.refreshed, ["anime-lib"])
        self.assertEqual(server.root_refreshed, 0)

    def test_get_librarys_raises_returns_string(self):
        server = FakeServer([], library_error=RuntimeError("Emby 连接超时"))
        result = refresh_library("动漫", modules=[emby_module({"主 Emby": server})])
        self.assertIsInstance(result, str)
        self.assertEqual(result, "刷新 Emby 失败：Emby 连接超时")
        self.assertEqual(server.root_refreshed, 0)

    def test_refresh_call_raises_returns_string(self):
        class BoomServer(FakeServer):
            def _refresh(self, library_id):
                raise RuntimeError("刷新接口 500")

        server = BoomServer([FakeLibrary("动漫", "anime-lib")])
        result = refresh_library("动漫", modules=[emby_module({"主 Emby": server})])
        self.assertEqual(result, "刷新 Emby 失败：刷新接口 500")
        self.assertEqual(server.root_refreshed, 0)

    def test_injected_modules_do_not_import_host(self):
        before = {name for name in sys.modules if name == "app" or name.startswith("app.")}
        server = FakeServer([FakeLibrary("动漫", "anime-lib")])
        refresh_library("动漫", modules=[emby_module({"主 Emby": server})])
        after = {name for name in sys.modules if name == "app" or name.startswith("app.")}
        self.assertEqual(after - before, set())

    def test_modules_none_uses_lazy_host_import(self):
        server = FakeServer([FakeLibrary("动漫", "host-anime-lib")])
        module = emby_module({"宿主 Emby": server})
        sentinel = object()
        _FakeModuleManager.seen_types = []
        _FakeModuleManager.instances = [module]
        fake_core = types.ModuleType("app.core.module")
        fake_core.ModuleManager = _FakeModuleManager
        fake_types = types.ModuleType("app.schemas.types")
        fake_types.ModuleType = types.SimpleNamespace(MediaServer=sentinel)
        patched = {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.module": fake_core,
            "app.schemas": types.ModuleType("app.schemas"),
            "app.schemas.types": fake_types,
        }
        with mock.patch.dict(sys.modules, patched):
            result = refresh_library("动漫", modules=None)
        self.assertEqual(result, "已通知 宿主 Emby 刷新媒体库 动漫")
        self.assertEqual(server.refreshed, ["host-anime-lib"])
        self.assertEqual(_FakeModuleManager.seen_types, [sentinel])


if __name__ == "__main__":
    unittest.main()
