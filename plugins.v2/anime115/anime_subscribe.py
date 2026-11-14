"""从 MoviePilot 订阅里挑出动漫。普通电视剧和电影不进动漫花园。"""

from __future__ import annotations

from .anime_names import Show


# TMDB 的 Animation。MoviePilot 自己的动漫分类也用这个号。
ANIME_GENRE_ID = 16
_ANIME_SOURCES = {"bangumi", "anilist"}
_ANIME_CATEGORY = ("动漫", "动画", "動漫", "日番", "国漫", "番剧", "新番")
_ANIME_GENRE_NAMES = {"animation", "动画", "動漫", "anime"}
_MOVIE = {"电影", "movie"}
_ACTIVE = {"", "n", "r", "新建", "订阅中"}


def load_mp_subscriptions() -> list[dict]:
    """读 MoviePilot 订阅表。没有安装宿主时抛错，调用方改报读不到订阅。"""
    from app.db.subscribe_oper import SubscribeOper

    rows = SubscribeOper().list() or []
    return [_row(item) for item in rows]


def shows_from_subscriptions(rows, genres_of=None, aliases_of=None) -> tuple[list[Show], list[dict]]:
    """留下会去搜 dmhy 的动漫。其余每条一个跳过原因。

    aliases_of 可以给 TMDB 别名，用来分辨同名别的剧。
    """
    shows = []
    notes = []
    seen = set()
    for raw in rows or []:
        row = raw if isinstance(raw, dict) else _row(raw)
        show, reason = classify_subscription(row, genres_of)
        if show is not None and aliases_of is not None:
            show.extra_aliases = list(aliases_of(show) or [])
        name = str(row.get("name") or row.get("keyword") or "")
        if show is None:
            notes.append({"show": name, "title": "", "reason": reason})
            continue
        key = (show.tmdb_id or 0, show.season or 0, show.keyword)
        if key in seen:
            continue
        seen.add(key)
        shows.append(show)
    return shows, notes


def classify_subscription(row: dict, genres_of=None) -> tuple[Show | None, str]:
    """动漫返回 Show。电影、普通剧、暂停的订阅返回空和原因。"""
    if _inactive(row.get("state")):
        return None, "未在订阅"
    if _is_movie(row.get("type")):
        return None, "电影"
    if _text_has_anime(row.get("type")):
        return _show_from(row), "类型是动漫"
    if _from_anime_source(row):
        return _show_from(row), "动漫数据源"
    if _text_has_anime(row.get("media_category")):
        return _show_from(row), "分类是动漫"
    own_genres = _genres_of_row(row)
    if ANIME_GENRE_ID in own_genres or _named_animation(row):
        return _show_from(row), "类型是动画"
    if own_genres:
        return None, "不是动画"
    tmdb_id = _tmdb_id(row)
    if tmdb_id and genres_of is not None:
        looked = genres_of(tmdb_id)
        if looked is None:
            return None, "无法确认是动画"
        if ANIME_GENRE_ID in _as_ids(looked):
            return _show_from(row), "TMDB 动画"
        return None, "不是动画"
    return None, "无法确认是动画"


def _show_from(row: dict) -> Show:
    name = str(row.get("name") or "").strip()
    keyword = str(row.get("keyword") or "").strip()
    search = keyword or name or "未知"
    aliases = [name] if name and name != search else []
    return Show(
        keyword=search,
        aliases=aliases,
        tmdb_id=_tmdb_id(row),
        airing=True,
        season=_season(row.get("season")),
    )


def _row(item) -> dict:
    if isinstance(item, dict):
        return item
    return {
        "name": getattr(item, "name", "") or "",
        "year": getattr(item, "year", "") or "",
        "type": getattr(item, "type", "") or "",
        "keyword": getattr(item, "keyword", "") or "",
        "tmdbid": getattr(item, "tmdbid", None),
        "bangumiid": getattr(item, "bangumiid", None),
        "anilistid": getattr(item, "anilistid", None),
        "media_source": getattr(item, "media_source", "") or "",
        "media_category": getattr(item, "media_category", "") or "",
        "season": getattr(item, "season", None),
        "state": getattr(item, "state", "") or "",
        "note": getattr(item, "note", None),
        "genre_ids": getattr(item, "genre_ids", None),
        "total_episode": getattr(item, "total_episode", None),
    }


def _inactive(state) -> bool:
    text = str(state or "").strip()
    if text.casefold() in _ACTIVE or text in _ACTIVE:
        return False
    return True


def _is_movie(value) -> bool:
    text = str(value or "").strip()
    return text.casefold() in _MOVIE or text == "电影"


def _from_anime_source(row: dict) -> bool:
    source = str(row.get("media_source") or "").strip().casefold()
    if source in _ANIME_SOURCES:
        return True
    return bool(_positive_int(row.get("bangumiid")) or _positive_int(row.get("anilistid")))


def _text_has_anime(value) -> bool:
    text = str(value or "")
    return any(token in text for token in _ANIME_CATEGORY)


def _named_animation(row: dict) -> bool:
    blobs = [row.get("genres"), (row.get("note") or {}).get("genres") if isinstance(row.get("note"), dict) else None]
    for blob in blobs:
        if isinstance(blob, str) and any(name.casefold() in blob.casefold() for name in _ANIME_GENRE_NAMES):
            return True
        if isinstance(blob, list):
            for item in blob:
                name = item.get("name") if isinstance(item, dict) else str(item or "")
                if str(name).strip().casefold() in _ANIME_GENRE_NAMES:
                    return True
    return False


def _genres_of_row(row: dict) -> set[int]:
    found = set(_as_ids(row.get("genre_ids")))
    note = row.get("note")
    if isinstance(note, dict):
        found.update(_as_ids(note.get("genre_ids")))
        found.update(_as_ids(note.get("genres")))
    found.update(_as_ids(row.get("genres")))
    return found


def _as_ids(value) -> set[int]:
    found = set()
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, int):
        return {value} if value > 0 else set()
    if isinstance(value, str):
        for part in value.replace("，", ",").split(","):
            if part.strip().isdigit():
                found.add(int(part.strip()))
        return found
    if isinstance(value, dict):
        try:
            number = int(value.get("id"))
        except (TypeError, ValueError):
            return found
        return {number} if number > 0 else set()
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found.update(_as_ids(item))
    return found


def _tmdb_id(row: dict) -> int | None:
    return _positive_int(row.get("tmdbid") if row.get("tmdbid") not in (None, "") else row.get("tmdb_id"))


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _season(value) -> int | None:
    number = _positive_int(value)
    return number


def parse_cap_overrides(text: str) -> dict:
    """手动写的每季集数上限。

    每行一部，形如「剧名=1:11,2:13,3:11」。有些番在 TMDB 上被压成一季
    （比如《我推的孩子》是单季 35 集），订阅里的上限会挡住跨季连号换算，
    这时用它纠正。优先于 MoviePilot 订阅里的值。
    """
    result = {}
    for line in str(text or "").splitlines():
        row = line.strip()
        if not row or "=" not in row:
            continue
        name, _, tail = row.partition("=")
        show = name.strip()
        if not show:
            continue
        for chunk in tail.split(","):
            piece = chunk.strip()
            if not piece or ":" not in piece:
                continue
            season_text, _, size_text = piece.partition(":")
            try:
                season = int(season_text)
                size = int(size_text)
            except ValueError:
                continue
            if season > 0 and size > 0:
                result[(show, season)] = size
    return result
