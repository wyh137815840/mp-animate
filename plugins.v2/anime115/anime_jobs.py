"""每日补磁力、整理和摊平的编排。会话由调用方传入。"""

from __future__ import annotations

import functools
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .anime_call import (
    DAILY_ROUND_BUDGET,
    OFFLINE_TIMEOUT,
    ROUND_BUDGET,
    STORAGE_TIMEOUT,
    Stopped,
    budget_exceeded,
    stopped,
    system_stopped,
    wait_call,
)
from .anime_dmhy import Pick, SearchOptions, select_shows
from .anime_flatten import Node, apply_proposals, propose
from .anime_names import Show, _fold_name, _has_episode_range, join_path, name_of, norm, parent_path, parse_episode, parse_episode_range, safe_dir_name, strip_mount_prefix, title_matches
from .anime_playurl import PlayUrlError, play_url_for_strm
from .anime_strm import commit_strm, pickcode_of, strm_relative
from .anime_subs import (
    SIDECAR_SUFFIXES,
    ass_title_matches,
    fetch_bytes,
    is_subtitle_dir,
    is_subtitle_name,
    match_subtitles,
    subtitle_relative,
    unpack,
    write_sidecar,
)


SUB_EXTS = {".ass", ".srt", ".ssa", ".sup", ".sub", ".idx", ".vtt"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".m2ts"}
_SHOW_YEAR = re.compile(r"^(.+?)\s*[(（](\d{4})[)）](?:\s*\{tmdb-\d+\})?$")
# 开放平台和 p115client 都可能一直不返回。到点就结束这一轮，避免整理把后面的周期全部挡住。
# 默认时限来自 anime_call：离线列表 45 秒，列目录/移动 60 秒。
# 这里保留一份可覆盖的副本，单测可以把 _OFFLINE_CALL_TIMEOUT 调小来验证超时路径。
_OFFLINE_CALL_TIMEOUT = OFFLINE_TIMEOUT
_STORAGE_CALL_TIMEOUT = STORAGE_TIMEOUT
# 扫描单条任务的预算；大包搬运另按文件数分配有限预算（见 _move_budget）。
# 115 慢的时候一次 move 要 8-10 秒，12 集的大包光搬运就上百秒，
# 预算太紧会让大包每次都卡在搬运中途，一集也搬不进来。
_TASK_BUDGET = 360
# 找 file_id 时最多探几个目录。收件箱里目录多的时候不能一个个试下去。
_NESTED_PROBE_LIMIT = 20
# 探目录的总时限。115 慢的时候一次列目录要 8-10 秒，探满 20 个会吃掉整条任务的预算；
# 115 快时次数上限仍然生效。
_NESTED_PROBE_SECONDS = 60


class JobStopped(Stopped):
    """停止或重载把这一轮取消了。"""


