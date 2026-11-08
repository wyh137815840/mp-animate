"""TMDB 季集数和已播进度。密钥只从配置读取，不写进日志。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request

from .anime_dmhy import moviepilot_proxy, rss_opener
from .anime_names import Show


@dataclass(frozen=True)
class SeasonProgress:
    """一季的计划集数，以及播出日不晚于今天的最后一集。"""

    episode_count: int
    last_aired: int | None


def shanghai_today(now: datetime | None = None) -> date:
    """按亚洲上海的日历日判断一集播没播。"""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Asia/Shanghai")).date()
    except Exception:
        return moment.astimezone(timezone(timedelta(hours=8))).date()


def progress_from_season(payload: dict | None, today: date) -> SeasonProgress | None:
    """从 TMDB season 详情算出计划集数和已播到第几集。没有集列表时返回空。"""
    episodes = (payload or {}).get("episodes") or []
    numbers = []
    aired = []
    saw_date = False
    for item in episodes:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("episode_number") or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        numbers.append(number)
        air = str(item.get("air_date") or "")[:10]
        if len(air) == 10:
            saw_date = True
            if air <= today.isoformat():
                aired.append(number)
    try:
        declared = int((payload or {}).get("episode_count") or 0)
    except (TypeError, ValueError):
        declared = 0
    if not numbers and declared <= 0:
        return None
    count = max(declared, max(numbers) if numbers else 0)
    last_aired = (max(aired) if aired else 0) if saw_date else None
    return SeasonProgress(count, last_aired)


def match_tv_id(results: list[dict], names: list[str]) -> int | None:
    """只接受名称或原名和追更名完全一致的电视剧。"""
    folded = {name.casefold() for name in names if name}
    for item in results or []:
        if not isinstance(item, dict):
            continue
        labels = [str(item.get("name") or ""), str(item.get("original_name") or "")]
        if not any(label.casefold() in folded for label in labels if label):
            continue
        try:
            return int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def resolve_tv_id(show: Show, search) -> int | None:
    """有 tmdb_id 就用它。否则按剧名和别名搜索，对上才返回。"""
    if show.tmdb_id:
        return show.tmdb_id
    names = show.names()
    for query in names:
        matched = match_tv_id(search(query), names)
        if matched:
            return matched
    return None


def live_bounds(api_key: str, today: date):
    """给打分用的季进度。没填插件密钥时用 MoviePilot 的 TMDB_API_KEY。"""
    key = (api_key or "").strip() or _settings_key()
    warned = {"done": False}
    ids: dict[str, int] = {}
    cached: dict[tuple[int, int], SeasonProgress | None] = {}

    def bounds(show: Show, season: int) -> tuple[int, int | None] | None:
        if not show.airing and not show.tmdb_id:
            return None
        if not key:
            if not warned["done"]:
                warned["done"] = True
                _log("动漫 115 未配置 tmdb_api_key，连载中不限制播出进度")
            return None
        tv_id = show.tmdb_id
        if not tv_id:
            if show.keyword not in ids:
                ids[show.keyword] = resolve_tv_id(show, lambda name: _search(name, key)) or 0
            tv_id = ids[show.keyword]
        if not tv_id:
            _log(f"动漫 115 TMDB 没对上：{show.keyword}")
            return None
        cache_key = (tv_id, season)
        if cache_key not in cached:
            cached[cache_key] = progress_from_season(_season(tv_id, season, key), today)
            item = cached[cache_key]
            if item is None:
                _log(f"动漫 115 TMDB 季详情为空：{show.keyword} S{season:02d}")
            else:
                aired = "未知" if item.last_aired is None else str(item.last_aired)
                _log(f"动漫 115 TMDB {show.keyword} S{season:02d} 已播 {aired}/{item.episode_count}")
        item = cached[cache_key]
        if item is None:
            return None
        return item.episode_count, item.last_aired

    return bounds


def _search(name: str, api_key: str) -> list[dict]:
    payload = _fetch_json("/3/search/tv", {"query": name}, api_key)
    rows = (payload or {}).get("results") or []
    return [row for row in rows if isinstance(row, dict)]


def _season(tv_id: int, season: int, api_key: str) -> dict | None:
    return _fetch_json(f"/3/tv/{int(tv_id)}/season/{int(season)}", {}, api_key)


def _fetch_json(path: str, query: dict, api_key: str) -> dict | None:
    """请求 TMDB。失败日志只写状态，不写带密钥的地址。"""
    params = {"api_key": api_key, "language": "zh-CN", **query}
    url = f"https://{_api_domain()}{path}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "MoviePilot-Anime115", "Accept": "application/json"})
    try:
        with rss_opener(moviepilot_proxy()).open(request, timeout=20) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                _log(f"动漫 115 TMDB 返回 {status}")
                return None
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        _log(f"动漫 115 TMDB 请求失败：{type(exc).__name__}")
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("status_code")
    if isinstance(code, int) and code >= 400:
        _log(f"动漫 115 TMDB {payload.get('status_message') or code}")
        return None
    return payload


def _settings_key() -> str:
    try:
        from app.core.config import settings
    except Exception:
        return ""
    return str(getattr(settings, "TMDB_API_KEY", "") or "").strip()


def _api_domain() -> str:
    domain = "api.themoviedb.org"
    try:
        from app.core.config import settings
        domain = str(getattr(settings, "TMDB_API_DOMAIN", "") or domain).strip() or domain
    except Exception:
        pass
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    return domain.strip("/") or "api.themoviedb.org"


def _log(message: str) -> None:
    try:
        from app.log import logger
    except Exception:
        return
    logger.info(message)
