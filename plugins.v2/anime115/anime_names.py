"""追更名单、集数和路径。这些函数不访问网络。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


_SEASON_WORD = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
# 繁体到简体，只收简繁成对且不会改成另一个常用字的。用来让「探险家」对上「探險家」。
_HANT_HANS_PAIRS = (
    "險险", "學学", "對对", "嬌娇", "東东", "國国", "門门", "開开", "關关", "長长",
    "電电", "視视", "網网", "語语", "話话", "書书", "畫画", "後后", "從从", "為为",
    "時时", "間间", "會会", "來来", "這这", "個个", "們们", "說说", "還还", "過过",
    "發发", "現现", "實实", "與与", "無无", "愛爱", "親亲", "見见", "覺觉", "讓让",
    "請请", "謝谢", "點点", "頭头", "題题", "風风", "飛飞", "馬马", "體体", "條条",
    "樂乐", "機机", "氣气", "漢汉", "滿满", "燈灯", "貓猫", "蟲虫", "衛卫", "裡里",
    "裏里", "麼么", "樣样", "號号", "節节", "級级", "約约", "紅红", "線线", "練练",
    "組组", "終终", "結结", "給给", "統统", "續续", "義义", "聲声", "聽听", "臉脸",
    "興兴", "術术", "裝装", "複复", "觀观", "記记", "許许", "設设", "試试", "認认",
    "識识", "譯译", "議议", "護护", "變变", "貝贝", "賽赛", "轉转", "輕轻", "邊边",
    "達达", "運运", "選选", "適适", "醫医", "鐵铁", "陽阳", "隊队", "難难", "靈灵",
    "靜静", "頁页", "預预", "領领", "飯饭", "鬥斗", "龍龙", "龜龟", "雲云", "鐘钟",
)
_HANT_TO_HANS = str.maketrans(
    "".join(pair[0] for pair in _HANT_HANS_PAIRS),
    "".join(pair[1] for pair in _HANT_HANS_PAIRS),
)


@dataclass
class Show:
    """一部追更。"""

    keyword: str
    aliases: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    tmdb_id: int | None = None
    airing: bool = False
    season: int | None = None

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
    """标题里要出现搜索词或任一别名。简繁、星号和空格先折成同一种写法。"""
    folded = _fold_name(title)
    for name in show.names():
        folded_name = _fold_name(name)
        if folded_name and folded_name in folded:
            return True
    return False


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
        if _SEASON_RANGE_PREFIX.search(text[:matched.start(1)]):
            continue
        first, second = int(matched.group(1)), int(matched.group(2))
        # 集数从 1 开始；86 - 01 这类数字剧名不是范围。
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


def _season_from_words(text: str) -> int:
    """没有明确季标记时算第 1 季。"""
    seasons = _seasons_in_text(text)
    return min(seasons) if seasons else 1


def safe_dir_name(name: str) -> str:
    """去掉 Windows 路径里不能用的字符，供季目录上一级使用。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "unknown"
