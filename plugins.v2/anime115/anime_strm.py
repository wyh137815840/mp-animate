"""本地 strm。只写播放地址，不写 nfo、海报。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SXXEXX = re.compile(r"S(\d{1,2})E(\d{1,3})", re.IGNORECASE)


def build_play_url(play_base: str, pickcode: str, filename: str) -> str:
    """和 115 AV刮削一样，把 {pickcode}、{filename} 填进播放模板。"""
    template = (play_base or "").strip()
    code = quote((pickcode or "").strip(), safe=".-_")
    name = quote((filename or "video").strip() or "video", safe=".-_()[] ")
    if "{pickcode}" in template or "{filename}" in template or "{file_name}" in template:
        return (
            template.replace("{pickcode}", code)
            .replace("{filename}", name)
            .replace("{file_name}", name)
        )
    base = template.rstrip("/")
    return f"{base}/redirect?pickcode={code}"


def pickcode_of(node) -> str:
    """115 的 pickcode。移动和改名都不会变。"""
    raw = getattr(node, "raw", None)
    if isinstance(raw, dict):
        value = raw.get("pc") or raw.get("pickcode") or raw.get("pick_code") or ""
    else:
        value = getattr(raw, "pickcode", None) or getattr(raw, "pick_code", None) or ""
    return str(value or "").strip()


def episode_strm_name(show_folder: str, season: int, episode: int, original_name: str) -> str:
    """同一季里用相同的 SxxExx 收成 Emby 组合版本，发行名留在后面。"""
    show = _safe_part(show_folder)
    stem = _safe_part(_stem(original_name))
    token = f"S{int(season):02d}E{int(episode):02d}"
    if _SXXEXX.search(stem) and token.lower() in stem.lower():
        base = stem
    else:
        base = f"{show} - {token} - {stem}"
    suffix = ".strm"
    if len((base + suffix).encode("utf-8")) <= 240:
        return base + suffix
    prefix = f"{show} - {token} - "
    room = 240 - len(suffix.encode("utf-8")) - len(prefix.encode("utf-8"))
    trimmed = stem.encode("utf-8")[: max(room, 0)].decode("utf-8", "ignore").rstrip()
    return f"{prefix}{trimmed}".rstrip(" -") + suffix


def strm_relative(show_folder: str, season: int, episode: int, original_name: str) -> str:
    """strm 目录下的相对路径。季目录跟库里的 Season 文件夹对齐。"""
    show = _safe_part(show_folder)
    season_dir = f"Season {int(season):02d}"
    return f"{show}/{season_dir}/{episode_strm_name(show_folder, season, episode, original_name)}"


def write_strm(root: str, relative: str, url: str) -> str:
    """把一行播放地址写到 strm 根下。不创建 nfo 或图片。"""
    base = Path(root).expanduser().resolve()
    parts = [part for part in relative.split("/") if part and part not in (".", "..")]
    if not parts:
        raise OSError("strm 路径为空")
    target = (base / Path(*parts)).resolve()
    if base != target and base not in target.parents:
        raise OSError("strm 路径越界")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(url.strip() + "\n", encoding="utf-8")
    return str(target)


def _stem(name: str) -> str:
    text = name or ""
    if "." in text:
        return text.rsplit(".", 1)[0]
    return text


def _safe_part(name: str) -> str:
    cleaned = _INVALID.sub(" ", name or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "unknown"
