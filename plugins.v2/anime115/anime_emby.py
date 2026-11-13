"""通知 MoviePilot 里的 Emby 刷新指定媒体库，并给新写的 strm 预热。

失败只返回中文句子，不抛异常。
"""

from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _running_media_server_modules():
    """延迟导入宿主，只在调用方没有注入 modules 时走这里。"""
    from app.core.module import ModuleManager
    from app.schemas.types import ModuleType

    return ModuleManager().get_running_type_modules(ModuleType.MediaServer)


def _match_emby(library_name: str, modules=None):
    """找出名字匹配的 Emby 实例和媒体库。

    返回 [(实例名, host, apikey, 库 id, 库名)]，找不到就是空列表。
    """
    wanted = str(library_name or "").strip()
    candidates = _running_media_server_modules() if modules is None else modules
    found = []
    for module in candidates or []:
        if module.__class__.__name__ != "EmbyModule":
            continue
        for instance_name, server in (module.get_instances() or {}).items():
            host = str(getattr(server, "_host", "") or "").rstrip("/")
            apikey = str(getattr(server, "_apikey", "") or "")
            if not host or not apikey:
                continue
            for library in server.get_librarys() or []:
                name = str(getattr(library, "name", "") or "")
                if wanted and name.casefold() != wanted.casefold():
                    continue
                found.append((instance_name, host, apikey, str(getattr(library, "id", "") or ""), name))
    return found


def refresh_library(library_name: str, modules=None) -> str:
    """刷新 MoviePilot 里已登录 Emby 的指定媒体库，返回给详情页看的中文结果。

    :param library_name: 媒体库名称，忽略首尾空白、按大小写不敏感匹配
    :param modules: 可选的媒体服务器模块可迭代对象；只在单测里注入，
                    注入时完全不导入 MoviePilot 宿主
    :return: 中文结果说明。找不到库或刷新失败都只返回句子，绝不抛异常，
             也绝不回退成刷新全部媒体库
    """
    try:
        wanted = str(library_name or "").strip()
        candidates = _running_media_server_modules() if modules is None else modules
        found = False
        for module in candidates or []:
            if module.__class__.__name__ != "EmbyModule":
                continue
            found = True
            for instance_name, server in (module.get_instances() or {}).items():
                libraries = server.get_librarys() or []
                target = next(
                    (
                        library
                        for library in libraries
                        if str(getattr(library, "name", "") or "").casefold() == wanted.casefold()
                    ),
                    None,
                )
                if target is None:
                    continue
                refresh_one = getattr(server, "_Emby__refresh_emby_library_by_id", None)
                if not refresh_one:
                    return f"{instance_name} 不支持按库刷新"
                if refresh_one(str(getattr(target, "id", ""))):
                    return f"已通知 {instance_name} 刷新媒体库 {target.name}"
                return f"{instance_name} 刷新媒体库 {target.name} 失败"
        if not found:
            return "MoviePilot 里没有可用的 Emby"
        return f"没有找到名为 {wanted} 的 Emby 媒体库"
    except Exception as exc:
        return f"刷新 Emby 失败：{exc}"


def _api_get(host: str, apikey: str, path: str, params: dict, timeout: int = 60):
    query = dict(params or {})
    query["api_key"] = apikey
    url = f"{host}{path}?{urlencode(query)}"
    request = Request(url, headers={"User-Agent": "MoviePilot-Anime115", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            return None
        return json.loads(response.read().decode("utf-8", "replace"))


def _api_post(host: str, apikey: str, path: str, params: dict, timeout: int = 120):
    query = dict(params or {})
    query["api_key"] = apikey
    url = f"{host}{path}?{urlencode(query)}"
    request = Request(url, data=b"{}", method="POST",
                      headers={"User-Agent": "MoviePilot-Anime115", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return getattr(response, "status", 200)


def probe_once(host: str, apikey: str, library_id: str, paths: list[str]) -> int:
    """让 Emby 先探测这些 strm。用户点播放时就不用等 ffprobe。

    返回成功探测的条数。
    """
    wanted = {str(p) for p in (paths or []) if p}
    if not wanted or not library_id:
        return 0
    payload = _api_get(host, apikey, "/Items", {
        "ParentId": library_id,
        "Recursive": "true",
        "IncludeItemTypes": "Episode",
        "Fields": "Path",
        "Limit": 20000,
    }) or {}
    items = payload.get("Items") or []
    by_path = {}
    for item in items:
        path = str(item.get("Path") or "")
        item_id = str(item.get("Id") or "")
        if path and item_id:
            by_path[path] = item_id
    done = 0
    for path in sorted(wanted):
        item_id = by_path.get(path)
        if not item_id:
            continue
        try:
            _api_post(host, apikey, f"/Items/{item_id}/PlaybackInfo", {})
        except Exception:
            continue
        done += 1
    return done


def start_prewarm(library_name: str, paths: list[str], modules=None, cancel=None,
                  rounds: int = 6, wait: int = 90) -> None:
    """后台给新写的 strm 预热。

    Emby 扫库是异步的，文件刚写完时它还看不到，所以隔一会儿再试，
    最多试 rounds 轮。整轮都不阻塞整理任务。
    """
    targets = [str(p) for p in (paths or []) if p]
    if not targets:
        return

    def worker():
        for _ in range(max(1, rounds)):
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                return
            try:
                for _name, host, apikey, library_id, _lib in _match_emby(library_name, modules):
                    count = probe_once(host, apikey, library_id, targets)
                    if count:
                        _log(f"动漫 115 已给 Emby 预热 {count} 集的媒体信息")
                        return
            except Exception as exc:
                _log(f"动漫 115 预热 Emby 失败：{type(exc).__name__}", "warning")
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                return
            time.sleep(max(5, wait))

    thread = threading.Thread(target=worker, name="anime115-prewarm", daemon=True)
    thread.start()


def _log(message: str, level: str = "info") -> None:
    try:
        from app.log import logger

        getattr(logger, level, logger.info)(message)
    except Exception:
        pass
