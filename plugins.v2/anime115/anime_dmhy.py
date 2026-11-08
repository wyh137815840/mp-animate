"""动漫花园 RSS 的解析和打分。不访问 115。"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from anime_names import Show, parse_episode, title_matches


_BATCH = re.compile(r"合集|全集|整季|batch|\bFin\b|BDrip|BDRip", re.IGNORECASE)
_RAW = re.compile(r"生肉|\bRAW\b", re.IGNORECASE)
_UHD = re.compile(r"2160|4K", re.IGNORECASE)
_BTIH = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", re.IGNORECASE)
_SIZE = re.compile(r"([\d.]+)\s*(GB|MB|KB|GiB|MiB)", re.IGNORECASE)
_UNIT = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3}


@dataclass
class Torrent:
    """一条资源。"""

    title: str
    infohash: str
    magnet: str
    size: int = 0
    published: datetime | None = None


@dataclass
class SearchOptions:
    """每日搜索的过滤条件。"""

    resolution: str = "1080"
    require_sub: bool = True
    allow_batch: bool = False
    lookback_hours: int = 36
    min_episode_mb: int = 80
    max_episode_gb: int = 8
    fansub_prefer: list[str] = field(default_factory=list)


@dataclass
class Pick:
    """一部剧里被选中的一集。"""

    show: str
    season: int
    episode: int
    torrent: Torrent
    score: int


def fetch_rss(keyword: str, timeout: int = 20) -> bytes:
    """拉取动漫花园动画分类的 RSS。已配置系统代理时走代理，否则直连。"""
    proxy = moviepilot_proxy()
    url = "https://share.dmhy.org/topics/rss/sort_id/2/rss.xml?keyword=" + quote(keyword or "")
    request = Request(url, headers={"User-Agent": "MoviePilot-Anime115"})
    _log("动漫花园 RSS 经系统代理拉取" if proxy else "动漫花园 RSS 直连拉取")
    with rss_opener(proxy).open(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"动漫花园返回 {response.status}")
        return response.read()


def moviepilot_proxy() -> str:
    """读取 MoviePilot 的系统代理。没有安装宿主或没填时返回空字符串。"""
    try:
        from app.core.config import settings
    except Exception:
        return ""
    configured = getattr(settings, "PROXY", None)
    if isinstance(configured, dict):
        host = configured.get("https") or configured.get("http") or ""
        if str(host).strip():
            return normalize_proxy(str(host))
    return normalize_proxy(str(getattr(settings, "PROXY_HOST", "") or ""))


def normalize_proxy(value: str) -> str:
    """补上缺失的 http 协议。空字符串保持直连。"""
    text = (value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return "http://" + text
    return text


def rss_opener(proxy: str):
    """有代理时 http/https 都走它。没有代理时不使用环境变量里的代理。"""
    if proxy:
        return build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    return build_opener(ProxyHandler({}))


def _log(message: str) -> None:
    try:
        from app.log import logger
    except Exception:
        return
    logger.info(message)


def parse_rss(payload: bytes | str) -> list[Torrent]:
    """从 RSS XML 取出磁力。认不出 infohash 的条目丢掉。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    root = ET.fromstring(payload)
    found = []
    for item in root.iter("item"):
        title = _text(item, "title")
        magnet = ""
        size = 0
        enclosure = item.find("enclosure")
        if enclosure is not None:
            magnet = enclosure.attrib.get("url") or ""
            size = _int(enclosure.attrib.get("length"))
        if not magnet:
            description = _text(item, "description")
            matched = _BTIH.search(description)
            magnet = f"magnet:?xt=urn:btih:{matched.group(1)}" if matched else ""
            if not size:
                size = _size_in_text(description)
        infohash = _infohash(magnet)
        if not infohash:
            continue
        if not size:
            size = _size_in_text(_text(item, "description"))
        found.append(Torrent(
            title=title,
            infohash=infohash,
            magnet=magnet,
            size=size,
            published=_published(_text(item, "pubDate")),
        ))
    return found


