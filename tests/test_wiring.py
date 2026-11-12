"""v1.0.23 接线测试：Emby 刷新开关、手动接口签名、版本与历史。不连宿主。"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"


def _install_fastapi_stub() -> None:
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


def _load_plugin():
    """用空壳 app 模块导入插件包，返回插件模块。"""
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

    class _Logger:
        def info(self, *_args, **_kwargs):
            return None

        def warning(self, *_args, **_kwargs):
            return None

        def error(self, *_args, **_kwargs):
            return None

    log.logger = _Logger()
    plugins = types.ModuleType("app.plugins")
    plugins.__path__ = []

    class _PluginBase:
        pass

    plugins._PluginBase = _PluginBase
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
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.plugins.anime115"] = module
    spec.loader.exec_module(module)
    return module


_PLUGIN = _load_plugin()


class ApiRunSignatureTest(unittest.TestCase):
    def test_manual_run_has_no_var_keyword(self):
        signature = inspect.signature(_PLUGIN.Anime115.api_run)
        kinds = {param.kind for param in signature.parameters.values()}
        self.assertNotIn(inspect.Parameter.VAR_KEYWORD, kinds)
        self.assertEqual(signature.parameters["job"].default, "daily")
        self.assertEqual(signature.parameters["root"].default, "")


class FormTest(unittest.TestCase):
    def test_emby_switch_and_library_field_exist(self):
        components, defaults = _PLUGIN.Anime115().get_form()
        text = json.dumps(components, ensure_ascii=False)
        self.assertIn('"refresh_emby"', text)
        self.assertIn('"emby_library"', text)
        self.assertIs(defaults["refresh_emby"], False)
        self.assertEqual(defaults["emby_library"], "动漫")


class RefreshResultTest(unittest.TestCase):
    def setUp(self):
        self.host = _PLUGIN.Anime115()
        self.host._dry_run = False
        self.host._refresh_emby = True
        self.host._emby_library = "动漫"
        self._original = _PLUGIN.refresh_library

    def tearDown(self):
        _PLUGIN.refresh_library = self._original

    def test_preview_does_not_refresh(self):
        self.host._dry_run = True
        payload = {"dry_run": True}
        self.host._refresh_emby_result(payload, wrote=True, planning=True)
        self.assertEqual(payload["emby"], "预览不刷新 Emby")

    def test_preview_without_planning_stays_quiet(self):
        self.host._dry_run = True
        payload = {"dry_run": True}
        self.host._refresh_emby_result(payload, wrote=False, planning=False)
        self.assertNotIn("emby", payload)

    def test_switch_off_does_not_refresh(self):
        self.host._refresh_emby = False
        payload = {"dry_run": False}
        self.host._refresh_emby_result(payload, wrote=True, planning=True)
        self.assertNotIn("emby", payload)

    def test_nothing_written_does_not_refresh(self):
        payload = {"dry_run": False}
        self.host._refresh_emby_result(payload, wrote=False, planning=True)
        self.assertNotIn("emby", payload)

    def test_real_write_refreshes_named_library(self):
        calls = []

        def fake_refresh(library):
            calls.append(library)
            return "已通知 emby 刷新媒体库 动漫"

        _PLUGIN.refresh_library = fake_refresh
        payload = {"dry_run": False}
        self.host._refresh_emby_result(payload, wrote=True, planning=True)
        self.assertEqual(calls, ["动漫"])
        self.assertIn("已通知", payload["emby"])

    def test_refresh_error_does_not_break_payload(self):
        def boom(_library):
            raise RuntimeError("emby 挂了")

        _PLUGIN.refresh_library = boom
        payload = {"dry_run": False, "ok": True}
        self.host._refresh_emby_result(payload, wrote=True, planning=True)
        self.assertIn("刷新 Emby 失败", payload["emby"])
        self.assertTrue(payload["ok"])


class VersionTest(unittest.TestCase):
    def test_package_and_history(self):
        package = json.loads((Path(__file__).resolve().parents[1] / "package.v2.json").read_text(encoding="utf-8"))
        self.assertEqual(package["Anime115"]["version"], "1.0.29")
        self.assertIn("标题比对", package["Anime115"]["history"]["v1.0.29"])
        self.assertEqual(_PLUGIN.Anime115.plugin_version, "1.0.29")


if __name__ == "__main__":
    unittest.main()
