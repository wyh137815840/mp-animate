"""整包里的字幕。

Emby 播 .strm 时，视频走 115 直链，字幕必须是本地真实文件，
所以这里把 115 上的字幕按集数找出来，下载一份放到 .strm 旁边同名的地方。
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from .anime_names import parse_episode

# 能直接当字幕用的扩展名
SUB_SUFFIXES = {".ass", ".srt", ".ssa", ".vtt", ".sup", ".sub"}
# Emby 实测认的外挂字幕格式。sup、sub 不在里面，写出去也挂不上。
SIDECAR_SUFFIXES = {".ass", ".srt", ".ssa", ".vtt"}
# 常见的字幕目录名
SUB_DIR_NAMES = {
    "subs", "sub", "subtitle", "subtitles", "字幕", "字幕文件", "ass", "srt",
    "chs", "cht", "chinese", "简体", "繁體", "繁体", "中字", "特效字幕",
}
# 字幕不会很大，超过这个大小多半不是字幕
MAX_SUB_BYTES = 32 * 1024 * 1024
_LEADING_NUMBER = re.compile(r"^(\d{1,3})(?:\D|$)")
_BRACKET_NUMBER = re.compile(r"[\[【]\s*(\d{1,3})\s*[\]】]")
# 结尾的集数：某剧 03、某剧 - 07。前面不能再有数字，免得把 2024 当成 24。
_TRAILING_NUMBER = re.compile(r"(?<!\d)(\d{1,3})\s*$")


def suffix_of(name: str) -> str:
    """小写的扩展名，带点。没有扩展名返回空。"""
    text = (name or "").strip()
    if "." not in text:
        return ""
    return "." + text.rsplit(".", 1)[-1].lower()


def stem_of(name: str) -> str:
    """去掉扩展名的文件名。"""
    text = (name or "").strip()
    suffix = suffix_of(text)
    return text[: -len(suffix)] if suffix and suffix != text else text


def is_subtitle_name(name: str) -> bool:
    """按扩展名判断是不是字幕。"""
    return suffix_of(name) in SUB_SUFFIXES


def is_subtitle_dir(name: str) -> bool:
    """目录名像字幕目录。字幕组常写成 Subs(简体)、字幕[CHS]。"""
    text = (name or "").strip().lower()
    if not text:
        return False
    if text in SUB_DIR_NAMES:
        return True
    core = re.sub(r"[\(\[\{].*?[\)\]\}]", "", text).strip()
    return core in SUB_DIR_NAMES


def episode_of(name: str) -> int:
    """字幕文件名里的集数。认不出返回 0。"""
    stem = stem_of(name)
    parsed = parse_episode(stem)
    if parsed:
        return int(parsed[1])
    matched = _LEADING_NUMBER.match(stem)
    if matched:
        number = int(matched.group(1))
        return number if 0 < number < 1000 else 0
    matched = _BRACKET_NUMBER.search(stem)
    if matched:
        number = int(matched.group(1))
        return number if 0 < number < 1000 else 0
    matched = _TRAILING_NUMBER.search(stem)
    if matched:
        number = int(matched.group(1))
        return number if 0 < number < 1000 else 0
    return 0


def _same_stem(left: str, right: str) -> bool:
    """两个文件名去掉扩展名后是不是同一个。"""
    return stem_of(left).strip().casefold() == stem_of(right).strip().casefold()


def match_subtitles(files, video, episode: int, season: int = 0) -> list:
    """给一集找出字幕文件。

    顺序：视频同目录里的同名字幕；同目录里集数对得上的；
    再看别的目录（常见是 Subs、字幕）里集数对得上的。
    """
    home = _parent(video.path)
    video_stem = stem_of(video.name)
    same_dir = []
    elsewhere = []
    for item in files or []:
        if getattr(item, "is_dir", False):
            continue
        if not is_subtitle_name(item.name):
            continue
        if _parent(item.path) == home:
            same_dir.append(item)
        else:
            elsewhere.append(item)
    picked = []
    seen = set()

    def take(node):
        if node.path in seen:
            return
        seen.add(node.path)
        picked.append(node)

    for item in same_dir:
        # 常见字幕名是 video.SC.ass / video.TC.ass / video.zh-CN.srt。
        # 主干后带语言后缀时也应匹配，但不能把 Show - 01 匹配成 Show - 010。
        sub_stem = stem_of(item.name).casefold()
        if _same_stem(item.name, video_stem) or sub_stem.startswith(video_stem.casefold() + "."):
            take(item)
    if episode > 0:
        for item in same_dir:
            if episode_of(item.name) == episode:
                take(item)
        for item in elsewhere:
            if episode_of(item.name) == episode:
                take(item)
    return picked


def _parent(path: str) -> str:
    text = (path or "").strip()
    if "/" not in text:
        return ""
    return text.rsplit("/", 1)[0]


def language_suffix(name: str) -> str:
    """字幕的语言码。

    Emby 认同目录同主干名的外挂字幕，但没语言码时客户端不一定自动选中文，
    所以写的时候补上 zh-CN；文件名里写明繁体的按繁体算。
    """
    text = (name or "").lower()
    if re.search(r"(?:^|[.\[\s_-])(?:tc|zh-tw)(?:[.\]\s_-]|$)", text) or any(
        word in text for word in ("繁体", "繁體", "big5", "cht", "繁")
    ):
        return "zh-TW"
    return "zh-CN"


def subtitle_relative(strm_relative: str, subtitle_name: str) -> str:
    """字幕要放在 .strm 旁边，名字只换扩展名。

    Emby 认的是「同目录、同主干名」：主干要一模一样，扩展名随意，
    中间可以再带一个语言码。
    """
    relative = (strm_relative or "").strip()
    suffix = suffix_of(subtitle_name) or ".ass"
    head, _, _tail = relative.rpartition("/")
    stem = stem_of(relative.rsplit("/", 1)[-1])
    name = f"{stem}.{language_suffix(subtitle_name)}{suffix}"
    if len(name.encode("utf-8")) > 240:
        room = 240 - len(f".{language_suffix(subtitle_name)}{suffix}".encode("utf-8"))
        stem = stem.encode("utf-8")[: max(room, 0)].decode("utf-8", "ignore").rstrip()
        name = f"{stem}.{language_suffix(subtitle_name)}{suffix}"
    return f"{head}/{name}" if head else name


def unpack(data: bytes, name: str) -> bytes:
    """字幕是 zip 时，取出里面的字幕文件。"""
    if suffix_of(name) != ".zip":
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.namelist():
                if entry.endswith("/"):
                    continue
                if suffix_of(entry) in SUB_SUFFIXES:
                    return archive.read(entry)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return data
    return data


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    """按播放地址模板下载文件内容。字幕和视频走同一套 115 直链。

    请求要带 UA：115 的下载地址是按 UA 签的，换了 UA 会 403。
    """
    import httpx

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        data = response.content
    if not data:
        raise ValueError("字幕内容是空的")
    if len(data) > MAX_SUB_BYTES:
        raise ValueError(f"字幕太大：{len(data)} 字节")
    return data


def write_sidecar(root: str, relative: str, data: bytes) -> str:
    """把字幕写到 strm 目录下的同名位置。"""
    from .anime_strm import write_bytes

    return write_bytes(root, relative, data)