def select_shows(
    shows: list[Show],
    entries_of: Callable[[Show], list[Torrent]],
    options: SearchOptions,
    now: datetime,
) -> tuple[list[Pick], list[dict]]:
    """每部剧、每一集只留分数最高的一条。"""
    picks = []
    notes = []
    for show in shows:
        chosen, dropped = select_one(show, entries_of(show), options, now)
        picks.extend(chosen)
        notes.extend(dropped)
    return picks, notes


def select_one(
    show: Show,
    torrents: list[Torrent],
    options: SearchOptions,
    now: datetime,
) -> tuple[list[Pick], list[dict]]:
    """对一部剧打分。"""
    best: dict[tuple[int, int], Pick] = {}
    notes = []
    for torrent in torrents:
        score, reason = score_torrent(show, torrent, options, now)
        if score is None:
            notes.append({"show": show.keyword, "title": torrent.title, "reason": reason})
            continue
        parsed = parse_episode(torrent.title)
        if parsed is None:
            notes.append({"show": show.keyword, "title": torrent.title, "reason": "集数不确定"})
            continue
        season, episode = parsed
        pick = Pick(show.keyword, season, episode, torrent, score)
        current = best.get((season, episode))
        if current is None or _better(pick, current):
            best[(season, episode)] = pick
    return list(best.values()), notes


def score_torrent(
    show: Show,
    torrent: Torrent,
    options: SearchOptions,
    now: datetime,
) -> tuple[int | None, str]:
    """返回分数。被丢掉时分数为空，并给出原因。"""
    title = torrent.title or ""
    if not title_matches(title, show):
        return None, "标题对不上"
    if options.require_sub and _RAW.search(title):
        return None, "生肉"
    if options.resolution.lower() != "4k" and _UHD.search(title):
        return None, "4K"
    if not options.allow_batch and _BATCH.search(title):
        return None, "合集"
    if torrent.published is not None and _too_old(torrent.published, now, options.lookback_hours):
        return None, "超出回溯时间"
    if torrent.size:
        if torrent.size < options.min_episode_mb * 1024 * 1024:
            return None, "体积过小"
        if torrent.size > options.max_episode_gb * 1024 ** 3:
            return None, "体积过大"
    score = 0
    groups = [*show.groups, *options.fansub_prefer]
    if any(group and group.casefold() in title.casefold() for group in groups):
        score += 100
    wanted = options.resolution
    if wanted and wanted.casefold() in title.casefold():
        score += 40
    if re.search(r"简体|简中|CHS|\bGB\b", title, re.IGNORECASE):
        score += 30
    elif "简繁" in title:
        score += 10
    elif re.search(r"繁体|CHT|BIG5", title, re.IGNORECASE):
        score += 5
    return score, ""


def _better(candidate: Pick, current: Pick) -> bool:
    if candidate.score != current.score:
        return candidate.score > current.score
    left = candidate.torrent.published or datetime.min.replace(tzinfo=timezone.utc)
    right = current.torrent.published or datetime.min.replace(tzinfo=timezone.utc)
    if left != right:
        return left > right
    return candidate.torrent.size > current.torrent.size


def _too_old(published: datetime, now: datetime, hours: int) -> bool:
    left = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
    right = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return (right - left).total_seconds() > hours * 3600


def _infohash(magnet: str) -> str:
    """40 位 hex 直接小写。32 位 base32 先解码成 20 字节再转 hex。"""
    matched = _BTIH.search(magnet or "")
    if not matched:
        return ""
    token = matched.group(1)
    if len(token) == 40:
        return token.lower()
    try:
        raw = base64.b32decode(token.upper())
    except Exception:
        return ""
    if len(raw) != 20:
        return ""
    return raw.hex()


def _published(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None


def _size_in_text(value: str) -> int:
    matched = _SIZE.search(value or "")
    if not matched:
        return 0
    number = float(matched.group(1))
    unit = _UNIT[matched.group(2).upper()]
    return int(number * unit)


def _text(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
