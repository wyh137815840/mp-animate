"""追更名单、集数和路径。这些函数不访问网络。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


_SEASON_WORD = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
# 繁体到简体用内置的完整字表（4720 对常用字），见 anime_hant。
# 早先只收了 120 对字，碧藍之海、葬送的芙莉蓮这类繁体发布名折不过来，整部剧会搜不到。
from .anime_hant import HANT_TO_HANS as _HANT_TO_HANS


@dataclass
class Show:
    """一部追更。"""

    keyword: str
    aliases: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    tmdb_id: int | None = None
    airing: bool = False
    season: int | None = None
    # TMDB 给的各语言别名。用来分辨同名别的剧：
    # 《死神》的别名里有 BLEACH、千年血战篇，别剧没有。
    extra_aliases: list[str] = field(default_factory=list)

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
    """标题里要出现搜索词或任一别名。简繁、星号和空格先折成同一种写法。

    剧名只是另一个剧名的前缀时不算命中：「死神」不等于「死神少爷与黑女仆」。
    后面接的是季标记（二期、S2、Season 2）或发行形态（全集、合集）时仍然算命中。
    """
    kept = _fold_keep(title)
    folded_title = _fold_name(title)
    for name in show.names():
        name_kept = _fold_keep(name)
        if not name_kept:
            continue
        index = kept.find(name_kept)
        if index >= 0:
            rest = kept[index + len(name_kept):]
            if _continues_longer_name(rest):
                continue
            return True
        # 剧名里带符号（如「魔法★探险家」）时，用去掉分隔符的写法再找一次。
        folded_name = _fold_name(name)
        if folded_name and folded_name in folded_title:
            return True
    # 别名命中就认。TMDB 别名够特别（BLEACH、千年血战篇、ブリーチ），
    # 能把本剧的续作名和同名别的剧分开。
    for alias in show.extra_aliases:
        folded_alias = _fold_name(alias)
        if not folded_alias or folded_alias in _fold_name(show.keyword):
            continue
        if len(folded_alias) < 3 and not folded_alias.isascii():
            continue
        if folded_alias in folded_title:
            return True
    return False


def _fold_keep(text: str) -> str:
    """折叠繁简与大小写，但保留空格和分隔符：判断剧名边界要用。"""
    return unicodedata.normalize("NFKC", text or "").casefold().translate(_HANT_TO_HANS)


def _continues_longer_name(rest: str) -> bool:
    """剧名后面紧跟着中文时，说明这是另一个更长的剧名。

    「死神少爺與黑女僕」里的「死神」是另一部剧；
    「死神 千年血战篇」「葬送的芙莉莲 / 葬送のフリーレン」是同一部。
    """
    if not rest:
        return False
    if _SEASON_AFTER_NAME.match(rest):
        return False
    if _BATCH_AFTER_NAME.match(rest):
        return False
    return _is_cjk(rest[0])


# 剧名后面接这些词时仍是同一部剧：合集、全集、剧场版等是发行形态，不是另一部剧。
_BATCH_AFTER_NAME = re.compile(
    r"^(?:全集|合集|总集|總集|全季|完结|完結|剧场版|劇場版|电影版|電影版"
    r"|特别篇|特別篇|特典|ova|sp|tv版|bd版|bdrip|webrip)"
)

# 剧名后面接这些东西时，仍算同一部剧：第二季、二期、Season 2、2nd Season、SP、OVA。
_SEASON_AFTER_NAME = re.compile(
    r"^(?:第?[一二三四五六七八九十0-9]{1,3}[季期]"
    r"|s[0-9]{1,2}(?:e[0-9]{1,3})?"
    r"|season[0-9]{1,2}"
    r"|[0-9]{1,2}(?:st|nd|rd|th)season"
    r"|sp[0-9]{1,2}?|ova)"
)


def _is_cjk(ch: str) -> bool:
    """是不是汉字或假名。中文续接才可能是另一部更长的剧名。"""
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def _fold_name(text: str) -> str:
    """去掉空白和符号，并把常见繁体折成简体。拉丁字母按小写比较。"""
    normalized = unicodedata.normalize("NFKC", text or "").casefold().translate(_HANT_TO_HANS)
    return "".join(ch for ch in normalized if _kept(ch))


def _kept(ch: str) -> bool:
    if ch.isalnum():
        return True
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


# 季标记只从明确写法里取，避免把分辨率、版本号、码率当成季。
_SEASON_EPISODE = re.compile(r"S(\d{1,2})\s*E(\d{1,3})", re.IGNORECASE)
_SEASON_CN = re.compile(r"第\s*(\d{1,2}|[一二三四五六七八九十])\s*[季期]")
_SEASON_CN_PERIOD = re.compile(r"(\d{1,2}|[一二三四五六七八九十])\s*期")
_SEASON_EN = re.compile(r"season\s*(\d{1,2})|(\d{1,2})\s*(?:st|nd|rd|th)\s*season", re.IGNORECASE)
_S_SEASON_EPISODE = re.compile(r"(?<![0-9A-Za-z])S(\d{1,2})\s*[-–~]\s*(\d{1,3})(?![0-9A-Za-z])", re.IGNORECASE)
# 整包范围：01-12、03~04。S/E 前缀（S2 - 05、S01E03）和 Season 2 - 05 里的季号不算。
# 前后挡住数字和点，避免把日期（2024-2025、2024.01.15）和 5.1 这类数字当成范围。
_EPISODE_RANGE = re.compile(
    r"(?<![\d.\-SEse])(\d{1,3})\s*[-–~]\s*(\d{1,3})(?:\s*v\d+)?(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_SEASON_RANGE_PREFIX = re.compile(r"(?:season|第)\s*$", re.IGNORECASE)
# 数字直接贴在单词后面时，只有这些集数标记才算范围（EP01-12），否则多半是剧名编号（Show001 - 02）。
_EPISODE_RANGE_MARKER = re.compile(r"(?:episode|ep|e|vol|volume|#)\s*\.?\s*$", re.IGNORECASE)
_VIDEO_EXT = r"\.(?:mkv|mp4|avi|ts|m2ts|flv|wmv|mov|webm|rmvb|m4v|iso|ogm)\b"
# 发布标题常以 .mkv 结尾，集数后面跟着扩展名也要认。
_DASHED_EPISODE = re.compile(
    rf"[-–]\s*(\d{{1,3}})(?:\s*v\d+)?(?=\s|\[|【|$|{_VIDEO_EXT})",
    re.IGNORECASE,
)
_BRACKET_EPISODE = re.compile(r"\[(\d{1,3})(?:\s*v\d+)?\]", re.IGNORECASE)
_SPOKEN_EPISODE = re.compile(r"第\s*(\d{1,3})\s*[话話集]")


def parse_episode(title: str) -> tuple[int, int] | None:
    """从发布标题里取季和集。对不上、有两套不同集数或季、或是整包范围时返回空。"""
    text = title or ""
    if re.search(r"OVA|特典|剧场版|劇場版|\bSP\b", text, re.IGNORECASE):
        return None
    if _has_episode_range(text):
        return None
    episodes = set()
    for matched in _SEASON_EPISODE.finditer(text):
        episodes.add(int(matched.group(2)))
    for matched in _S_SEASON_EPISODE.finditer(text):
        episodes.add(int(matched.group(2)))
    for spoken in _SPOKEN_EPISODE.finditer(text):
        episodes.add(int(spoken.group(1)))
    for dashed in _DASHED_EPISODE.finditer(text):
        episodes.add(int(dashed.group(1)))
    for bracket in _BRACKET_EPISODE.finditer(text):
        episodes.add(int(bracket.group(1)))
    if len(episodes) != 1:
        return None
    seasons = _seasons_in_text(text)
    if len(seasons) > 1:
        return None
    season = next(iter(seasons), 1)
    episode = next(iter(episodes))
    if episode <= 0 or season <= 0:
        return None
    return season, episode


def _has_episode_range(text: str) -> bool:
    """01-12、03~04 这类整包范围。S2 - 05、Season 2 - 05 里的季号不算范围。"""
    for matched in _EPISODE_RANGE.finditer(text):
        prefix = text[:matched.start(1)]
        if _SEASON_RANGE_PREFIX.search(prefix):
            continue
        # Show001 - 02、x264-10 这类数字属于剧名/编码，不是集数范围；
        # EP01-12 这种明确带集数前缀的写法仍按范围处理。
        if prefix and prefix[-1].isalpha() and not _EPISODE_RANGE_MARKER.search(prefix):
            continue
        first, second = int(matched.group(1)), int(matched.group(2))
        # 集数从 1 开始，且范围必须升序；86 - 01 是单集标题。
        if 0 < first < second:
            return True
    return False


def _seasons_in_text(text: str) -> set[int]:
    """标题里出现的季。SxxExx、第N季/期、N期、Season N、Nth Season、S2 - 05。"""
    found = set()
    for matched in _SEASON_EPISODE.finditer(text):
        found.add(int(matched.group(1)))
    for matched in _SEASON_CN.finditer(text):
        found.add(_season_number(matched.group(1)))
    for matched in _SEASON_CN_PERIOD.finditer(text):
        found.add(_season_number(matched.group(1)))
    for matched in _SEASON_EN.finditer(text):
        found.add(int(matched.group(1) or matched.group(2)))
    for matched in _S_SEASON_EPISODE.finditer(text):
        found.add(int(matched.group(1)))
    found.discard(0)
    return found


def _season_number(token: str) -> int:
    if token.isdigit():
        return int(token)
    return _SEASON_WORD.get(token, 1)


# 集数范围：01-12、1-24。前面挡住 S/E 和数字，避免把 S01E03、日期当范围。
_RANGE_EPISODE = re.compile(
    # 范围后面可以跟字母（01-13TV全集、01-13END），但后面不能再跟数字。
    r"(?<![\d.\-SEse])(\d{1,3})\s*[-\u2013~]\s*(\d{1,3})(?:\s*v\d+)?(?![0-9])",
    re.IGNORECASE,
)
# 标题里明确写出的季号。
_SEASON_MARKER = re.compile(
    r"(?:第\s*([0-9]{1,2}|[一二三四五六七八九十])\s*[季期]"
    r"|season\s*([0-9]{1,2})"
    r"|([0-9]{1,2})\s*(?:st|nd|rd|th)\s*season"
    r"|(?<![0-9a-z])s([0-9]{1,2})(?![0-9]))",
    re.IGNORECASE,
)


def parse_episode_range(title: str) -> tuple[int, int] | None:
    """标题里的集数范围，如 01-12、1-24。不是范围就返回空。"""
    for matched in _RANGE_EPISODE.finditer(title or ""):
        first, second = int(matched.group(1)), int(matched.group(2))
        if 0 < first < second <= 999:
            return first, second
    return None


def explicit_season(title: str) -> int | None:
    """标题里明确写出的季号。没写就返回空，不猜。"""
    matched = _SEASON_MARKER.search(title or "")
    if not matched:
        return None
    for token in matched.groups():
        if not token:
            continue
        if token.isdigit():
            return int(token)
        return _SEASON_WORD.get(token, 1)
    return None


def _season_from_words(text: str) -> int:
    """没有明确季标记时算第 1 季。"""
    seasons = _seasons_in_text(text)
    return min(seasons) if seasons else 1


def safe_dir_name(name: str) -> str:
    """去掉 Windows 路径里不能用的字符，供季目录上一级使用。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "unknown"
