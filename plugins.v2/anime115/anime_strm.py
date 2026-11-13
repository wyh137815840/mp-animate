"""本地 strm。只写播放地址，不写 nfo、海报。"""

from __future__ import annotations

import errno
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SXXEXX = re.compile(r"S(\d{1,2})E(\d{1,3})", re.IGNORECASE)


def build_play_url(play_base: str, pickcode: str, filename: str) -> str:
    """和 115 AV刮削一样，把 {pickcode}、{filename} 填进播放模板。"""
    template = (play_base or "").strip()
    code = quote((pickcode or "").strip(), safe=".-_")
    # 文件名里的空格必须编码成 %20：以前 strm 地址是服务器自己去拉，HTTP 库会顺手编码；
    # 现在（MediaWarp 302 直链）是手机/客户端自己去拉，URL 里带裸空格或裸方括号很多客户端会直接报错。
    name = quote((filename or "video").strip() or "video", safe=".-_")
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


# 低于这个空闲量才把 errno 28 当成磁盘真的满了。共享预留过期时同样返回 28，但空间还在。
_STALE_FREE_BYTES = 64 * 1024 * 1024
_STRM_ATTEMPTS = 3
_STRM_RETRY_PAUSE = 1.0


def write_strm(root: str, relative: str, url: str) -> str:
    """把一行播放地址写到 strm 根下。不创建 nfo 或图片。"""
    target = _safe_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(url.strip() + "\n", encoding="utf-8")
    return str(target)


def write_bytes(root: str, relative: str, data: bytes) -> str:
    """把二进制内容写到 strm 根下。字幕和 strm 放在一起。"""
    target = _safe_target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return str(target)


def _safe_target(root: str, relative: str) -> Path:
    """算出要写的文件，不许跑出 strm 根。"""
    base = Path(root).expanduser().resolve()
    parts = [part for part in relative.split("/") if part and part not in (".", "..")]
    if not parts:
        raise OSError("strm 路径为空")
    target = (base / Path(*parts)).resolve()
    if base != target and base not in target.parents:
        raise OSError("strm 路径越界")
    return target


def free_bytes(path: str) -> int | None:
    """路径所在文件系统还剩多少字节。目录还不存在时看最近的已有上级。"""
    probe = Path(path).expanduser()
    try:
        while not probe.exists():
            if probe.parent == probe:
                break
            probe = probe.parent
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def strm_write_failure(exc: OSError, root: str) -> str:
    """写成日志和任务里能直接看的句子。有空闲时的 errno 28 不是磁盘已满。"""
    if not _is_enospc(exc):
        return f"strm 写入失败：{exc}"
    free = free_bytes(root)
    if free is None:
        return f"strm 写入失败：errno 28，无法确认剩余空间：{exc}"
    if free >= _STALE_FREE_BYTES:
        return f"strm 写入失败：errno 28，{root} 仍有 {free} 字节空闲，这是共享预留过期"
    return f"strm 写入失败：磁盘已满，剩余 {free} 字节"


def commit_strm(root: str, relative: str, url: str, cancel=None, attempts: int | None = None, pause: float | None = None) -> str:
    """写入 strm。共享预留过期时先重试几次；失败会记日志，并由调用方留到下一轮。"""
    total = _STRM_ATTEMPTS if attempts is None else attempts
    gap = _STRM_RETRY_PAUSE if pause is None else pause
    last = "strm 写入失败"
    for attempt in range(total):
        try:
            return write_strm(root, relative, url)
        except OSError as exc:
            last = strm_write_failure(exc, root)
            _strm_log(last)
            if not _stale_reservation(exc, root) or attempt + 1 == total or _event_set(cancel):
                raise OSError(last) from exc
            if gap and gap > 0:
                if cancel is not None and hasattr(cancel, "wait"):
                    cancel.wait(gap)
                else:
                    time.sleep(gap)
            if _event_set(cancel):
                raise OSError(last) from exc
    raise OSError(last)


def _stale_reservation(exc: OSError, root: str) -> bool:
    if not _is_enospc(exc):
        return False
    free = free_bytes(root)
    return free is not None and free >= _STALE_FREE_BYTES


def _is_enospc(exc: OSError) -> bool:
    number = getattr(exc, "errno", None)
    return number in {28, errno.ENOSPC}


def _event_set(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _strm_log(message: str) -> None:
    try:
        from app.log import logger
        logger.warning(message)
    except Exception:
        import logging
        logging.getLogger("anime115").warning(message)


def _stem(name: str) -> str:
    text = name or ""
    if "." in text:
        return text.rsplit(".", 1)[0]
    return text


def _safe_part(name: str) -> str:
    cleaned = _INVALID.sub(" ", name or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "unknown"
