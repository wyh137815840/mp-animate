"""追更名单、集数和路径。这些函数不访问网络。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_SEASON_WORD = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


@dataclass
class Show:
    """一部追更。"""

    keyword: str
    aliases: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    tmdb_id: int | None = None
    airing: bool = False

    def names(self) -> list[str]:
        """搜索词和别名，长的排前面，避免短别名先命中。"""
        values = [self.keyword, *self.aliases]
        return sorted({item for item in values if item}, key=len, reverse=True)


def parse_watchlist(text: str) -> list[Show]:
    """解析多行追更。`剧名 | 别名 | group=字幕组 | tmdb_id=编号 | 连载中`。`#` 开头是注释。"""
    shows = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        keyword = parts[0]
        if not keyword:
            continue
        aliases = []
        groups = []
        tmdb_id = None
        airing = False
        for part in parts[1:]:
            if not part:
                continue
            lowered = part.casefold()
            if lowered.startswith("group="):
                groups.extend(item.strip() for item in part.split("=", 1)[1].split(",") if item.strip())
            elif lowered.startswith("tmdb_id=") or lowered.startswith("tmdb="):
                token = part.split("=", 1)[1].strip()
                if token.isdigit():
                    tmdb_id = int(token)
            elif part == "连载中" or lowered == "airing":
                airing = True
            else:
                aliases.append(part)
        shows.append(Show(keyword=keyword, aliases=aliases, groups=groups, tmdb_id=tmdb_id, airing=airing))
    return shows


def norm(path: str) -> str:
    """整理成以 / 开头、不带结尾斜杠的路径。"""
    text = (path or "").replace("\\", "/").strip()
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def strip_mount_prefix(path: str, prefix: str) -> str:
    """去掉 CloudDrive 挂载前缀。前缀对不上时原样返回。"""
    path = norm(path)
    mount = norm(prefix) if (prefix or "").strip() else ""
    if not mount or mount == "/":
        return path
    if path == mount:
        return "/"
    if path.startswith(mount + "/"):
        return norm(path[len(mount):])
    return path


def name_of(path: str) -> str:
    """路径最后一段。"""
    path = norm(path)
    if path == "/":
        return ""
    return path.rsplit("/", 1)[-1]


def parent_path(path: str) -> str:
    """上级目录。"""
    path = norm(path)
    if path == "/":
        return "/"
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


def join_path(parent: str, name: str) -> str:
    """拼接路径。"""
    parent = norm(parent)
    if parent == "/":
        return "/" + name
    return parent + "/" + name


def title_matches(title: str, show: Show) -> bool:
    """标题里要出现搜索词或任一别名。"""
    folded = (title or "").casefold()
    return any(name.casefold() in folded for name in show.names())


def parse_episode(title: str) -> tuple[int, int] | None:
    """从发布标题里取季和集。对不上、或有两套不同集数时返回空。"""
    text = title or ""
    if re.search(r"OVA|特典|剧场版|劇場版|\bSP\b", text, re.IGNORECASE):
        return None
    seasons = set()
    episodes = set()
    season_episode = re.search(r"S(\d{1,2})\s*E(\d{1,3})", text, re.IGNORECASE)
    if season_episode:
        seasons.add(int(season_episode.group(1)))
        episodes.add(int(season_episode.group(2)))
    for spoken in re.finditer(r"第\s*(\d{1,3})\s*[话話集]", text):
        episodes.add(int(spoken.group(1)))
    for dashed in re.finditer(r"[-–]\s*(\d{1,3})(?:\s*v\d+)?(?=\s|\[|【|$)", text, re.IGNORECASE):
        episodes.add(int(dashed.group(1)))
    for bracket in re.finditer(r"\[(\d{1,3})(?:\s*v\d+)?\]", text, re.IGNORECASE):
        episodes.add(int(bracket.group(1)))
    if len(episodes) != 1:
        return None
    if len(seasons) > 1:
        return None
    if seasons:
        season = next(iter(seasons))
    else:
        season = _season_from_words(text)
    episode = next(iter(episodes))
    if episode <= 0 or season <= 0:
        return None
    return season, episode


def _season_from_words(text: str) -> int:
    numbered = re.search(r"第\s*([一二三四五六七八九十]|\d{1,2})\s*季", text)
    if numbered:
        token = numbered.group(1)
        if token.isdigit():
            return int(token)
        return _SEASON_WORD.get(token, 1)
    if re.search(r"2nd\s*season", text, re.IGNORECASE):
        return 2
    return 1


def safe_dir_name(name: str) -> str:
    """去掉 Windows 路径里不能用的字符，供季目录上一级使用。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "unknown"