def _event_set(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _cancelled(session) -> bool:
    """任务被停止，或者宿主正在关停。"""
    return stopped(getattr(session, "cancel", None))


def _system_stopped() -> bool:
    """宿主正在关停。只在长循环里抽查，避免频繁读全局状态。"""
    return system_stopped()


def _call(session, func, timeout: float = None):
    """在限时内等 115 返回。停止或重载时不再占着这一轮。"""
    limit = _OFFLINE_CALL_TIMEOUT if timeout is None else timeout
    try:
        return wait_call(getattr(session, "cancel", None), func, limit)
    except Stopped as exc:
        raise JobStopped(str(exc)) from exc


def _call_safe(session, func, default=None, label: str = "115 调用", timeout: float = None):
    """限时调用一次 115。超时或出错就返回默认值，不打断整轮。"""
    try:
        return _call(session, func, timeout)
    except JobStopped:
        raise
    except TimeoutError as exc:
        _job_log(f"动漫 115 {label}超时：{exc}", "warning")
        return default
    except Exception as exc:
        _job_log(f"动漫 115 {label}失败：{exc}", "warning")
        return default


def _task_limit(deadline: float = None) -> float:
    """这一条任务还剩多少时间可用。"""
    if deadline is None:
        return _STORAGE_CALL_TIMEOUT
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("这一条整理超过时间预算")
    return min(_STORAGE_CALL_TIMEOUT, remaining)


def _move_budget(config: JobConfig, n_nodes: int) -> float:
    """按要搬的文件数给搬运时间。每个文件预留搬运间隔加一次调用的余量。

    大包（整季 + 外挂字幕）文件多，固定预算会搬一半就超时；这里按文件数放宽，
    封顶 900 秒，并由调用者约束在整轮截止时间内。
    115 响应慢时一次调用要 8-10 秒，所以每个文件的余量给得比间隔宽。
    """
    gap = max(0, int(getattr(config, "move_gap_seconds", 2) or 0))
    needed = 60 + max(0, n_nodes) * (gap + 12)
    return float(min(900, needed))


def _list_dir(session, path: str, deadline: float = None) -> list:
    """限时列一层目录。失败或超时抛出去，由调用方决定这一条怎么记。

    deadline 是这一条任务的截止时间：单条任务里会连着列很多次目录，
    每次都按 60 秒算，一条任务能拖很久，所以还要留一条任务的总预算。
    """
    limit = _STORAGE_CALL_TIMEOUT
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("这一条整理超过时间预算")
        limit = min(limit, remaining)
    items = _call(session, lambda: session.list_dir(path) or [], limit)
    return list(items)


def _job_log(message: str, level: str = "info") -> None:
    try:
        from app.log import logger
        getattr(logger, level, logger.info)(message)
    except Exception:
        import logging
        getattr(logging.getLogger("anime115"), level, logging.getLogger("anime115").info)(message)


def _daily_stopped(config: JobConfig, inbox: str, chosen, notes, submitted) -> dict:
    return {
        "ok": False,
        "error": "已停止",
        "dry_run": config.dry_run,
        "inbox": inbox,
        "picks": [_pick_row(item) for item in chosen],
        "notes": notes,
        "submitted": submitted,
        "persist": bool(submitted) and not config.dry_run,
    }


def _organize_status(config: JobConfig, moves, *, error: str = "", stopped: bool = False, dirty: bool = False, tasks=None, seen=None) -> dict:
    """整理的结束状态。停掉或超时时仍可把已经搬完的任务留下来。"""
    if stopped and not error:
        error = "已停止"
    seen_changed = False
    if not config.dry_run and tasks is not None and (not error or dirty):
        seen_changed = _sync_seen_status(tasks, seen)
    public = [_public_move(item) for item in moves]
    payload = {
        "ok": not error,
        "dry_run": config.dry_run,
        "moves": public,
        "seen_changed": seen_changed,
        "persist": (not config.dry_run) and bool(error) and dirty,
    }
    if error:
        payload["error"] = error
    summary = _strm_summary(public)
    if summary:
        payload["strm_error"] = summary
    return payload


def _strm_summary(moves) -> str:
    errors = [item.get("strm_error") for item in moves if isinstance(item, dict) and item.get("strm_error")]
    if not errors:
        return ""
    return f"strm 写入失败 {len(errors)} 个：{errors[0]}"


def _retry_pending_strm(tasks, session, config: JobConfig) -> tuple[list, bool]:
    """上一轮已经搬走、但 strm 没写成的，这一轮先补。不再移动 115 上的文件。"""
    if config.dry_run or not (config.strm_root or "").strip() or not isinstance(tasks, list):
        return [], False
    notes = []
    changed = False
    cancel = getattr(session, "cancel", None)
    for task in tasks:
        if not isinstance(task, dict) or task.get("strm_status") != "failed":
            continue
        relative = str(task.get("strm_relative") or "")
        url = str(task.get("strm_url") or "")
        if not relative or not url:
            continue
        if _event_set(cancel):
            break
        try:
            path = commit_strm(config.strm_root, relative, url, cancel=cancel)
        except OSError as exc:
            notes.append({
                "action": "strm",
                "reason": "失败",
                "strm_error": str(exc),
                "show": task.get("show") or "",
                "name": task.get("title") or "",
            })
            continue
        task["strm_status"] = "written"
        task.pop("strm_relative", None)
        task.pop("strm_url", None)
        changed = True
        notes.append({
            "action": "strm",
            "reason": "ok",
            "strm": path,
            "show": task.get("show") or "",
            "name": task.get("title") or "",
        })
    return notes, changed


def _remember_strm(task: dict, decision: dict) -> None:
    if decision.get("strm") and not decision.get("strm_error"):
        task["strm_status"] = "written"
        task.pop("strm_relative", None)
        task.pop("strm_url", None)
        return
    relative = decision.get("strm_relative") or ""
    url = decision.get("strm_url") or ""
    if decision.get("strm_error") and relative and url:
        task["strm_status"] = "failed"
        task["strm_relative"] = relative
        task["strm_url"] = url


def _index_done(found: dict, complete: bool):
    if complete:
        _job_log(f"动漫 115 整理离线列表翻完，{len(found)} 条")
    else:
        _job_log(f"动漫 115 整理离线列表没有翻完，已看到 {len(found)} 条，没见到的任务先留着", "warning")
    return found, complete


@dataclass
class JobConfig:
    """三个任务共用的配置。"""

    dry_run: bool = True
    storage: str = "u115"
    offline_root: str = "/离线下载/动漫"
    mount_prefix: str = "/115open"
    library_root: str = ""
    flatten_roots: list[str] = field(default_factory=list)
    flatten_apply: bool = False
    flatten_max_moves: int = 30
    search: SearchOptions = field(default_factory=SearchOptions)
    offline_gap_seconds: int = 20
    max_submit_per_run: int = 10
    move_gap_seconds: int = 2
    min_episode_mb: int = 80
    strm_root: str = ""
    play_base: str = ""
    play_base_lan: str = ""
    strm_for: str = ""
    public_hosts: str = ""
    skip_owned: bool = True
    subtitle_enabled: bool = True
    # 每季的集数上限：{(剧名, 季): 集数}。用来挡住跨季绝对集数写错集。
    season_caps: dict = field(default_factory=dict)


def _season_cap(caps: dict, show: str, season: int) -> int | None:
    """这一季最多多少集。认不出来就返回空，表示不做限制。"""
    if not caps or not show or season <= 0:
        return None
    key = (show, season)
    if key in caps:
        return int(caps[key])
    for (name, number), total in caps.items():
        if int(number) != season:
            continue
        if name.startswith(show) or show.startswith(name):
            return int(total)
    return None


def _fair_order(picks: list) -> list:
    """把候选按剧轮流排一遍。

    每轮每部剧先出一条，剧多了也不会全被前面那几部占满额度。
    """
    grouped: dict[str, list] = {}
    for pick in picks:
        grouped.setdefault(getattr(pick, "show", "") or "", []).append(pick)
    ordered = []
    while True:
        added = False
        for items in grouped.values():
            if items:
                ordered.append(items.pop(0))
                added = True
        if not added:
            return ordered


# 同一时刻只跑一个作业：整理、每日补集、摊平都会动 115，重叠会互相干扰。
_JOB_LOCK = threading.Lock()


def _single_job(func):
    """已经有作业在跑时，这一轮直接跳过，不排队。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not _JOB_LOCK.acquire(blocking=False):
            _job_log(f"动漫 115 已有作业在跑，{func.__name__} 这一轮跳过", "warning")
            return {"ok": False, "error": "已有作业在跑", "counts": {}, "moved": 0, "submitted": 0}
        try:
            return func(*args, **kwargs)
        finally:
            _JOB_LOCK.release()
    return wrapper


@_single_job
def run_daily(shows, session, entries_of, seen, now: datetime, config: JobConfig, report=None,
              owned_session=None, fallback_session=None) -> dict:
    """搜索并在非预览时提交离线。预览不写 seen。

    owned_session 只用来查「库里已有同集」。开放平台的列表接口容易被 115 风控，
    有 Cookie 时改用 Cookie 查，离线提交仍然走原来的通道；
    fallback_session 是提交用的备用通道，开放平台超时时顶上。
    """
    if not shows:
        _tell(report, job="daily", phase="failed", dry_run=config.dry_run, index=0, total=0, current="", result="失败", failed=1)
        return {"ok": False, "error": "未配置追更", "picks": [], "dry_run": config.dry_run}
    inbox = _dated_inbox(config, now)
    started = time.monotonic()
    picks, notes = select_shows(shows, entries_of, config.search, now)
    owned = _OwnedIndex(owned_session or session, config) if config.skip_owned else None
    chosen = []
    checked = 0
    scan_limit = max(config.max_submit_per_run * 3, 5)
    for pick in _fair_order(picks):
        if len(chosen) >= config.max_submit_per_run or checked >= scan_limit:
            break
        if _episode_blocked(seen, pick, now):
            notes.append({"show": pick.show, "title": pick.torrent.title, "reason": "已经提交过"})
            continue
        if owned is not None:
            checked += 1
            try:
                in_library = owned.has(pick)
            except Exception as exc:
                _job_log(f"动漫 115 查库里已有集数失败，按没有处理：{exc}", "warning")
                in_library = None
            if in_library is True:
                notes.append({"show": pick.show, "title": pick.torrent.title, "reason": "库里已有这一集"})
                continue
        chosen.append(pick)
    if _cancelled(session):
        return _daily_stopped(config, inbox, chosen, notes, [])
    submitted = []
    step, skipped, failed = _walk_notes(report, "daily", config.dry_run, notes, len(chosen))
    if config.dry_run:
        for offset, pick in enumerate(chosen):
            _tell(
                report,
                job="daily",
                phase="running",
                dry_run=True,
                index=step + offset + 1,
                total=step + len(chosen),
                current=pick.torrent.title,
                result="ok",
                queued=len(chosen),
                submitted=0,
                skipped=skipped,
                failed=failed,
            )
        return {
            "ok": True,
            "dry_run": True,
            "inbox": inbox,
            "picks": [_pick_row(item) for item in chosen],
            "notes": notes,
            "submitted": [],
        }
    try:
        logged_in = _call(session, session.check_login)
    except JobStopped:
        return _daily_stopped(config, inbox, chosen, notes, [])
    except TimeoutError as exc:
        return {"ok": False, "error": f"115 登录检查超时：{exc}", "picks": [], "dry_run": False, "inbox": inbox}
    if not logged_in:
        # 开放平台的 access_token 失效时（refresh_token 无效），改用 Cookie 通道。
        if fallback_session is None:
            return {"ok": False, "error": "115 未登录", "picks": [], "dry_run": False}
        _job_log("动漫 115 开放平台未登录，改用 Cookie 通道提交", "warning")
        session = fallback_session
        try:
            logged_in = _call(session, session.check_login)
        except JobStopped:
            return _daily_stopped(config, inbox, chosen, notes, [])
        except TimeoutError:
            logged_in = True
        if not logged_in:
            return {"ok": False, "error": "115 未登录", "picks": [], "dry_run": False}
    try:
        quota = _quota_left(_call(session, session.quota))
    except JobStopped:
        return _daily_stopped(config, inbox, chosen, notes, [])
    except TimeoutError as exc:
        if fallback_session is None:
            return {
                "ok": False,
                "error": f"离线配额查询超时：{exc}（开放平台可能在风控，等风控过去再跑）",
                "picks": [],
                "dry_run": False,
                "inbox": inbox,
            }
        # 开放平台被 115 风控时，改用 Cookie 通道提交；两边的任务是同一批。
        _job_log(f"动漫 115 开放平台超时，改用 Cookie 通道提交：{exc}", "warning")
        session = fallback_session
        quota = None
        try:
            logged_in = _call(session, session.check_login)
        except JobStopped:
            return _daily_stopped(config, inbox, chosen, notes, [])
        except TimeoutError:
            logged_in = True
        if not logged_in:
            return {"ok": False, "error": "115 未登录", "picks": [], "dry_run": False}
    if quota is not None and quota <= 0:
        return {"ok": False, "error": "离线配额用尽", "picks": [_pick_row(item) for item in chosen], "dry_run": False}
    try:
        folder = _call(session, lambda: session.ensure_dir(inbox))
    except JobStopped:
        return _daily_stopped(config, inbox, chosen, notes, [])
    except TimeoutError as exc:
        return {"ok": False, "error": f"建当天目录超时：{exc}", "inbox": inbox, "dry_run": False}
    fileid = getattr(folder, "fileid", "") if folder is not None else ""
    if not fileid:
        return {"ok": False, "error": "建不了当天目录", "inbox": inbox, "dry_run": False}
    for index, pick in enumerate(chosen):
        if _cancelled(session):
            return _daily_stopped(config, inbox, chosen, notes, submitted)
        if quota is not None and quota <= 0:
            break
        if index % 5 == 0 and _system_stopped():
            return _daily_stopped(config, inbox, chosen, notes, submitted)
        if index % 5 == 0 and budget_exceeded(started, DAILY_ROUND_BUDGET):
            _job_log("动漫 115 每日任务超过时间预算，先收尾，下一轮继续", "warning")
            notes.append({"show": "", "title": "", "reason": "本轮超过时间预算，剩下的留到下一轮"})
            break
        if index:
            session.sleep(config.offline_gap_seconds)
        if _cancelled(session):
            return _daily_stopped(config, inbox, chosen, notes, submitted)
        magnet = pick.torrent.magnet
        try:
            result = _call(session, lambda magnet=magnet, folder=fileid: session.add_offline(magnet, folder))
        except JobStopped:
            return _daily_stopped(config, inbox, chosen, notes, submitted)
        except TimeoutError as exc:
            failed += 1
            notes.append({"show": pick.show, "title": pick.torrent.title, "reason": f"离线提交超时：{exc}"})
            _tell_daily(report, step + index + 1, step + len(chosen), pick.torrent.title, "失败", len(chosen) - index - 1, len(submitted), skipped, failed)
            continue
        if not _accepted(result):
            failed += 1
            notes.append({"show": pick.show, "title": pick.torrent.title, "reason": "离线提交失败"})
            _tell_daily(report, step + index + 1, step + len(chosen), pick.torrent.title, "失败", len(chosen) - index - 1, len(submitted), skipped, failed)
            continue
        if _landed_in_cloud_download(result):
            failed += 1
            notes.append({"show": pick.show, "title": pick.torrent.title, "reason": "落到了云下载"})
            _tell_daily(report, step + index + 1, step + len(chosen), pick.torrent.title, "失败", len(chosen) - index - 1, len(submitted), skipped, failed)
            continue
        _remember(seen, pick, now, inbox)
        submitted.append(_pick_row(pick))
        _tell_daily(report, step + index + 1, step + len(chosen), pick.torrent.title, "ok", len(chosen) - len(submitted), len(submitted), skipped, failed)
        if quota is not None:
            quota -= 1
    return {
        "ok": True,
        "dry_run": False,
        "inbox": inbox,
        "picks": [_pick_row(item) for item in chosen],
        "notes": notes,
        "submitted": submitted,
    }


@_single_job
def run_organize(tasks, session, config: JobConfig, report=None, seen=None) -> dict:
    """整理本插件提交过的任务。

    完成可以来自整份离线列表，不限前 5 页。列表已经翻完、任务却不在里面时，
    仍按提交时记下的日期收件箱整理。列表里还在下载或失败的不动。
    已经搬走的，和确认这一包里没有视频的，都不再扫。
    """
    library = strip_mount_prefix(config.library_root, config.mount_prefix) if config.library_root else ""
    if _cancelled(session):
        return _organize_status(config, [], stopped=True)
    if not library or library == "/":
        return {"ok": False, "error": "没有填写库根", "moves": [], "dry_run": config.dry_run}
    pending, dirty = _retry_pending_strm(tasks, session, config)
    if _cancelled(session):
        return _organize_status(config, pending, stopped=True, dirty=dirty, tasks=tasks, seen=seen)
    try:
        logged_in = _call(session, session.check_login)
    except JobStopped:
        return _organize_status(config, pending, stopped=True, dirty=dirty, tasks=tasks, seen=seen)
    except TimeoutError as exc:
        return _organize_status(config, pending, error=f"115 登录检查超时：{exc}", dirty=dirty, tasks=tasks, seen=seen)
    if not logged_in:
        return _organize_status(config, pending, error="115 未登录", dirty=dirty, tasks=tasks, seen=seen)
    _include_seen(tasks, seen)
    try:
        listed, complete = _offline_index(session)
    except JobStopped:
        return _organize_status(config, pending, stopped=True, dirty=dirty, tasks=tasks, seen=seen)
    except TimeoutError as exc:
        _job_log(f"动漫 115 整理离线列表超时：{exc}", "warning")
        return _organize_status(config, pending, error=f"离线列表超时：{exc}", dirty=dirty, tasks=tasks, seen=seen)
    work = []
    for task in tasks:
        if not isinstance(task, dict) or task.get("status") in _ORGANIZE_DONE:
            continue
        infohash = str(task.get("infohash") or "").lower()
        if not infohash:
            continue
        offline = listed.get(infohash)
        if offline is not None:
            if not _task_finished(offline):
                continue
            work.append((task, offline))
            continue
        if complete:
            work.append((task, {}))
    # 上轮耗时/不确定的任务不能永远占着队首，让未尝试的大包也轮得到。
    work.sort(key=lambda pair: float(pair[0].get("organize_attempted_at") or 0))
    moves = []
    organized = skipped = failed = 0
    started = time.monotonic()
    _tell(report, job="organize", phase="running", dry_run=config.dry_run, index=0, total=len(work), organized=0, skipped=0, failed=0, current="", result="")
    for index, pair in enumerate(work, start=1):
        if _cancelled(session):
            return _organize_status(config, [*pending, *moves], stopped=True, dirty=True, tasks=tasks, seen=seen)
        if index % 5 == 0 and _system_stopped():
            return _organize_status(config, [*pending, *moves], stopped=True, dirty=True, tasks=tasks, seen=seen)
        if budget_exceeded(started, ROUND_BUDGET):
            _job_log("动漫 115 整理超过时间预算，剩下的任务留到下一轮", "warning")
            return _organize_status(config, [*pending, *moves], stopped=True, dirty=True, tasks=tasks, seen=seen)
        task, offline = pair
        task["organize_attempted_at"] = time.time()
        task.pop("organize_reason", None)
        try:
            _organize_one(task, session, library, config, moves, offline, round_deadline=started + ROUND_BUDGET)
        except JobStopped:
            return _organize_status(config, [*pending, *moves], stopped=True, dirty=True, tasks=tasks, seen=seen)
        except Exception:
            moves.append({"action": "leave", "reason": "整理失败", "task": task.get("infohash"), "nodes": []})
            _tell(report, job="organize", phase="running", dry_run=config.dry_run, index=index, total=len(work), current=task.get("title") or task.get("show") or "", result="整理失败", organized=organized, skipped=skipped, failed=failed + 1)
            failed += 1
        else:
            last = moves[-1]
            if last.get("action") == "move" and not config.dry_run and task.get("status") == "moved":
                organized += 1
            elif last.get("action") == "move" and config.dry_run:
                organized += 1
            elif last.get("reason") in {"建不了目标目录", "移动失败", "整理失败"}:
                failed += 1
            else:
                skipped += 1
            _tell(
                report,
                job="organize",
                phase="running",
                dry_run=config.dry_run,
                index=index,
                total=len(work),
                current=task.get("title") or task.get("show") or "",
                result="ok" if last.get("action") == "move" else (last.get("error") or last.get("reason") or "跳过"),
                organized=organized,
                skipped=skipped,
                failed=failed,
            )
    return _organize_status(config, [*pending, *moves], tasks=tasks, seen=seen, dirty=True)


def _organize_one(task, session, library, config, moves, offline=None, round_deadline=None) -> None:
    # 一条任务有自己的时间预算，且不能突破整轮截止时间。
    deadline = time.monotonic() + _TASK_BUDGET
    if round_deadline is not None:
        deadline = min(deadline, round_deadline)
    try:
        files, pack, why, confirmed = _scope_to_pack(session, task, offline or {}, deadline)
    except TimeoutError as exc:
        moves.append({
            "action": "leave",
            "reason": "整理超时",
            "error": str(exc),
            "task": task.get("infohash"),
            "nodes": [],
        })
        return
    if why:
        task["organize_reason"] = why
        task["organize_checked_at"] = time.time()
        moves.append({"action": "leave", "reason": why, "task": task.get("infohash"), "nodes": [], "pack": pack})
        return
    decision = _decide_move(task, files, library, pack, config.season_caps)
    decision["pack"] = pack
    if decision.get("action") != "move":
        # 记下这一条为什么没搬成，汇报时能区分「包没落地」和「真的卡住」。
        task["organize_reason"] = str(decision.get("reason") or decision.get("error") or "")
        task["organize_checked_at"] = time.time()
    moves.append(decision)
    _close_empty_leftover(task, decision, confirmed, config)
    if decision["action"] == "move":
        # 大包按文件数追加搬运预算，免得搬一半超预算（杜鹃的婚约 BDRip 这类）。
        n_nodes = len(decision.get("nodes") or [])
        deadline = max(deadline, time.monotonic() + _move_budget(config, n_nodes))
        if round_deadline is not None:
            deadline = min(deadline, round_deadline)
        show_folder, why = _show_folder(session, library, task.get("show") or "", deadline)
        if why:
            decision["action"] = "leave"
            decision["reason"] = why
        else:
            season_name = decision["dest_dir"].rsplit("/", 1)[-1]
            decision["dest_dir"] = join_path(join_path(library, show_folder), season_name)
    if decision["action"] != "move" or config.dry_run:
        if decision["action"] == "move" and config.dry_run:
            _apply_strm(decision, config, write=False)
        return
    dest_dir = decision["dest_dir"]
    try:
        made = _call(session, lambda: session.ensure_dir(dest_dir), _task_limit(deadline))
    except JobStopped:
        raise
    except Exception as exc:
        _log_mkdir(dest_dir, exc)
        made = None
        failure = exc
    else:
        failure = None
    if made is None or not str(getattr(made, "fileid", "") or ""):
        decision["action"] = "leave"
        decision["reason"] = "建不了目标目录"
        decision["error"] = f"{dest_dir}：{failure}" if failure is not None else f"{dest_dir}：没有目录 id"
        return
    try:
        existing = {item.name for item in _list_dir(session, dest_dir, deadline)}
    except JobStopped:
        raise
    except Exception:
        existing = set()
    if decision.get("items"):
        # 整包：一集一集搬，一集一个 strm
        moved_items = 0
        failed = 0
        for item in decision["items"]:
            if any(video.name in existing for video in (item.get("videos") or [item["video"]])):
                item["skipped"] = "collision"
                continue
            ok = True
            for node in item["nodes"]:
                new_name = _move_name(item, node)
                try:
                    moved = _call(
                        session,
                        lambda node=node, new_name=new_name: session.move(node, dest_dir, new_name),
                        _task_limit(deadline),
                    )
                except JobStopped:
                    raise
                except Exception as exc:
                    _job_log(f"动漫 115 移动失败 {node.name}：{exc}", "warning")
                    moved = False
                if not moved:
                    ok = False
                    break
                session.sleep(config.move_gap_seconds)
            if not ok:
                failed += 1
                item["error"] = "移动失败"
                continue
            moved_items += 1
            _apply_strm_item(decision, item, config, write=True, cancel=getattr(session, "cancel", None))
        decision["moved_episodes"] = moved_items
        decision["failed_episodes"] = failed
        if moved_items:
            _remove_empty_dir(session, decision, pack)
        if failed:
            # 有一集没搬成，任务留着，下一轮继续
            decision["action"] = "leave"
            decision["reason"] = "整包有集没搬成"
            return
        task["status"] = "moved"
        return
    if decision["name"] in existing:
        decision["action"] = "leave"
        decision["reason"] = "collision"
        return
    for item in decision["nodes"]:
        try:
            moved = _call(session, lambda item=item: session.move(item, dest_dir, item.name), _task_limit(deadline))
        except JobStopped:
            raise
        except Exception as exc:
            _job_log(f"动漫 115 移动失败 {item.name}：{exc}", "warning")
            moved = False
        if not moved:
            decision["action"] = "leave"
            decision["reason"] = "移动失败"
            return
        session.sleep(config.move_gap_seconds)
    task["status"] = "moved"
    _apply_strm(decision, config, write=True, cancel=getattr(session, "cancel", None))
    _remember_strm(task, decision)


@_single_job
def run_strm(session, config: JobConfig, limit: str = "", report=None) -> dict:
    """给库里已经整理好的视频补 strm。不移动 115 上的文件，也不写 nfo。"""
    library = strip_mount_prefix(config.library_root, config.mount_prefix) if config.library_root else ""
    root = (config.strm_root or "").strip()
    if _cancelled(session):
        return {"ok": False, "error": "已停止", "items": [], "dry_run": config.dry_run}
    if not root:
        return {"ok": False, "error": "没有填写 strm 目录", "items": [], "dry_run": config.dry_run}
    if not library or library == "/":
        return {"ok": False, "error": "没有填写库根", "items": [], "dry_run": config.dry_run}
    try:
        logged_in = _call(session, session.check_login)
    except JobStopped:
        return {"ok": False, "error": "已停止", "items": [], "dry_run": config.dry_run}
    except TimeoutError as exc:
        return {"ok": False, "error": f"115 登录检查超时：{exc}", "items": [], "dry_run": config.dry_run}
    if not logged_in:
        return {"ok": False, "error": "115 未登录", "items": [], "dry_run": config.dry_run}
    chosen = strip_mount_prefix(limit, config.mount_prefix) if (limit or "").strip() else ""
    if chosen and chosen != library and not chosen.startswith(library + "/"):
        return {"ok": False, "error": "路径不在库根下", "items": [], "dry_run": config.dry_run}
    rows = _library_videos(session, library, chosen)
    items = []
    written = skipped = failed = 0
    _tell(report, job="strm", phase="running", dry_run=config.dry_run, index=0, total=len(rows), written=0, skipped=0, failed=0, current="", result="")
    for index, row in enumerate(rows, start=1):
        if _cancelled(session):
            return {
                "ok": False,
                "error": "已停止",
                "dry_run": config.dry_run,
                "written": written,
                "skipped": skipped,
                "failed": failed,
                "items": items,
            }
        item = _strm_one(row, config, cancel=getattr(session, "cancel", None))
        items.append(item)
        if item["action"] == "write" and not config.dry_run and not item.get("error"):
            written += 1
            result = "ok"
        elif item["action"] == "write" and config.dry_run:
            written += 1
            result = "ok"
        elif item.get("error"):
            failed += 1
            result = item["error"]
        else:
            skipped += 1
            result = item.get("reason") or "跳过"
        _tell(
            report,
            job="strm",
            phase="running",
            dry_run=config.dry_run,
            index=index,
            total=len(rows),
            current=row["name"],
            result=result,
            written=written,
            skipped=skipped,
            failed=failed,
        )
    payload = {
        "ok": True,
        "dry_run": config.dry_run,
        "written": written,
        "skipped": skipped,
        "failed": failed,
        "items": items,
    }
    if failed:
        first = next((item.get("error") for item in items if item.get("error")), "")
        payload["strm_error"] = f"strm 写入失败 {failed} 个：{first}"
    return payload


@_single_job
def run_flatten(session, config: JobConfig, report=None) -> dict:
    """先分类。安全移动为 0，或预览、或没开摊平落盘时，不移动。"""
    roots = [strip_mount_prefix(item, config.mount_prefix) for item in config.flatten_roots if str(item).strip()]
    roots = [norm(item) for item in roots]
    if _cancelled(session):
        return {"ok": False, "error": "已停止", "counts": {}, "dry_run": config.dry_run, "moved": 0}
    if not roots or any(item == "/" for item in roots):
        return {"ok": False, "error": "摊平目录为空或是根目录", "counts": {}, "dry_run": config.dry_run}
    try:
        logged_in = _call(session, session.check_login)
    except JobStopped:
        return {"ok": False, "error": "已停止", "counts": {}, "dry_run": config.dry_run, "moved": 0}
    except TimeoutError as exc:
        return {"ok": False, "error": f"115 登录检查超时：{exc}", "counts": {}, "dry_run": config.dry_run, "moved": 0}
    if not logged_in:
        return {"ok": False, "error": "115 未登录", "counts": {}, "dry_run": config.dry_run}
    nodes = []
    started = time.monotonic()
    try:
        for root in roots:
            if _cancelled(session):
                raise JobStopped("已停止")
            if budget_exceeded(started, ROUND_BUDGET):
                _job_log("动漫 115 摊平超过时间预算，先收尾", "warning")
                break
            nodes.extend(_flatten_list(session, root, depth=6))
    except JobStopped:
        return {"ok": False, "error": "已停止", "counts": {}, "dry_run": config.dry_run, "moved": 0}
    if _cancelled(session):
        return {"ok": False, "error": "已停止", "counts": {}, "dry_run": config.dry_run, "moved": 0}
    proposals = propose(nodes, roots, config.min_episode_mb * 1024 * 1024)
    apply = (not config.dry_run) and config.flatten_apply
    try:
        moved = apply_proposals(proposals, session, apply, config.flatten_max_moves)
    except Stopped as exc:
        raise JobStopped(str(exc)) from exc
    if moved:
        _delete_empty(session, proposals, set(roots))
    counts = {"ok_eligible_non_collision": 0, "collision": 0, "uncertain": 0, "skip": 0}
    skipped = 0
    for index, item in enumerate(proposals, start=1):
        counts[item.kind] = counts.get(item.kind, 0) + 1
        if item.kind != "ok_eligible_non_collision":
            skipped += 1
        _tell(
            report,
            job="flatten",
            phase="running",
            dry_run=not apply,
            index=index,
            total=len(proposals),
            current=name_of(item.source),
            result="ok" if item.kind == "ok_eligible_non_collision" else "跳过",
            moved=moved if index == len(proposals) else 0,
            skipped=skipped,
            failed=0,
        )
    return {
        "ok": True,
        "dry_run": not apply,
        "counts": counts,
        "moved": moved,
        "proposals": [
            {"kind": item.kind, "source": item.source, "dest": item.dest, "reason": item.reason}
            for item in proposals[:200]
        ],
    }


def _tell(report, **fields) -> None:
    if report:
        report(fields)


def _log_mkdir(path: str, exc: Exception) -> None:
    """建目录失败要把路径和 115 返回留下来。界面上仍记「建不了目标目录」。"""
    text = f"动漫 115 建不了目标目录 {path}：{exc}"
    try:
        from app.log import logger
        logger.warning(text)
    except Exception:
        import logging
        logging.getLogger("anime115").warning(text)


def _tell_daily(report, index, total, title, result, queued, submitted, skipped, failed) -> None:
    _tell(
        report,
        job="daily",
        phase="running",
        dry_run=False,
        index=index,
        total=total,
        current=title,
        result=result,
        queued=queued,
        submitted=submitted,
        skipped=skipped,
        failed=failed,
    )


def _walk_notes(report, job, dry_run, notes, queued) -> tuple[int, int, int]:
    _tell(report, job=job, phase="running", dry_run=dry_run, index=0, total=len(notes) + queued, current="", result="", queued=queued, submitted=0, organized=0, moved=0, skipped=0, failed=0)
    skipped = 0
    for index, note in enumerate(notes, start=1):
        skipped += 1
        _tell(
            report,
            job=job,
            phase="running",
            dry_run=dry_run,
            index=index,
            total=len(notes) + queued,
            current=note.get("title") or note.get("show") or "",
            result=note.get("reason") or "跳过",
            queued=queued,
            submitted=0,
            skipped=skipped,
            failed=0,
        )
    return len(notes), skipped, 0


def _dated_inbox(config: JobConfig, now: datetime) -> str:
    root = strip_mount_prefix(config.offline_root, config.mount_prefix)
    stamp = now.strftime("%Y%m%d")
    if root == "/":
        return "/" + stamp
    return root + "/" + stamp


def _seen_key(pick: Pick) -> str:
    """记住「这一集/这一包」的键。整包按整段记，不按第一集。"""
    span = getattr(pick, "pack", None) or None
    if span and int(span[1]) > int(span[0]):
        return f"{pick.show}|{pick.season}|pack|{int(span[0])}-{int(span[1])}"
    return f"{pick.show}|{pick.season}|{pick.episode}"


def _episode_blocked(seen: dict, pick: Pick, now: datetime) -> bool:
    hashes = seen.setdefault("hashes", {})
    episodes = seen.setdefault("episodes", {})
    if pick.torrent.infohash in hashes:
        return True
    key = _seen_key(pick)
    previous = hashes.get(episodes.get(key, ""), {})
    if not previous:
        return False
    if previous.get("status") == "failed":
        return False
    span = getattr(pick, "pack", None) or None
    if span and int(span[1]) > int(span[0]):
        # 整包：之前那一份是内封字幕、这一份是外挂字幕，就再下一份。
        # 外挂字幕能单独挂到 Emby 旁边按语言切换，内封的做不到。
        if "外挂" in (pick.torrent.title or "") and "外挂" not in str(previous.get("title") or ""):
            return False
    submitted = _parse_time(previous.get("at"))
    if submitted and now - submitted > timedelta(days=3) and previous.get("status") != "moved":
        return False
    return True


def _remember(seen: dict, pick: Pick, now: datetime, folder: str) -> None:
    hashes = seen.setdefault("hashes", {})
    episodes = seen.setdefault("episodes", {})
    key = _seen_key(pick)
    hashes[pick.torrent.infohash] = {
        "at": now.isoformat(),
        "show": pick.show,
        "season": pick.season,
        "episode": pick.episode,
        "status": "submitted",
        "folder": folder,
        "title": pick.torrent.title,
    }
    episodes[key] = pick.torrent.infohash


def _delete_empty(session, proposals, roots: set[str]) -> None:
    parents = {
        parent_path(item.source)
        for item in proposals
        if item.kind == "ok_eligible_non_collision"
    }
    for path in parents:
        if path in roots or path == "/":
            continue
        try:
            if _list_dir(session, path):
                continue
            _call(session, lambda: session.delete_dir(path), _STORAGE_CALL_TIMEOUT)
        except JobStopped:
            raise
        except Exception as exc:
            _job_log(f"动漫 115 清理空目录失败 {path}：{exc}", "warning")


def _pick_row(pick: Pick) -> dict:
    return {
        "show": pick.show,
        "season": pick.season,
        "episode": pick.episode,
        "title": pick.torrent.title,
        "infohash": pick.torrent.infohash,
        "score": pick.score,
    }


def _quota_left(payload) -> int | None:
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    for key in ("surplus", "remain", "left"):
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


# 115 开放平台的成功业务码：0 成功，20004 表示任务已在（重复提交也算接单）。
# 与宿主 U115_DEFAULT_ACCEPTED_CODES 保持一致。
_OFFLINE_ACCEPTED_CODES = (0, 20004)


def _task_accepted(task) -> bool:
    """单条离线任务的结果。state 明确 false、或业务码不是 0/20004 都算没接单。"""
    if not isinstance(task, dict):
        # 结构未知时保守放行，不让一次格式变化把正常提交挡掉。
        return True
    if task.get("state") in (False, 0, "false"):
        return False
    number = _plain_int(task.get("code"))
    if number is not None:
        return number in _OFFLINE_ACCEPTED_CODES
    return True


def _data_accepted(data) -> bool:
    """add_task_urls 的 data 是逐任务结果，可能是列表或单个对象，没有就维持现状。"""
    if isinstance(data, list):
        if not data:
            return True
        return all(_task_accepted(item) for item in data)
    if isinstance(data, dict):
        return _task_accepted(data)
    return True


def _accepted(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("state") in (False, 0, "false"):
        return False
    if payload.get("state") not in (True, 1, "true"):
        code = payload.get("code")
        if not (code in (0, None) and "data" in payload):
            return False
    return _data_accepted(payload.get("data"))


def _landed_in_cloud_download(payload) -> bool:
    text = str(payload)
    return "云下载" in text and "/离线下载/" not in text


# 开放平台 get_task_list 不带 page_count 时，一页满 20 条就还有下一页。
_OFFLINE_PAGE_SIZE = 20
_OFFLINE_PAGE_LIMIT = 200


def _finished_hashes(session) -> set[str]:
    return set(_finished_tasks(session))


def _finished_tasks(session) -> dict[str, dict]:
    """已完成的离线任务，按 infohash 记下名字和 file_id，用来认自己的那一包。"""
    listed, _complete = _offline_index(session)
    return {infohash: task for infohash, task in listed.items() if _task_finished(task)}


def _offline_index(session) -> tuple[dict[str, dict], bool]:
    """整份离线列表，不在第 5 页停下。

    第二个值表示列表有没有看完。看完了，不在列表里的提交才按收件箱整理。
    没看完时，没见到的任务先留着，避免把还在下载的文件搬走。
    """
    found: dict[str, dict] = {}
    _job_log("动漫 115 整理开始翻离线列表")
    for page in range(1, _OFFLINE_PAGE_LIMIT + 1):
        if _cancelled(session):
            raise JobStopped("已停止")
        if page % 5 == 0 and _system_stopped():
            raise JobStopped("系统正在停止")
        _job_log(f"动漫 115 整理离线列表第 {page} 页")
        try:
            payload = _call(session, lambda page=page: session.list_offline(page))
        except JobStopped:
            raise
        except TimeoutError:
            raise
        except Exception:
            if page == 1:
                raise
            return _index_done(found, False)
        if payload is None or not isinstance(payload, (dict, list)):
            # 接口失败时宿主返回 None，这里不能当成「列表已翻完」：
            # 没见到的任务要按没看完处理，继续 pending，免得把还在下载的搬走。
            return _index_done(found, False)
        if isinstance(payload, list):
            rows = [item for item in payload if isinstance(item, dict)]
        else:
            rows = [item for item in _task_rows(payload) if isinstance(item, dict)]
        if not rows:
            return _index_done(found, True)
        added = 0
        for task in rows:
            infohash = str(task.get("info_hash") or task.get("infohash") or "").lower()
            if not infohash or infohash in found:
                continue
            found[infohash] = task
            added += 1
        page_count = _meta_int(payload, ("page_count", "pageCount", "total_page", "pages"))
        total = _meta_int(payload, ("count", "total", "total_count", "task_count"))
        if page_count is not None and page >= page_count:
            return _index_done(found, True)
        # count 若只等于本页条数，它不是总任务数，不能在这里把列表当成已经看完。
        if total and len(found) >= total and total > len(rows):
            return _index_done(found, True)
        if page_count is None and total is None and len(rows) < _OFFLINE_PAGE_SIZE:
            return _index_done(found, True)
        if added == 0:
            # 满页又没有总页数，说明翻页没有前进，后面的任务可能还没看到。
            if page_count is None and len(rows) >= _OFFLINE_PAGE_SIZE:
                return _index_done(found, False)
            if page_count is not None and page < page_count:
                return _index_done(found, False)
            return _index_done(found, True)
    return _index_done(found, False)


def _meta_int(payload, keys: tuple[str, ...]) -> int | None:
    if not isinstance(payload, dict):
        return None
    sources = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        sources.append(data)
    for source in sources:
        for key in keys:
            number = _plain_int(source.get(key))
            if number is not None:
                return number
    return None


def _plain_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


# moved 是已经入库。no_video 是确认这一包里没有视频。两条都不再扫。
_ORGANIZE_DONE = {"moved", "no_video"}


def _include_seen(tasks, seen) -> None:
    """tasks 里没有、但 seen 里还记着的提交，也拿去对收件箱。"""
    if not isinstance(tasks, list) or not isinstance(seen, dict):
        return
    known = {
        str(item.get("infohash") or "").lower()
        for item in tasks
        if isinstance(item, dict)
    }
    hashes = seen.get("hashes")
    if not isinstance(hashes, dict):
        return
    for infohash, row in hashes.items():
        key = str(infohash or "").lower()
        if not key or key in known or not isinstance(row, dict):
            continue
        if row.get("status") in _ORGANIZE_DONE:
            continue
        folder = str(row.get("folder") or "").strip()
        if not folder:
            continue
        tasks.append({
            "infohash": key,
            "show": row.get("show") or "",
            "season": _number(row.get("season")),
            "episode": _number(row.get("episode")),
            "title": row.get("title") or row.get("show") or "",
            "folder": folder,
            "status": row.get("status") or "submitted",
        })
        known.add(key)


def _sync_seen_status(tasks, seen) -> bool:
    """把任务上已经结束的状态写回 seen。no_video 不是 moved，也不放开当天的提交。"""
    if not isinstance(seen, dict):
        return False
    hashes = seen.get("hashes")
    if not isinstance(hashes, dict):
        return False
    changed = False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = task.get("status")
        if status not in _ORGANIZE_DONE:
            continue
        row = hashes.get(str(task.get("infohash") or "").lower())
        if isinstance(row, dict) and row.get("status") != status:
            row["status"] = status
            changed = True
    return changed


def _number(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _task_rows(payload) -> list:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("tasks", "task", "list"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def resolve_show_folder(names: list[str], show: str) -> tuple[str, str]:
    """库根下优先用完全同名的剧目录。没有时，唯一的「剧名 (年份)」也可以。"""
    wanted = safe_dir_name(show or "") or "unknown"
    folders = [name for name in names if name]
    if wanted in folders:
        return wanted, ""
    years = []
    raw = (show or "").strip()
    for name in folders:
        matched = _SHOW_YEAR.match(name.strip())
        if not matched:
            continue
        stem = matched.group(1).strip()
        if safe_dir_name(stem) == wanted or stem == raw:
            years.append(name)
    if len(years) == 1:
        return years[0], ""
    if len(years) > 1:
        return "", "剧名目录不唯一"
    return wanted, ""


def _show_folder(session, library: str, show: str, deadline: float = None) -> tuple[str, str]:
    try:
        children = _list_dir(session, library, deadline)
    except JobStopped:
        raise
    except Exception:
        children = []
    names = [item.name for item in children if getattr(item, "is_dir", False)]
    return resolve_show_folder(names, show)


class _OwnedIndex:
    """查库根里是不是已经有这一集。只读；出错或认不准时放行（返回 None），不挡提交。"""

    def __init__(self, session, config):
        self.session = session
        self.config = config
        self.library = (
            strip_mount_prefix(config.library_root, config.mount_prefix) if config.library_root else ""
        )
        self._folders: list[str] | None = None
        self._episodes: dict[tuple[str, int], set[int] | None] = {}

    def has(self, pick) -> bool | None:
        """True=库里已有；False=没有；None=查不了（没有库根、目录不全、接口失败）。"""
        try:
            return self._has(pick)
        except Exception:
            return None

    def _has(self, pick) -> bool | None:
        if not self.library or self.library == "/":
            return None
        if self._folders is None:
            items = _list_dir(self.session, self.library)
            self._folders = [item.name for item in items if getattr(item, "is_dir", False)]
        if not self._folders:
            return None
        folders = self._candidate_folders(getattr(pick, "show", "") or "")
        if not folders:
            return False
        season = int(getattr(pick, "season", 0) or 0)
        episode = int(getattr(pick, "episode", 0) or 0)
        # 查库里已有之前先按订阅上限换算成真正的季集号：发布用绝对编号时
        # （第二季接着第一季往下写），不换算会重复提交库里已有的集。
        caps = getattr(self.config, "season_caps", None) or {}
        if season > 0 and episode > 0 and caps:
            season, episode = _resolve_episode(
                getattr(pick, "show", "") or "", season, season, episode, caps)
        span = getattr(pick, "pack", None) or None
        if span and int(span[1]) > int(span[0]):
            # 整包：包里还缺集就还值得下，只看第一集会把整包误判成已有
            return self._pack_missing(pick, folders, season, int(span[0]), int(span[1]))
        if season <= 0 or episode <= 0:
            return None
        unknown = False
        for folder in folders:
            key = (folder, season)
            if key not in self._episodes:
                self._episodes[key] = self._load_episodes(folder, season)
            episodes = self._episodes[key]
            if episodes is None:
                unknown = True
                continue
            if episode in episodes:
                return True
        return None if unknown else False

    def _pack_missing(self, pick, folders: list[str], season: int, first: int, last: int) -> bool | None:
        """整包里还缺集吗？缺就还值得下（返回 False=没有，即不已有）。

        库里已经有这一段里的每一集才算已有；缺一集就还要下。
        """
        if season <= 0 or first <= 0 or last < first:
            return None
        missing = False
        unknown = False
        for folder in folders:
            key = (folder, season)
            if key not in self._episodes:
                self._episodes[key] = self._load_episodes(folder, season)
            episodes = self._episodes[key]
            if episodes is None:
                unknown = True
                continue
            for number in range(first, last + 1):
                if number not in episodes:
                    missing = True
                    break
            if missing:
                break
        if unknown:
            return None
        return not missing

    def _candidate_folders(self, show: str) -> list[str]:
        """同名目录和「剧名 (年份)」目录都算。库里有重复目录时取并集，避免只查空目录就放行。"""
        wanted = safe_dir_name(show or "") or "unknown"
        raw = (show or "").strip()
        found = []
        for name in self._folders or []:
            if name == wanted:
                found.append(name)
                continue
            matched = _SHOW_YEAR.match(name.strip())
            if not matched:
                continue
            stem = matched.group(1).strip()
            if safe_dir_name(stem) == wanted or stem == raw:
                found.append(name)
                continue
            # 「神之塔 -Tower of God (2020)」这种「剧名 + 副标题 (年份)」也算同一部。
            folded_stem = _fold_name(stem)
            folded_wanted = _fold_name(wanted or raw)
            if folded_wanted and folded_stem.startswith(folded_wanted):
                found.append(name)
        return found

    def _load_episodes(self, folder: str, season: int) -> set[int] | None:
        show_dir = join_path(self.library, folder)
        try:
            children = _list_dir(self.session, show_dir)
        except JobStopped:
            raise
        except Exception:
            return None
        season_dirs = [
            item
            for item in children
            if getattr(item, "is_dir", False) and _season_dir_number(item.name) == season
        ]
        if not season_dirs:
            return set()
        episodes: set[int] = set()
        for season_dir in season_dirs:
            try:
                files = _list_dir(self.session, season_dir.path)
            except JobStopped:
                raise
            except Exception:
                continue
            for item in files:
                parsed = parse_episode(item.name)
                if parsed is not None and parsed[0] == season:
                    episodes.add(parsed[1])
        return episodes


def _is_video_file(item: Node) -> bool:
    """字幕和同名附属文件不是视频。目录即使名叫 .mkv 也不是。"""
    if item.is_dir or _is_subtitle(item.name):
        return False
    return _suffix(item.name) in VIDEO_EXTS and "sample" not in item.name.lower()


def _is_subtitle(name: str) -> bool:
    return _suffix(name) in SUB_EXTS


def _episode_of_name(name: str, default_season: int = 0) -> tuple[int, int] | None:
    """目录名常以 .mkv 结尾。去掉这一层再认集数。

    季号以文件自己写的为准（S2、S02E04 之类）；文件没写季号时按订阅给的季算，
    否则「第32话」这种文件会被当成第一季，和任务对不上。
    文件写的季和订阅的季不一致时，按文件自己的季报出去，由上层判定不合而不是改名。
    """
    text = name or ""
    if _suffix(text) in VIDEO_EXTS or _suffix(text) in SUB_EXTS:
        text = text.rsplit(".", 1)[0]
    parsed = parse_episode(text)
    if parsed is None:
        return None
    explicit = _content_season(text)
    if explicit is not None:
        return (explicit, parsed[1])
    if default_season > 0:
        return (default_season, parsed[1])
    return parsed


def _scope_to_pack(session, task: dict, offline: dict, deadline: float = None) -> tuple[list[Node], str, str, bool]:
    """只看这一条磁力自己的目录，不扫同一天里的其它包。

    最后一个值表示这一包是不是已经看到了。空列表也可能是路径不存在。
    """
    inbox = norm(task.get("folder") or "")
    if not inbox:
        return [], "", "没有离线目录", False
    wanted = _wanted_names(task, offline)
    file_id = _offline_file_id(offline)
    episode = int(task.get("episode") or 0)
    children = _list_dir(session, inbox, deadline)
    if _folder_is_pack(inbox, wanted):
        files = _flatten_list(session, inbox, depth=3, deadline=deadline)
        return files, inbox, "", _pack_seen(session, inbox, children, files, deadline)
    chosen, why = _choose_child(children, wanted, file_id, episode)
    if why:
        return [], "", why, False
    if chosen is None and file_id:
        chosen, why = _choose_by_nested_id(session, children, file_id, deadline, wanted)
    if why:
        return [], "", why, False
    if chosen is not None:
        if chosen.is_dir:
            return _flatten_list(session, chosen.path, depth=3, deadline=deadline), chosen.path, "", True
        files = _file_and_sidecars(children, chosen)
        known = {item.path for item in files}
        for node in _subtitle_in_dirs(session, children, deadline):
            if node.path not in known:
                files.append(node)
                known.add(node.path)
        return files, chosen.path, "", True
    dirs = [item for item in children if item.is_dir]
    if not dirs:
        videos = _real_videos(children)
        if len(videos) > 1 and wanted:
            matched = _match_one(videos, wanted, episode)
            if len(matched) == 1:
                return _file_and_sidecars(children, matched[0]), matched[0].path, "", True
        files = _flatten_list(session, inbox, depth=3, deadline=deadline)
        return files, inbox, "", _pack_seen(session, inbox, children, files, deadline)
    label = wanted[0] if wanted else (task.get("show") or task.get("infohash") or "")
    listed = "、".join(item.name for item in dirs[:8])
    return [], "", f"找不到这一包：{label}；现有 {listed}", False


def _close_empty_leftover(task: dict, decision: dict, confirmed: bool, config: JobConfig) -> None:
    """这一包已经列出来、里面确实没有视频，才结束任务。

    找不到包、目录不存在、集数对不上都留着。no_video 不是整理成功，也不是建目录失败。
    """
    if config.dry_run or not confirmed:
        return
    if decision.get("action") != "leave" or decision.get("reason") != "没有视频":
        return
    task["status"] = "no_video"
    task["reason"] = "没有视频"


def _pack_seen(session, folder: str, children: list, files: list, deadline: float = None) -> bool:
    """看到里面的文件，或父目录里有这一层，才算这一包存在。"""
    if children or files:
        return True
    return _dir_listed(session, folder, deadline)


def _dir_listed(session, folder: str, deadline: float = None) -> bool:
    folder = norm(folder)
    if not folder or folder == "/":
        return False
    parent = parent_path(folder)
    leaf = name_of(folder)
    try:
        children = _list_dir(session, parent, deadline)
    except JobStopped:
        raise
    except Exception:
        return False
    for item in children:
        if getattr(item, "name", "") == leaf or norm(getattr(item, "path", "")) == folder:
            return True
    return False


def _wanted_names(task: dict, offline: dict) -> list[str]:
    found = []
    for key in ("name", "file_name", "n", "fn"):
        value = str((offline or {}).get(key) or "").strip()
        if value and value not in found:
            found.append(value)
    title = str(task.get("title") or "").strip()
    if title and title not in found:
        found.append(title)
    show = str(task.get("show") or "").strip()
    if show and show not in found:
        found.append(show)
    return found


def _offline_file_id(offline: dict) -> str:
    for key in ("file_id", "fid", "fileid"):
        value = str((offline or {}).get(key) or "").strip()
        if value and value != "0":
            return value
    return ""


# 115 上离线包的目录名用的是种子原名：字幕组原名、原剧名、原扩展名。
# 动漫花园展示的标题是重命名后的：字幕组中文名、中文剧名、换过的标签。
# 两边对不上时按「剧名核心词 + 集数」认包：只认同一部剧同一集，认不准就留在原地。
_PACK_NOISE_WORDS = {
    "1080p", "1080i", "2160p", "720p", "480p", "4k", "uhd", "fhd",
    "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "flac",
    "dts", "eac3", "opus", "mp4", "mkv", "avi", "wmv", "mov", "flv",
    "web", "webrip", "webdl", "bdrip", "bluray", "bd", "hdtv", "dvd",
    "dvdrip", "remux", "repack", "10bit", "8bit", "ma10p", "hdr", "sdr",
    "ass", "srt", "ssa", "sub", "subs", "subtitle", "subtitles",
    "softsub", "hardsub", "chs", "cht", "jp", "jpn", "chi", "gb", "big5",
    "eng", "end", "fin", "v2", "v3", "rec", "uncencored", "cr", "baha",
}
_PACK_NOISE_RE = re.compile(
    r"^(?:srt|ass|ssa|sub|webrip|webdl|bdrip|bluray|x26[45]|h26[45]|hevc"
    r"|avc|aac|ac3|flac|ma10p|dts|opus|ddp?|10bit|8bit|1080p|720p)[a-z0-9]*$"
)
# 中文标签：简体双语、招募翻译、内嵌字幕这些。两边写法不同，不能当剧名。
_PACK_CJK_NOISE = (
    "简体", "繁体", "简繁", "内嵌", "内封", "招募", "国语", "粤语",
    "日语", "中字", "特效", "时轴", "急招", "修复", "重制",
)
_PACK_LATIN_RE = re.compile(r"[A-Za-z0-9]+")
_PACK_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]+")
_PACK_SE_RE = re.compile(r"[Ss]\d{1,2}\s*[Ee]\s*(\d{1,3})")
_PACK_CN_EP_RE = re.compile(r"第\s*(\d{1,3})\s*[话話集]")
_PACK_SEASON_CN_RE = re.compile(r"第\s*(\d{1,2})\s*[季期]")
_PACK_SEASON_EN_RE = re.compile(r"season\s*(\d{1,2})", re.IGNORECASE)
_PACK_SEASON_ORD_RE = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)\s*season", re.IGNORECASE)
_PACK_SEASON_NUM_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})(?![0-9])", re.IGNORECASE)
_PACK_SEASON_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "vi": 6, "vii": 7, "viii": 8}
_PACK_RANGE_BRACKET_RE = re.compile(r"\[(\d{1,3})\s*[-–~]\s*(\d{1,3})[^\]]*\]")
_PACK_RANGE_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,3})\s*[-–~]\s*(\d{1,3})(?![0-9A-Za-z])")
_PACK_BRACKET_RE = re.compile(r"\[(\d{1,3})(?:\s*v\d+)?\]")
_PACK_DASH_RE = re.compile(r"(?<![A-Za-z0-9])[-–]\s*(\d{1,3})(?:\s*v\d+)?(?![0-9A-Za-z])")


def _pack_strip_ext(name: str) -> str:
    """去掉视频或字幕的扩展名。"""
    text = (name or "").strip()
    suffix = _suffix(text)
    if suffix in VIDEO_EXTS or suffix in SUB_EXTS:
        return text[: -len(suffix)]
    return text


def _pack_words(name: str) -> tuple[set[str], set[str]]:
    """名字切成词。拉丁字母数字算一个词，连续的汉字假名算一个词，汉字折成简体。"""
    text = _pack_strip_ext(name)
    latin = {chunk.lower() for chunk in _PACK_LATIN_RE.findall(text)}
    cjk = set()
    for chunk in _PACK_CJK_RE.findall(text):
        folded = _fold_name(chunk)
        if folded:
            cjk.add(folded)
    return latin, cjk


def _pack_is_noise(token: str) -> bool:
    """技术标签：分辨率、编码、来源、字幕语言。两边写法常常不同，不算剧名。"""
    if not token:
        return True
    if token in _PACK_NOISE_WORDS:
        return True
    return bool(_PACK_NOISE_RE.match(token))


def _pack_is_cjk_noise(chunk: str) -> bool:
    return any(chunk.startswith(word) for word in _PACK_CJK_NOISE)


def _pack_episodes(name: str) -> set[int]:
    """名字里能看出来的集数。整包范围会展开成一片。"""
    text = _pack_strip_ext(name)
    found: set[int] = set()
    for matched in _PACK_SE_RE.finditer(text):
        found.add(int(matched.group(1)))
    for matched in _PACK_CN_EP_RE.finditer(text):
        found.add(int(matched.group(1)))
    for matched in _PACK_RANGE_BRACKET_RE.finditer(text):
        first, second = int(matched.group(1)), int(matched.group(2))
        if 0 < first <= second:
            found.update(range(first, second + 1))
    for matched in _PACK_RANGE_RE.finditer(text):
        first, second = int(matched.group(1)), int(matched.group(2))
        if 0 < first < second:
            found.update(range(first, second + 1))
    for matched in _PACK_BRACKET_RE.finditer(text):
        found.add(int(matched.group(1)))
    for matched in _PACK_DASH_RE.finditer(text):
        found.add(int(matched.group(1)))
    return {value for value in found if 0 < value < 1000}


def _pack_core(name: str) -> tuple[set[str], set[str]]:
    """去掉扩展名、技术标签和集数，只剩剧名和字幕组这些内容词。"""
    latin, cjk = _pack_words(name)
    episodes = _pack_episodes(name)
    latin = {
        token for token in latin
        if not _pack_is_noise(token) and not (token.isdigit() and int(token) in episodes)
    }
    cjk = {chunk for chunk in cjk if not _pack_is_cjk_noise(chunk)}
    return latin, cjk


def _pack_lead_words(name: str) -> set[str]:
    """开头方括号里的字幕组。115 上是原名，花园上是中文名，不算剧名差异。"""
    text = (name or "").strip()
    if not text.startswith("["):
        return set()
    end = text.find("]")
    if end < 0:
        return set()
    latin, cjk = _pack_words(text[1:end])
    return latin | cjk


def _pack_episode_relation(wanted: set[int], child: set[int], episode: int = 0) -> str:
    """集数对不对得上。exact 是同一集，batch 是包含这一集的整包，空串是不对。"""
    if wanted and child:
        if wanted == child:
            return "exact"
        if wanted < child:
            return "batch"
        return ""
    if child and not wanted:
        if episode and episode in child:
            return "exact"
        return ""
    if wanted and not child:
        # 这一包看不出集数。只有整包标题才敢认，认不准就留在原地。
        return "batch" if len(wanted) > 1 else ""
    return "exact"


def _pack_seasons(name: str) -> set[int]:
    """名字里能看出来的季。第一季不算，认不出就当没有。"""
    return {value for value in _declared_seasons(name) if value > 1}


def _declared_seasons(name: str) -> set[int]:
    """名字里写明的所有季，包括第一季。用来判断一个包是不是混了多季。"""
    text = _pack_strip_ext(name)
    found: set[int] = set()
    for matched in _PACK_SEASON_CN_RE.finditer(text):
        found.add(int(matched.group(1)))
    for matched in _PACK_SEASON_EN_RE.finditer(text):
        found.add(int(matched.group(1)))
    for matched in _PACK_SEASON_ORD_RE.finditer(text):
        found.add(int(matched.group(1)))
    for matched in _PACK_SEASON_NUM_RE.finditer(text):
        found.add(int(matched.group(1)))
    latin, _cjk = _pack_words(text)
    for token in latin:
        if token in _PACK_SEASON_ROMAN:
            found.add(_PACK_SEASON_ROMAN[token])
    return found


def _pack_match(wanted: str, child: str, episode: int = 0) -> str:
    """这一包是不是 wanted 说的那一部剧的那一集。

    返回 exact（就是这一集）、batch（是包含这一集的整包），空串表示不像。
    只认同一部剧同一季同一集：认不准就不认，宁可留在原地也不搬错。
    """
    wanted_eps = _pack_episodes(wanted)
    child_eps = _pack_episodes(child)
    relation = _pack_episode_relation(wanted_eps, child_eps, episode)
    if not relation:
        return ""
    wanted_seasons = _pack_seasons(wanted)
    child_seasons = _pack_seasons(child)
    if wanted_seasons and child_seasons:
        if wanted_seasons.isdisjoint(child_seasons):
            return ""
    elif wanted_seasons or child_seasons:
        # 一边写着季一边没写，说不准是哪一季，不认。
        return ""
    w_latin, w_cjk = _pack_core(wanted)
    c_latin, c_cjk = _pack_core(child)
    if not (w_latin or w_cjk) or not (c_latin or c_cjk):
        return ""
    # 两边都看不出集数时不敢放宽字幕组，只能按原名认。
    strict = not wanted_eps and not child_eps
    lead = set() if strict else _pack_lead_words(child)
    if c_latin - lead:
        # 字幕组在开头方括号里，剧名在括号外，字幕组写法不同不算差异。
        residual = (c_latin - w_latin) - lead
    else:
        # 整包名字都在方括号里，那就当剧名看，不能整个放过。
        residual = c_latin - w_latin
    if residual:
        return ""
    if c_cjk - w_cjk:
        return ""
    # 证据要落在剧名上：两边字幕组都算的话不算数，单独一个 no、the 也不算数。
    evidence = (c_latin & w_latin) - _pack_lead_words(wanted)
    if not _pack_evidence_ok(evidence) and not (c_cjk & w_cjk):
        return ""
    return relation


def _pack_evidence_ok(evidence: set[str]) -> bool:
    """共享的剧名词够不够硬。单个 no、the 这种常用词不算数。"""
    if len(evidence) >= 2:
        return True
    return any(len(token) >= 5 for token in evidence)


def _pack_candidates(nodes: list[Node], wanted: str, episode: int = 0) -> tuple[list[Node], bool]:
    """按剧名核心和集数找这一包。第二个值表示有多个候选，不敢搬。"""
    exact = []
    batch = []
    for item in nodes:
        relation = _pack_match(wanted, item.name, episode)
        if relation == "exact":
            exact.append(item)
        elif relation == "batch":
            batch.append(item)
    exact = _prefer_dir(exact)
    batch = _prefer_dir(batch)
    if len(exact) == 1:
        return exact, False
    if len(exact) > 1:
        return exact, True
    if len(batch) == 1:
        return batch, False
    return batch, len(batch) > 1


def _node_file_id(node: Node) -> str:
    raw = node.raw
    if isinstance(raw, dict):
        return str(raw.get("fid") or raw.get("file_id") or raw.get("cid") or "").strip()
    return str(getattr(raw, "fileid", "") or getattr(raw, "id", "") or "").strip()


def _leaf_key(name: str) -> str:
    text = (name or "").strip()
    if _suffix(text) in VIDEO_EXTS or _suffix(text) in SUB_EXTS:
        text = text.rsplit(".", 1)[0]
    return _fold_name(text)


def _name_hit(child: str, wanted: str, mode: str) -> bool:
    left = _leaf_key(child)
    right = _leaf_key(wanted)
    if not left or not right:
        return False
    if left == right:
        return mode == "exact"
    if mode == "exact":
        return False
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 6 and short in long


def _folder_is_pack(inbox: str, wanted: list[str]) -> bool:
    """记下的路径本身就是这一包时才整目录扫描。日期目录对不上磁力名。"""
    leaf = name_of(inbox)
    if not leaf:
        return False
    return any(_name_hit(leaf, item, "exact") for item in wanted)


def _unique_paths(nodes: list[Node]) -> list[Node]:
    found = []
    seen = set()
    for item in nodes:
        if item.path in seen:
            continue
        seen.add(item.path)
        found.append(item)
    return found


def _prefer_dir(nodes: list[Node]) -> list[Node]:
    dirs = [item for item in nodes if item.is_dir]
    return _unique_paths(dirs or nodes)


def _match_one(nodes: list[Node], wanted: list[str], episode: int = 0) -> list[Node]:
    """按离线任务名、再按提交标题，各自单独对。对上一个就用它。"""
    for name in wanted:
        exact = _unique_paths(item for item in nodes if _name_hit(item.name, name, "exact"))
        if len(exact) == 1:
            return exact
        # 115 上离线包是种子原名，花园标题是重命名后的，按剧名核心加集数认。
        packs, many = _pack_candidates(nodes, name, episode)
        if len(packs) == 1:
            return packs
        partial = _unique_paths(item for item in nodes if _name_hit(item.name, name, "partial"))
        if len(partial) == 1:
            return partial
    return []


def _choose_child(children: list[Node], wanted: list[str], file_id: str, episode: int = 0) -> tuple[Node | None, str]:
    if file_id:
        hits = [item for item in children if _node_file_id(item) == file_id]
        picked = _prefer_dir(hits)
        if len(picked) == 1:
            return picked[0], ""
        if len(picked) > 1:
            return None, _dir_reason(picked)
    ambiguous: list[Node] = []
    for name in wanted:
        exact = _prefer_dir([item for item in children if _name_hit(item.name, name, "exact")])
        if len(exact) == 1:
            return exact[0], ""
        if len(exact) > 1:
            ambiguous = exact
            continue
        # 115 上离线包是种子原名，花园标题是重命名后的，按剧名核心加集数认。
        packs, many = _pack_candidates(children, name, episode)
        if len(packs) == 1:
            return packs[0], ""
        if many:
            ambiguous = packs
            continue
        partial = _prefer_dir([item for item in children if _name_hit(item.name, name, "partial")])
        if len(partial) == 1:
            return partial[0], ""
        if len(partial) > 1:
            ambiguous = partial
    if ambiguous:
        return None, _dir_reason(ambiguous)
    return None, ""


def _choose_by_nested_id(session, children: list[Node], file_id: str, deadline: float = None,
                         wanted: list[str] = None) -> tuple[Node | None, str]:
    """file_id 有时是外壳里面的正片，而不是外壳自己。

    收件箱里目录多的时候不能一个个试下去，最多探 _NESTED_PROBE_LIMIT 个。
    先探名字对得上的目录，通常一两次就能找到，省下大量列目录请求。
    """
    directories = [item for item in children if item.is_dir]
    likely = []
    rest = []
    for directory in directories:
        if wanted and any(
            _name_hit(directory.name, name, "exact") or _name_hit(directory.name, name, "partial")
            for name in wanted
        ):
            likely.append(directory)
        else:
            rest.append(directory)
    owners = []
    probes = 0
    started = time.monotonic()
    for directory in [*likely, *rest]:
        if probes >= _NESTED_PROBE_LIMIT:
            _job_log(f"动漫 115 找这一包只探了 {_NESTED_PROBE_LIMIT} 个目录，先记找不到", "warning")
            break
        if time.monotonic() - started > _NESTED_PROBE_SECONDS:
            _job_log(f"动漫 115 找这一包探了 {_NESTED_PROBE_SECONDS} 秒还没找到，先记找不到", "warning")
            break
        probes += 1
        nested = _list_dir(session, directory.path, deadline)
        if any(_node_file_id(item) == file_id for item in nested):
            owners.append(directory)
    if len(owners) == 1:
        return owners[0], ""
    if len(owners) > 1:
        return None, _dir_reason(owners)
    return None, ""


def _dir_reason(nodes: list[Node]) -> str:
    return "不止一个目录：" + "、".join(item.name for item in nodes[:8])


def _file_and_sidecars(children: list[Node], video: Node) -> list[Node]:
    stem = video.name.rsplit(".", 1)[0] if "." in video.name else video.name
    home = parent_path(video.path)
    kept = []
    for item in children:
        if item.path == video.path:
            kept.append(item)
            continue
        if item.is_dir or parent_path(item.path) != home:
            continue
        if _is_video_file(item):
            continue
        if _is_subtitle(item.name) or item.name.startswith(stem + ".") or item.name.startswith(stem + " "):
            kept.append(item)
    return kept


def _subtitle_in_dirs(session, children: list[Node], deadline: float = None) -> list[Node]:
    """子目录里的字幕文件。

    很多资源把字幕放在 Subs、字幕、CHS 这类子目录里，只列这些目录，不乱扫。
    """
    found = []
    for item in children:
        if not getattr(item, "is_dir", False):
            continue
        if not is_subtitle_dir(item.name):
            continue
        try:
            nodes = _list_dir(session, item.path, deadline)
        except JobStopped:
            raise
        except Exception:
            continue
        for node in nodes:
            if getattr(node, "is_dir", False):
                continue
            if is_subtitle_name(node.name):
                found.append(node)
    return found


# 特别篇标记：OVA、OAD、番外。出现在集数前面，说明这是同一季里的附赠内容。
_SPECIAL_MARKER = re.compile(
    r"(?i)(?:\[(?:OVA\d*|OAD|番外|特別編|特别篇)\]|(?<![A-Za-z])(?:OVA\d*|OAD|番外)(?=\s*[-–\[【\s]))",
)


def _main_videos(videos: list[Node]) -> list[Node]:
    """一个包里认得出集数的才算正片。

    BD 目录里常混着 NCOP、NCED、MENU、OAD、PV 这类没有集数的花絮，
    它们认不出集数；只要包里还有认得出集数的正片，就把花絮排除掉。
    全都认不出时保持原样，交给上层按「集数对不上」处理，不乱搬。

    带 OVA、OAD、番外标记的文件也算特别篇：包里同时有正片时只留正片，
    不让 OVA 变成同一集的另一版本；但整包都是 OVA 时（有些番的 OVA 就是那一季的正片）
    保持原样。
    """
    if len(videos) <= 1:
        return videos
    numbered = [item for item in videos if _pack_episode_full(item.name) > 0]
    if not numbered:
        return videos
    specials = [item for item in numbered if _SPECIAL_MARKER.search(item.name)]
    plain = [item for item in numbered if not _SPECIAL_MARKER.search(item.name)]
    if specials and plain:
        return plain
    return numbered


def _videos_for_task(files: list[Node], task: dict, season_caps: dict = None) -> list[Node]:
    """这一包里只有一个正片就用它。多个正片时只留集数对得上的，不对跨包合并。"""
    # 只排除有明确花絮标记的文件；全部是花絮时也不能回退为正片。
    original = _real_videos(files)
    videos = [item for item in original if not _is_extra_video(item.name)]
    videos = _main_videos(videos)
    season = int(task.get("season") or 0)
    episode = int(task.get("episode") or 0)
    show = str(task.get("show") or "")

    def _match(item) -> bool:
        parsed = _episode_of_name(item.name, season)
        if parsed is None:
            return False
        resolved = _resolve_episode(show, _content_season(item.name) or 0, season,
                                    parsed[1], season_caps or {})
        return _same_episode(resolved, season, episode)

    if len(videos) <= 1:
        # 剔除花絮后剩下的正片不能冒充任务所需的另一集。
        if videos and len(original) > 1 and episode > 0:
            if not _match(videos[0]):
                return []
        return videos
    if episode > 0:
        named = [item for item in videos if _match(item)]
        if len(named) == 1:
            return named
        titled = [item for item in named if _title_hits(item, task)]
        if len(titled) == 1:
            return titled
        if named:
            return named
    titled = [item for item in videos if _title_hits(item, task)]
    if len(titled) == 1:
        return titled
    return videos


def _real_videos(files: list[Node]) -> list[Node]:
    """外壳路径下面还有文件时，外壳自己不算视频。"""
    paths = [norm(item.path) for item in files]
    found = []
    for item in files:
        if not _is_video_file(item):
            continue
        prefix = norm(item.path) + "/"
        if any(path.startswith(prefix) for path in paths):
            continue
        found.append(item)
    return found


# BD 整包里混着的花絮/宣传视频：不是正片，单集判定时要剔掉，
# 否则「正片 + PV」会被当成「不止一个视频」整包放下不动。
# 只认这些明确的标记，不碰正常标题（SPY×FAMILY 里的 SP 不会误判）。
_EXTRA_VIDEO = re.compile(
    r"(?<![A-Za-z])(?:PV|CM|MENU|NCOP|NCED|ED|OP|OAD|Yokoku|Preview|Trailer)\d*(?=\s*(?:[\].)(]|$))|"
    r"Non[- ]?telop|ノンテロップ|"
    r"予告|预告|預告|キャラクターPV|ボイスドラマ|Voice\s*Drama|"
    r"映像特典|メイキング|片頭|片尾",
    re.IGNORECASE,
)

# 独立的方括号花絮标签：这种文件整个就是花絮。
_EXTRA_TAG_ONLY = re.compile(
    r"\[(?:SP\d*|MENU|Yokoku|Preview|Trailer|预告|特装|特典)\]",
    re.IGNORECASE,
)

# 方括号里的花絮说明（[キャラクターPV]、[ボイスドラマ]…）。
_EXTRA_BRACKET = re.compile(
    r"\[(?:[^\]】]*(?:キャラクターPV|ボイスドラマ|映像特典|メインニュース|ノンテロップ|"
    r"Non[- ]?telop|Epilogue|エピローグ|予告|特装|特典|Voice\s*Drama)[^\]】]*)\]",
    re.IGNORECASE,
)

# 圆括号里的花絮说明（(CM 01)、(PV 02)、(Non-telop Epilogue)…）。
_EXTRA_PAREN = re.compile(
    r"\((?:PV|CM|MENU|NCOP|NCED|ED|OP|OAD|Yokoku|Preview|Trailer|预告|特装)[^)]*\)",
    re.IGNORECASE,
)

# 只是一枚花絮标记的方括号，不算这一集自己的标题。
_EXTRA_ONLY_WORD = re.compile(
    r"^(?:PV|CM|MENU|NCOP|NCED|ED|OP|OAD|SP\d*|Mini\d*|Yokoku|Preview|Trailer)$",
    re.IGNORECASE,
)


def _content_text(name: str) -> str:
    """去掉开头的字幕组。内容标记只看剩下的部分，组名不算花絮。"""
    return re.sub(r"^[\[【][^\]】]{1,40}[\]】]\s*", "", name or "")


def _has_title_bracket(name: str) -> bool:
    """文件名里有没有这一集自己的标题（方括号里的正经剧集名）。

    有的话说明文件是正式的一集，里面的 NCED、PV 之类只是说明这一集带了什么。
    """
    for segment in re.findall(r"[\[【]([^\]】]+)[\]】]", name or ""):
        segment = segment.strip()
        if len(segment) < 3 or _EXTRA_ONLY_WORD.match(segment):
            continue
        if _EXTRA_BRACKET.search(f"[{segment}]"):
            continue
        if _pack_is_noise_segment(segment):
            continue
        if re.fullmatch(r"[\d\s\-~,.]+", segment):
            continue
        if len(segment) < 8 and len(segment.split()) < 2:
            continue
        return True
    return False


def _is_extra_video(name: str) -> bool:
    """是不是花絮/宣传视频（PV、CM、MENU、NCOP/NCED、予告、SP 等）。"""
    text = name or ""
    # Mini/SP 的编号是特别篇编号，不能与正片同号合并成“另一版本”。
    special = re.search(r"(?<![A-Za-z])(?:Mini|SP)[ _-]*[\[【]?\d+", text, re.IGNORECASE)
    if special:
        return True
    content = _content_text(text)
    if _EXTRA_TAG_ONLY.search(content) or _EXTRA_BRACKET.search(content):
        return True
    if _EXTRA_PAREN.search(content):
        return True
    # [PV] 出现在集数前面时是「这是 PV」的标记（蓝光合集里常见），要排除；
    # 出现在片名后面、后面没有集数时，是这一集自带的内容，靠下面的片名判断。
    pv = re.search(r"[\[【]\s*PV\s*[\]】]", content, re.IGNORECASE)
    if pv and re.search(r"[\[【]\s*[0-9]{1,3}\s*[\]】]", content[pv.end():]):
        return True
    if _has_title_bracket(content):
        # 名字里带着这一集自己的标题（[The Sealed Sword Frenzy]），
        # NCED、PV 之类只是说明这一集里带了什么，不是花絮本体。
        return False
    return bool(_EXTRA_VIDEO.search(content))


def _title_hits(video: Node, task: dict) -> bool:
    title = str(task.get("title") or "")
    blob = f"{video.path} {video.name}"
    if title and (title_matches(blob, Show(keyword=title)) or title_matches(title, Show(keyword=video.name))):
        return True
    show = str(task.get("show") or "")
    return bool(show) and title_matches(blob, Show(keyword=show))


def _same_episode(parsed: tuple[int, int] | None, season: int, episode: int) -> bool:
    if parsed is None or parsed[1] != episode:
        return False
    return not season or parsed[0] == season


def _task_finished(task: dict) -> bool:
    status = str(task.get("status", task.get("status_text", ""))).strip().lower()
    if status in {"-1", "3", "4", "fail", "failed", "error", "失败"}:
        return False
    if status in {"2", "complete", "completed", "已完成"}:
        return True
    percent = task.get("percentDone", task.get("percent"))
    try:
        return float(percent) >= 100
    except (TypeError, ValueError):
        return False


def _flatten_list(session, root: str, depth: int, deadline: float = None) -> list[Node]:
    found = []
    stack = [(norm(root), 0)]
    seen = set()
    while stack:
        if _cancelled(session):
            raise JobStopped("已停止")
        current, level = stack.pop()
        if current in seen or level > depth:
            continue
        seen.add(current)
        for item in _list_dir(session, current, deadline):
            node = item if isinstance(item, Node) else Node(
                path=norm(getattr(item, "path", "")),
                name=getattr(item, "name", "") or name_of(getattr(item, "path", "")),
                is_dir=bool(getattr(item, "is_dir", False) or getattr(item, "type", "") == "dir"),
                size=int(getattr(item, "size", 0) or 0),
                raw=item,
            )
            found.append(node)
            if node.is_dir:
                stack.append((norm(node.path), level + 1))
    return found


def _explicit_season(name: str) -> int | None:
    """名字里明确写出的季号。没写就返回空。"""
    from .anime_names import explicit_season

    return explicit_season(name)


# 整包里常见的集数写法：[12END]、24 END。通用解析认不出这些，拆包时兜一下。
_PACK_EPISODE_BRACKET = re.compile(r"\[(\d{1,3})\s*(?:END|E)\]", re.IGNORECASE)
_PACK_EPISODE_TRAILING = re.compile(r"(?<![\d.\-])(\d{1,2})\s+END\b", re.IGNORECASE)


def _pack_episode(name: str) -> int:
    """整包里的集数兜底。认不出就返回 0。"""
    text = name or ""
    matched = _PACK_EPISODE_BRACKET.search(text)
    if matched:
        return int(matched.group(1))
    matched = _PACK_EPISODE_TRAILING.search(text)
    if matched:
        return int(matched.group(1))
    return 0


# 整包里文件名末尾直接跟集数：Strike the Blood 23
# 数字前面不能挨着字母（SP30、OVA2 这类特别篇不算正片集数），也不能挨着数字。
_PACK_TRAILING_NUMBER = re.compile(r"(?<![0-9A-Za-z])(\d{1,3})\s*$")
# 方括号里的纯数字：[04][1080P][简体] 这种 OVA 整包里常见
_PACK_TAG_NUMBER = re.compile(r"[\[【]\s*(\d{1,3})\s*[\]】]")
# 文件名里的方括号标签
_PACK_TAGS = re.compile(r"[\[\(【][^\]\)】]*[\]\)】]")
# 蓝光分卷文件：Vol.4.mkv 是一整卷，不是第四集。
_PACK_VOLUME_FILE = re.compile(r"(?:^|[\s\[(])(?:vol(?:ume)?)[\s._-]*\d{1,2}(?=$|[\].)\s])", re.IGNORECASE)
# 包名写明混了多季：S1+S2、S1&S2、S1/S2。
_PACK_MULTI_SEASON = re.compile(r"[Ss]\s*(\d{1,2})\s*[+&/]\s*[Ss]\s*(\d{1,2})")


def _pack_episode_full(name: str) -> int:
    """整包里文件名的集数。通用解析认不出的写法在这里兜底。
    - 「24 END」「[12END]」这类
    - 标题后面直接跟集数：Strike the Blood 23
    - OVA、SP 的文件名：通用解析一律不收，但整包里它们同样是这一集的内容
    """
    text = name or ""
    if _PACK_VOLUME_FILE.search(text):
        # Vol.4.mkv 是一整卷，不能当成第四集。
        return 0
    episode = _pack_episode(text)
    if episode > 0:
        return episode
    parsed = parse_episode(text)
    if parsed:
        return int(parsed[1])
    if _has_episode_range(text):
        # 01-12 这种是一整个范围文件，不是单独一集
        return 0
    stripped = _PACK_TAGS.sub(" ", text)
    if "." in stripped:
        stripped = stripped.rsplit(".", 1)[0]
    matched = _PACK_TRAILING_NUMBER.search(stripped.strip())
    if matched:
        number = int(matched.group(1))
        if 0 < number < 1000:
            return number
    # 方括号里的纯数字：[04][1080p][简体]
    for tag in _PACK_TAG_NUMBER.finditer(text):
        number = int(tag.group(1))
        if 0 < number < 1000:
            return number
    return 0


def _pack_group_segment(name: str) -> str:
    """文件名开头方括号里的字幕组。组名不是剧名，不作为比对依据。"""
    matched = re.match(r"^[\[【]([^\]】]{1,40})[\]】]", name or "")
    return matched.group(1) if matched else ""


def _pack_is_noise_segment(segment: str) -> bool:
    """方括号里这一段是不是技术标签或集数标记。"""
    text = segment or ""
    if re.fullmatch(r"\d{1,3}(?:v\d+)?", text.strip()):
        # 纯数字的方括号是集数，不是剧名。
        return True
    if _PACK_RANGE_RE.search(text):
        return True
    if re.search(r"(?i)(?:第\s*\d+\s*[话話集]|ep(?:isode)?\s*\d+|\d{1,3}\s*end|\b(?:ova|oad|sp|mini)\d*\b)", text):
        return True
    latin, cjk = _pack_words(text)
    if not cjk and all(_pack_is_noise(token) for token in latin):
        return True
    if cjk and any(word in text for word in _PACK_CJK_NOISE):
        return True
    return False


def _pack_series_labels(name: str) -> list[str]:
    """从文件名里取出可能的剧名写法：去掉字幕组、技术标签和集数。"""
    text = name or ""
    if "." in text:
        text = text.rsplit(".", 1)[0]
    group = _pack_group_segment(name)
    body = text
    if group:
        body = re.sub(r"^[\[【]" + re.escape(group) + r"[\]】]\s*", "", body)
    labels = []
    for segment in re.findall(r"[\[【]([^\]】]+)[\]】]", body):
        segment = segment.strip()
        if len(segment) < 2 or _pack_is_noise_segment(segment):
            continue
        labels.append(segment)
    cleaned = _PACK_TAGS.sub(" ", body)
    cleaned = re.sub(r"(?i)\b(?:s\d{1,2}e\d{1,3}|ova\d*|oad|sp\d*|mini)\b", " ", cleaned)
    cleaned = re.sub(r"(?i)\s+end\b", " ", cleaned)
    cleaned = re.sub(r"(?i)\s+s\d{1,2}\b", " ", cleaned)
    cleaned = re.sub(r"(?:\s*[-–]\s*|\s+)\d{1,3}(?:\.\d+)?\s*$", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) >= 2:
        labels.append(cleaned)
    return labels


def _pack_label_in(target: str, label: str) -> bool:
    """这一段剧名写法在目标名字里出现吗。"""
    if not target or not label:
        return False
    if title_matches(target, Show(keyword=label)):
        return True
    # 中日文发布名常有差异（「女神のカフェテラス」对「女神的露天咖啡厅」），
    # 开头汉字对得上就当同一部；罗马字必须完整出现。
    chunks = _PACK_CJK_RE.findall(label)
    head = "".join(chunks)[:2]
    return len(head) >= 2 and head in target


def _content_season(name: str) -> int | None:
    """文件名里写明的季号。开头字幕组里的 S2、S3 之类不算季号。"""
    text = re.sub(r"^[\[【][^\]】]{1,40}[\]】]\s*", "", name or "")
    return _explicit_season(text)


def _path_season(node: Node) -> int | None:
    """文件所在目录里写明的季号。蓝光合集常按 S1、S2 分目录。

    只看目录，不看文件名：文件名里的 S2E01 说的是集，不是这一集属于哪一季。
    """
    parts = [part for part in (node.path or "").split("/")[:-1] if part]
    found = set()
    for part in parts:
        season = _explicit_season(part)
        if season and season > 0:
            found.add(season)
    if len(found) == 1:
        return next(iter(found))
    return None


def _pack_items(videos: list[Node], task: dict, pack_name: str, files: list[Node],
                season_caps: dict = None) -> list[dict]:
    """整包：按每个视频自己文件名里的集数，拆成一集一项。

    认不准就返回空列表，让上层按单集处理（宁可少收，不要收错）。
    要求：每个视频都有集数、互不重复、连号、季与包一致。
    """
    season = int(task.get("season") or 0)
    show = str(task.get("show") or "")
    # 包名写明的季：文件自己没写季时用它当底
    pack_seasons = _pack_seasons(pack_name or "")
    if len(pack_seasons) != 1:
        pack_seasons = set()
    fallback_season = next(iter(pack_seasons)) if pack_seasons else season
    # 每个文件自己写明的季号。parse_episode 认不出季时会默认第 1 季，不能拿它当依据。
    explicit_seasons = set()
    resolved_seasons = set()
    parsed_items = []
    for video in videos:
        parsed = parse_episode(video.name)
        # 季号先看文件名（S05E01 这类最明确），再看所在目录（蓝光合集按 S1、S2 分目录）。
        stated = _explicit_season(video.name)
        path_season = _path_season(video)
        explicit = stated if stated is not None else path_season
        episode = _pack_episode_full(video.name)
        if episode <= 0:
            # 认不出集数的文件（NCOP 之类）不参与拆分，留在包里
            continue
        if explicit is not None:
            explicit_seasons.add(explicit)
        if stated in (0, 1) and path_season is None:
            # 整部剧连着数时（第二季写成 S01E13），按各季集数换算它属于哪一季第几集
            resolved_season, episode = _resolve_episode(show, stated or 0, fallback_season,
                                                        episode, season_caps or {})
        else:
            resolved_season = explicit if explicit is not None else fallback_season
        resolved_seasons.add(resolved_season)
        parsed_items.append((video, episode, explicit, resolved_season))
    # 同一包可能混入另一部作品（标题不同但编号相同），不能当正片多版本。
    # 只保留剧名与包名或任务标题对得上的那些文件；对不上的整包留下不动。
    targets = [pack_name or "", str(task.get("title") or "")]
    labels_by_item = [[label for label in _pack_series_labels(item[0].name)
                       if any(_pack_label_in(target, label) for target in targets)]
                      for item in parsed_items]
    if not any(labels_by_item):
        # 一个都认不出来：这一包和这一集对不上，宁可留在原地。
        return []
    keep_index = {index for index, labels in enumerate(labels_by_item) if labels}
    parsed_items = [item for index, item in enumerate(parsed_items) if index in keep_index]
    explicit_seasons = {s for _, _, s, _ in parsed_items if s is not None}
    if len(explicit_seasons) > 1:
        # 文件里写着不同的季，不敢拆
        return []
    resolved_seasons = {s for _, _, _, s in parsed_items}
    if len(resolved_seasons) > 1:
        # 换算后这些文件分属不同季，不敢整包按一季拆
        return []
    if not explicit_seasons and _PACK_MULTI_SEASON.search(pack_name or ""):
        # 包名写明混了多季（S1+S2），文件自己又没写属于哪一季，不敢整包按一季拆。
        return []
    season = next(iter(resolved_seasons)) if resolved_seasons else season
    if season <= 0:
        return []
    items = []
    used = set()
    for video, episode, explicit, resolved in parsed_items:
        if episode <= 0:
            return []
        if resolved != season:
            # 文件换算后属于别的季，不敢拆
            return []
        if episode in used:
            # 同一集有多个版本（NC、CM&PV 之类）：算多版本，一起搬
            for item in items:
                if item["episode"] == episode:
                    item["videos"].append(video)
                    break
            continue
        used.add(episode)
        items.append({"video": video, "videos": [video], "season": season, "episode": episode})
    if len(items) < 2:
        return []
    numbers = sorted(item["episode"] for item in items)
    if numbers != list(range(numbers[0], numbers[-1] + 1)):
        # 部分集已搬走后，源目录会有空号。只有任务标题与该包目录明确声明
        # 同一个范围、且所有文件集数都在范围内时，才继续搬剩余集；不猜编号。
        declared = parse_episode_range(name_of(pack_name))
        requested = parse_episode_range(str(task.get("title") or ""))
        if not declared or declared != requested or not all(declared[0] <= n <= declared[1] for n in numbers):
            return []
    return sorted(items, key=lambda item: item["episode"])


def _pack_sidecars(files: list[Node], video: Node) -> list[Node]:
    """和这一集同名的字幕、字体等附属文件，跟着一起搬。"""
    stem = video.name.rsplit(".", 1)[0]
    home = parent_path(video.path)
    kept = []
    for item in files:
        if item.path == video.path or item.is_dir:
            continue
        if parent_path(item.path) != home:
            continue
        # 字幕只由 match_subtitles 按当前集匹配；不能每集都搬整季字幕。
        if not _is_subtitle(item.name) and _suffix(item.name) not in VIDEO_EXTS and (
            item.name.startswith(stem + ".") or item.name.startswith(stem + " ")
        ):
            kept.append(item)
    return kept


def _move_name(item: dict, node: Node) -> str:
    """搬到季目录时的名字。

    字幕改成跟这一集的视频同名，只换扩展名：整包里各集的字幕常常重名，
    直接搬会撞车。
    """
    subs = {getattr(sub, "path", "") for sub in item.get("subs") or []}
    if getattr(node, "path", "") not in subs:
        return node.name
    video = item.get("video")
    stem = (video.name.rsplit(".", 1)[0] if video is not None else node.name) or node.name
    suffix = _suffix(node.name) or ".ass"
    # 同主干字幕保留 SC/TC 等原始后缀，避免简繁两份改成同一个名字。
    if node.name.startswith(stem + "."):
        return node.name
    from .anime_subs import language_suffix
    name = f"{stem[:190]}.{language_suffix(node.name)}{suffix}"
    return name


def _show_caps(caps: dict, show: str) -> dict[int, int]:
    """这部剧每一季各多少集。从订阅上限里取，认不出来的季不出现。"""
    if not caps or not show:
        return {}
    exact: dict[int, int] = {}
    for (name, number), total in caps.items():
        try:
            season = int(number)
            size = int(total)
        except (TypeError, ValueError):
            continue
        if season <= 0 or size <= 0:
            continue
        if name == show:
            exact[season] = size
    if exact:
        return exact
    merged: dict[int, int] = {}
    for (name, number), total in caps.items():
        try:
            season = int(number)
            size = int(total)
        except (TypeError, ValueError):
            continue
        if season <= 0 or size <= 0:
            continue
        if name.startswith(show) or show.startswith(name):
            merged[season] = size
    return merged


def _absolute_season(show: str, episode: int, caps: dict) -> tuple[int, int] | None:
    """按各季集数累加，把「整部剧连着数」的集号换算成第几季第几集。

    有些大包接着上一季的集数往下写：两季的剧，第二季按规矩该写 S02E01，
    他们写成 S01E13。这里按每季的集数累加，找出这个集号真正属于哪一季。
    换算不出来返回空，不猜。
    """
    mapping = _show_caps(caps, show)
    if not mapping or episode <= 0:
        return None
    total = 0
    for season in sorted(mapping):
        size = mapping[season]
        if total < episode <= total + size:
            return (season, episode - total)
        total += size
    return None


def _resolve_episode(show: str, stated_season: int, fallback_season: int,
                     episode: int, caps: dict) -> tuple[int, int]:
    """确定文件属于第几季第几集。

    文件自己写的季集数在订阅上限之内就按它来；超出上限、而文件写的是第 1 季或
    没写季时，按「整部剧连着数」的写法换算（发布组接着上一季的集数往下写，
    两季的剧第二季写成 S01E13）。换算不出来保留原样，让上层按「集数对不上」处理。
    """
    if episode <= 0:
        return (stated_season or fallback_season, episode)
    mapping = _show_caps(caps, show)
    if stated_season:
        size = mapping.get(stated_season)
        if size and episode <= size:
            return (stated_season, episode)
        if stated_season == 1:
            resolved = _absolute_season(show, episode, caps)
            if resolved:
                return resolved
        return (stated_season, episode)
    size = mapping.get(fallback_season)
    if size and episode <= size:
        return (fallback_season, episode)
    resolved = _absolute_season(show, episode, caps)
    if resolved:
        return resolved
    return (fallback_season, episode)


def _decide_move(task: dict, files: list[Node], library: str, pack_name: str = "",
                 season_caps: dict = None) -> dict:
    # 整包优先：包里多集时按每集自己的集数拆开入库。
    all_videos = [item for item in _real_videos(files) if not _is_extra_video(item.name)]
    all_videos = _main_videos(all_videos)
    if len(all_videos) > 1:
        items = _pack_items(all_videos, task, pack_name, files, season_caps)
        if items:
            show = safe_dir_name(task.get("show") or "unknown")
            season = items[0]["season"]
            cap = _season_cap(season_caps or {}, str(task.get("show") or ""), season)
            if cap and any(int(item.get("episode") or 0) > cap for item in items):
                # 集数超出本季上限：多半是跨季的绝对集数（蓝光合集常见），不猜不搬。
                return {
                    "action": "leave",
                    "reason": f"集数超出本季上限 {cap} 集",
                    "task": task.get("infohash"),
                    "nodes": [],
                }
            dest_dir = join_path(join_path(library, show), f"Season {season:02d}")
            for item in items:
                nodes = []
                subs = []
                for video in item.get("videos") or [item["video"]]:
                    sidecars = _pack_sidecars(files, video)
                    # 字幕可能在 Subs、字幕这样的子目录里，按集数认。
                    found = match_subtitles(files, video, int(item.get("episode") or 0),
                                           int(item.get("season") or 0))
                    known = {node.path for node in sidecars}
                    for node in found:
                        if node.path not in known:
                            sidecars.append(node)
                            known.add(node.path)
                    nodes.append(video)
                    nodes.extend(sidecars)
                    for node in found:
                        if node not in subs:
                            subs.append(node)
                item["nodes"] = nodes
                item["subs"] = subs
            return {
                "action": "move",
                "reason": "ok",
                "task": task.get("infohash"),
                "dest_dir": dest_dir,
                "name": None,
                "season": season,
                "episode": 0,
                "nodes": [node for item in items for node in item["nodes"]],
                "items": items,
                "pack_episodes": len(items),
            }
    videos = _videos_for_task(files, task, season_caps)
    if not videos:
        present = [item for item in files if _is_video_file(item)]
        reason = "没有视频" if not present else "集数对不上"
        return {"action": "leave", "reason": reason, "task": task.get("infohash"), "nodes": []}
    if len(videos) != 1:
        listed = "、".join(item.name for item in videos[:8])
        return {"action": "leave", "reason": f"不止一个视频：{listed}", "task": task.get("infohash"), "nodes": []}
    video = videos[0]
    task_season = int(task.get("season") or 0)
    parsed = _episode_of_name(video.name, task_season)
    from_file = parsed is not None
    if parsed is None:
        # 文件名里没有集数：只能靠任务标题认，标题也没有就说明这一集认不准。
        parsed = _episode_of_name(str(task.get("title") or ""), task_season)
    if parsed is None:
        return {"action": "leave", "reason": "uncertain", "task": task.get("infohash"), "nodes": []}
    season, episode = parsed
    if from_file:
        # 整部剧连着数时（第二季写成 S01E13），按各季集数换算它属于哪一季第几集
        season, episode = _resolve_episode(str(task.get("show") or ""),
                                            _content_season(video.name) or 0, task_season,
                                            episode, season_caps or {})
    if season <= 0 or episode <= 0:
        return {"action": "leave", "reason": "uncertain", "task": task.get("infohash"), "nodes": []}
    cap = _season_cap(season_caps or {}, str(task.get("show") or ""), season)
    if cap and episode > cap:
        return {
            "action": "leave",
            "reason": f"集数超出本季上限 {cap} 集",
            "task": task.get("infohash"),
            "nodes": [],
        }
    show = safe_dir_name(task.get("show") or "unknown")
    dest_dir = join_path(join_path(library, show), f"Season {season:02d}")
    stem = video.name.rsplit(".", 1)[0]
    home = parent_path(video.path)
    sidecars = [
        item for item in files
        if item.path != video.path
        and not item.is_dir
        and parent_path(item.path) == home
        and (_is_subtitle(item.name) or _suffix(item.name) not in VIDEO_EXTS and (
            item.name.startswith(stem + ".") or item.name.startswith(stem + " ")
        ))
    ]
    subs = match_subtitles(files, video, episode, season)
    known = {item.path for item in sidecars}
    for item in subs:
        if item.path not in known:
            sidecars.append(item)
            known.add(item.path)
    return {
        "action": "move",
        "reason": "ok",
        "task": task.get("infohash"),
        "dest_dir": dest_dir,
        "name": video.name,
        "season": season,
        "episode": episode,
        "nodes": [video, *sidecars],
        "subs": subs,
    }


_SEASON_DIR = re.compile(r"^season\s*(\d{1,2})$", re.IGNORECASE)


def _apply_strm(decision: dict, config: JobConfig, write: bool, cancel=None) -> None:
    """整理成功后写 strm。strm 根为空就什么都不做。"""
    root = (config.strm_root or "").strip()
    if not root or decision.get("action") != "move":
        return
    if decision.get("items"):
        for item in decision["items"]:
            _apply_strm_item(decision, item, config, write=write, cancel=cancel)
        return
    video = next((item for item in decision.get("nodes") or [] if item.name == decision.get("name")), None)
    if video is None:
        decision["strm_error"] = "没有视频"
        return
    result = _write_strm_for(decision, config, video, int(decision.get("season") or 0),
                              int(decision.get("episode") or 0), write=write, cancel=cancel)
    decision.update(result)
    if result.get("strm_relative") and not result.get("strm_skipped"):
        _apply_subtitles(decision, decision.get("subs") or [], config,
                         result["strm_relative"], write=write, cancel=cancel)


def _apply_strm_item(decision: dict, item: dict, config: JobConfig, write: bool, cancel=None) -> None:
    """整包里的一集：单独写一个 strm。同一集有多个版本时每个版本一个 strm。"""
    videos = [video for video in (item.get("videos") or []) if video is not None]
    if not videos and item.get("video") is not None:
        videos = [item["video"]]
    if not videos:
        item["strm_error"] = "没有视频"
        return
    written = []
    errors = []
    for video in videos:
        result = _write_strm_for(
            decision,
            config,
            video,
            int(item.get("season") or 0),
            int(item.get("episode") or 0),
            write=write,
            cancel=cancel,
        )
        if result.get("strm"):
            written.append(result["strm"])
        if result.get("strm_error"):
            errors.append(result["strm_error"])
        if result.get("strm_skipped"):
            item["strm_skipped"] = result["strm_skipped"]
        if result.get("strm_relative") and not result.get("strm_skipped"):
            _apply_subtitles(decision, item.get("subs") or [], config,
                             result["strm_relative"], write=write, cancel=cancel, item=item)
    if written:
        item["strm"] = written[0]
        item["strms"] = written
    if errors:
        item["strm_error"] = errors[0]


def _apply_subtitles(decision: dict, subs: list, config: JobConfig, relative: str,
                     write: bool, cancel=None, item: dict = None) -> None:
    """把这一集的字幕下载到 .strm 旁边。

    Emby 播 .strm 时视频走 115 直链，字幕得是本地真实文件才认。
    """
    target = item if item is not None else decision
    if not config.subtitle_enabled:
        return
    root = (config.strm_root or "").strip()
    if not root or not subs or not relative:
        return
    written = []
    for node in subs:
        if _event_set(cancel):
            return
        suffix = _suffix(node.name)
        if suffix not in SIDECAR_SUFFIXES:
            # Emby 挂不上 sup、sub 这些格式，写出去也没用
            target.setdefault("sub_skipped", f"{node.name}：Emby 不认 {suffix}")
            continue
        code = pickcode_of(node)
        if not code:
            target["sub_error"] = "字幕缺少 pickcode"
            continue
        try:
            url = _subtitle_url(config, code, node.name)
        except PlayUrlError as exc:
            target["sub_error"] = str(exc)
            continue
        sub_relative = subtitle_relative(relative, node.name)
        if not write:
            written.append(str(Path(root) / sub_relative))
            continue
        try:
            existing = Path(root) / sub_relative
            if existing.is_file() and existing.stat().st_size > 0:
                written.append(str(existing))
                continue
            data = unpack(fetch_bytes(url), node.name)
            if not ass_title_matches(data, node.name):
                raise ValueError("ASS 内部片名/集号与文件名矛盾，拒绝挂载")
            path = write_sidecar(root, sub_relative, data)
        except Exception as exc:
            _job_log(f"动漫 115 字幕下载失败 {node.name}：{exc}", "warning")
            target["sub_error"] = f"字幕下载失败：{exc}"
            continue
        written.append(path)
    if written:
        target["subs_written"] = written


def _write_strm_for(decision: dict, config: JobConfig, video, season: int, episode: int,
                     write: bool, cancel=None) -> dict:
    """给一集写 strm，返回结果。strm 根为空就什么都不做。"""
    root = (config.strm_root or "").strip()
    result: dict = {}
    if not root:
        return result
    code = pickcode_of(video)
    if not code:
        result["strm_error"] = "缺少 pickcode"
        return result
    show = name_of(parent_path(decision["dest_dir"]))
    dir_season = _season_dir_number(name_of(decision["dest_dir"]))
    season = dir_season or int(season or 0)
    if season <= 0 or episode <= 0:
        result["strm_error"] = "集数对不上"
        return result
    relative = strm_relative(show, season, episode, video.name)
    try:
        url = _play_url(config, code, video.name)
    except PlayUrlError as exc:
        result["strm_error"] = str(exc)
        return result
    result["strm_relative"] = relative
    result["strm_url"] = url
    if _local_episode_exists(root, relative, season, episode):
        result["strm_skipped"] = "季目录已有同集视频"
        return result
    if not write:
        result["strm"] = str(Path(root) / relative)
        return result
    try:
        result["strm"] = commit_strm(root, relative, url, cancel=cancel)
    except OSError as exc:
        result["strm_error"] = str(exc)
    return result


def _remove_empty_dir(session, decision: dict, pack: str) -> None:
    """整包搬空后，把原来那个包目录删掉。里面还有东西就不动。"""
    folder = norm(pack or "")
    if not folder or folder == "/" or not folder.startswith("/离线下载"):
        return
    try:
        if session.list_dir(folder):
            return
        session.delete_dir(folder)
    except Exception as exc:
        _job_log(f"动漫 115 清理空包目录失败 {folder}：{exc}", "warning")


def _local_episode_exists(root: str, relative: str, season: int, episode: int) -> bool:
    """目标季目录里已经有同集视频时不再叠一层 strm。本地目录读不到就当没有。"""
    try:
        folder = Path(root) / Path(relative).parent
        children = list(folder.iterdir())
    except OSError:
        return False
    for child in children:
        if child.suffix.lower() not in VIDEO_EXTS:
            continue
        parsed = parse_episode(child.name)
        if parsed is not None and parsed == (season, episode):
            return True
    return False


def _library_videos(session, library: str, limit: str) -> list[dict]:
    """库根下 Show/Season/视频。limit 可以是某一剧的目录。"""
    library = norm(library)
    if limit and norm(limit) != library:
        shows = [Node(norm(limit), name_of(limit), True)]
    else:
        shows = [item for item in _list_dir(session, library) if item.is_dir]
    rows = []
    for show in shows:
        for child in _list_dir(session, show.path):
            season_no = _season_dir_number(child.name) if child.is_dir else 0
            if season_no:
                for video in _list_dir(session, child.path):
                    if _is_video_file(video):
                        rows.append(_video_row(show.name, season_no, video))
                continue
            if _is_video_file(child):
                rows.append(_video_row(show.name, 0, child))
    return rows


@_single_job
def run_split(session, config: JobConfig, limit: str = "", report=None) -> dict:
    """把库里放错位置的整包拆开：季目录里再套一层的包，拆成一集一集。

    只处理「季目录里多出来的一层目录」，里面有多集且集数认得出来时才动。
    """
    library = strip_mount_prefix(config.library_root, config.mount_prefix) if config.library_root else ""
    root = (config.strm_root or "").strip()
    if not library or library == "/":
        return {"ok": False, "error": "没有填写库根", "items": [], "dry_run": config.dry_run}
    if not root:
        return {"ok": False, "error": "没有填写 strm 目录", "items": [], "dry_run": config.dry_run}
    chosen = strip_mount_prefix(limit, config.mount_prefix) if (limit or "").strip() else ""
    if chosen and chosen != library and not chosen.startswith(library + "/"):
        return {"ok": False, "error": "路径不在库根下", "items": [], "dry_run": config.dry_run}
    try:
        logged_in = _call(session, session.check_login)
    except JobStopped:
        return {"ok": False, "error": "已停止", "items": [], "dry_run": config.dry_run}
    except TimeoutError as exc:
        return {"ok": False, "error": f"115 登录检查超时：{exc}", "items": [], "dry_run": config.dry_run}
    if not logged_in:
        return {"ok": False, "error": "115 未登录", "items": [], "dry_run": config.dry_run}
    started = time.monotonic()
    deadline = started + _TASK_BUDGET * 3
    shows = _list_dir(session, chosen or library)
    items = []
    moved = failed = 0
    for show in [node for node in shows if node.is_dir]:
        if _cancelled(session):
            return {"ok": False, "error": "已停止", "items": items, "dry_run": config.dry_run}
        for season_dir in [node for node in _list_dir(session, show.path) if node.is_dir]:
            season_no = _season_dir_number(season_dir.name)
            if not season_no:
                continue
            for nested in [node for node in _list_dir(session, season_dir.path) if node.is_dir]:
                if budget_exceeded(started, _TASK_BUDGET * 3):
                    _job_log("动漫 115 拆包超过时间预算，先收尾", "warning")
                    return {"ok": True, "items": items, "moved": moved, "failed": failed,
                            "dry_run": config.dry_run}
                files = _flatten_list(session, nested.path, depth=2, deadline=deadline)
                videos = _real_videos(files)
                if len(videos) < 2:
                    continue
                items_item = _split_nested_pack(session, show, season_dir, nested, videos, files,
                                                config, deadline)
                if items_item is None:
                    continue
                item, done, bad = items_item
                items.append(_public_split(item))
                moved += done
                failed += bad
    return {"ok": True, "items": items, "moved": moved, "failed": failed, "dry_run": config.dry_run}


def _split_nested_pack(session, show: Node, season_dir: Node, nested: Node, videos: list[Node],
                       files: list[Node], config: JobConfig, deadline: float):
    """把季目录里多出来的那层包拆开。返回 (记录, 搬成几集, 失败几集)。"""
    season = _season_dir_number(season_dir.name)
    items = _pack_items(videos, {"season": season}, nested.name, files)
    if not items:
        return None
    decision = {
        "action": "move",
        "reason": "ok",
        "dest_dir": season_dir.path,
        "name": None,
        "season": season,
        "episode": 0,
        "items": [],
        "nodes": [],
        "pack": nested.path,
    }
    for item in items:
        item["nodes"] = [item["video"], *_pack_sidecars(files, item["video"])]
    decision["items"] = items
    decision["nodes"] = [node for item in items for node in item["nodes"]]
    if config.dry_run:
        decision["moved_episodes"] = len(items)
        decision["pack_episodes"] = len(items)
        return decision, 0, 0
    moved = 0
    failed = 0
    try:
        existing = {node.name for node in _list_dir(session, season_dir.path, deadline)}
    except JobStopped:
        raise
    except Exception:
        existing = set()
    for item in items:
        if item["video"].name in existing:
            # 季目录里已经有同名文件，这一集跳过，不覆盖
            item["skipped"] = "collision"
            continue
        ok = True
        for node in item["nodes"]:
            try:
                ok = _call(session, lambda node=node: session.move(node, season_dir.path, node.name),
                           _task_limit(deadline))
            except JobStopped:
                raise
            except Exception as exc:
                _job_log(f"动漫 115 拆包移动失败 {node.name}：{exc}", "warning")
                ok = False
            if not ok:
                break
            session.sleep(config.move_gap_seconds)
        if not ok:
            failed += 1
            item["error"] = "移动失败"
            continue
        moved += 1
        _apply_strm_item(decision, item, config, write=True, cancel=getattr(session, "cancel", None))
    decision["moved_episodes"] = moved
    decision["failed_episodes"] = failed
    try:
        if not session.list_dir(nested.path):
            session.delete_dir(nested.path)
    except Exception:
        pass
    return decision, moved, failed


def _video_row(show: str, season_no: int, video: Node) -> dict:
    parsed = parse_episode(video.name)
    season = season_no or (parsed[0] if parsed else 0)
    episode = parsed[1] if parsed else 0
    if season_no and parsed and parsed[0] != season_no:
        season = season_no
    return {"show": show, "season": season, "episode": episode, "name": video.name, "node": video}


def _strm_one(row: dict, config: JobConfig, cancel=None) -> dict:
    video = row["node"]
    if row["season"] <= 0 or row["episode"] <= 0:
        return {"action": "skip", "reason": "集数对不上", "name": video.name, "path": ""}
    code = pickcode_of(video)
    if not code:
        return {"action": "skip", "reason": "缺少 pickcode", "name": video.name, "path": ""}
    relative = strm_relative(row["show"], row["season"], row["episode"], video.name)
    try:
        url = _play_url(config, code, video.name)
    except PlayUrlError as exc:
        return {"action": "skip", "reason": str(exc), "name": video.name, "path": ""}
    path = str(Path(config.strm_root) / relative)
    if config.dry_run:
        return {"action": "write", "reason": "ok", "name": video.name, "path": path}
    try:
        written = commit_strm(config.strm_root, relative, url, cancel=cancel)
    except OSError as exc:
        return {"action": "write", "reason": "strm 写入失败", "error": str(exc), "name": video.name, "path": path}
    return {"action": "write", "reason": "ok", "name": video.name, "path": written}


def _play_url(config: JobConfig, pickcode: str, filename: str) -> str:
    """按 strm 模式生成播放地址。空模式仍只用原来的播放地址模板。"""
    return play_url_for_strm(
        config.play_base,
        pickcode,
        filename,
        play_base_lan=config.play_base_lan,
        strm_for=config.strm_for,
        public_hosts=config.public_hosts,
    )


def _subtitle_url(config: JobConfig, pickcode: str, filename: str) -> str:
    """本机下载字幕优先走已配置的 LAN 入口，不跟随 STRM 的公网播放模式。

    视频 STRM 的公网地址保持不变；没有配置 LAN 时沿用原播放模板。
    """
    if not str(config.play_base_lan or "").strip():
        return _play_url(config, pickcode, filename)
    return play_url_for_strm(
        config.play_base, pickcode, filename,
        play_base_lan=config.play_base_lan, strm_for="lan",
        public_hosts=config.public_hosts,
    )


def _season_dir_number(name: str) -> int:
    matched = _SEASON_DIR.match((name or "").strip())
    if not matched:
        return 0
    return int(matched.group(1))


def _public_split(decision: dict) -> dict:
    """拆包记录要能存进数据库，不能带着文件对象。"""
    hidden = {"nodes", "items", "subs", "strm_url", "strm_relative"}
    record = {key: value for key, value in decision.items() if key not in hidden}
    episodes = []
    for item in decision.get("items") or []:
        if not isinstance(item, dict):
            continue
        episodes.append({
            "episode": item.get("episode"),
            "season": item.get("season"),
            "name": getattr(item.get("video"), "name", ""),
            "strm": item.get("strm"),
            "error": item.get("error") or item.get("strm_error"),
            "skipped": item.get("skipped") or item.get("strm_skipped"),
        })
    if episodes:
        record["episodes"] = episodes
    return record


def _public_move(decision: dict) -> dict:
    """对外的记录不能带文件对象，否则存库会失败。"""
    hidden = {"nodes", "items", "subs", "strm_url", "strm_relative"}
    record = {key: value for key, value in decision.items() if key not in hidden}
    items = decision.get("items")
    if isinstance(items, list):
        record["episodes"] = [
            {
                "episode": item.get("episode"),
                "season": item.get("season"),
                "name": getattr(item.get("video"), "name", ""),
                "strm": item.get("strm"),
                "error": item.get("error") or item.get("strm_error"),
                "skipped": item.get("skipped") or item.get("strm_skipped"),
            }
            for item in items
            if isinstance(item, dict)
        ]
    return record


def _suffix(name: str) -> str:
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def new_task(pick_row: dict, folder: str, now: datetime) -> dict:
    """记一条已提交的离线任务。"""
    return {
        "infohash": pick_row["infohash"],
        "show": pick_row["show"],
        "season": pick_row["season"],
        "episode": pick_row["episode"],
        "title": pick_row["title"],
        "folder": folder,
        "status": "submitted",
        "at": now.isoformat(),
    }
