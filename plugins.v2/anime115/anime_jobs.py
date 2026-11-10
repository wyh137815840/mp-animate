"""每日补磁力、整理和摊平的编排。会话由调用方传入。"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .anime_dmhy import Pick, SearchOptions, select_shows
from .anime_flatten import Node, apply_proposals, propose
from .anime_names import Show, _fold_name, join_path, name_of, norm, parent_path, parse_episode, safe_dir_name, strip_mount_prefix, title_matches
from .anime_playurl import PlayUrlError, play_url_for_strm
from .anime_strm import commit_strm, pickcode_of, strm_relative


SUB_EXTS = {".ass", ".srt", ".ssa", ".sup", ".sub", ".idx", ".vtt"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".m2ts"}
_SHOW_YEAR = re.compile(r"^(.+?) [(（](\d{4})[)）]$")
# 开放平台和 p115client 都可能一直不返回。到点就结束这一轮，避免整理把后面的周期全部挡住。
_OFFLINE_CALL_TIMEOUT = 45


class JobStopped(Exception):
    """停止或重载把这一轮取消了。"""


def _event_set(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _cancelled(session) -> bool:
    return _event_set(getattr(session, "cancel", None))


def _call(session, func):
    """在限时内等 115 返回。停止或重载时不再占着这一轮。"""
    return _wait_call(getattr(session, "cancel", None), func, _OFFLINE_CALL_TIMEOUT)


def _wait_call(cancel, func, timeout: float):
    if _event_set(cancel):
        raise JobStopped("已停止")
    outcome = {}

    def run():
        try:
            outcome["value"] = func()
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="anime115-115", daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while worker.is_alive():
        if _event_set(cancel):
            raise JobStopped("已停止")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"115 请求超过 {int(timeout)} 秒")
        worker.join(min(0.5, remaining))
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


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


def run_daily(shows, session, entries_of, seen, now: datetime, config: JobConfig, report=None) -> dict:
    """搜索并在非预览时提交离线。预览不写 seen。"""
    if not shows:
        _tell(report, job="daily", phase="failed", dry_run=config.dry_run, index=0, total=0, current="", result="失败", failed=1)
        return {"ok": False, "error": "未配置追更", "picks": [], "dry_run": config.dry_run}
    inbox = _dated_inbox(config, now)
    picks, notes = select_shows(shows, entries_of, config.search, now)
    chosen = []
    for pick in picks:
        if _episode_blocked(seen, pick, now):
            notes.append({"show": pick.show, "title": pick.torrent.title, "reason": "已经提交过"})
            continue
        chosen.append(pick)
    chosen = chosen[: config.max_submit_per_run]
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
        return {"ok": False, "error": "115 未登录", "picks": [], "dry_run": False}
    try:
        quota = _quota_left(_call(session, session.quota))
    except JobStopped:
        return _daily_stopped(config, inbox, chosen, notes, [])
    except TimeoutError as exc:
        return {"ok": False, "error": f"离线配额查询超时：{exc}", "picks": [], "dry_run": False, "inbox": inbox}
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
    moves = []
    organized = skipped = failed = 0
    _tell(report, job="organize", phase="running", dry_run=config.dry_run, index=0, total=len(work), organized=0, skipped=0, failed=0, current="", result="")
    for index, pair in enumerate(work, start=1):
        if _cancelled(session):
            return _organize_status(config, [*pending, *moves], stopped=True, dirty=True, tasks=tasks, seen=seen)
        task, offline = pair
        try:
            _organize_one(task, session, library, config, moves, offline)
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


def _organize_one(task, session, library, config, moves, offline=None) -> None:
    files, pack, why, confirmed = _scope_to_pack(session, task, offline or {})
    if why:
        moves.append({"action": "leave", "reason": why, "task": task.get("infohash"), "nodes": [], "pack": pack})
        return
    decision = _decide_move(task, files, library)
    decision["pack"] = pack
    moves.append(decision)
    _close_empty_leftover(task, decision, confirmed, config)
    if decision["action"] == "move":
        show_folder, why = _show_folder(session, library, task.get("show") or "")
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
        made = session.ensure_dir(dest_dir)
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
        existing = {item.name for item in session.list_dir(dest_dir)}
    except Exception:
        existing = set()
    if decision["name"] in existing:
        decision["action"] = "leave"
        decision["reason"] = "collision"
        return
    for item in decision["nodes"]:
        if not session.move(item, dest_dir, item.name):
            decision["action"] = "leave"
            decision["reason"] = "移动失败"
            return
        session.sleep(config.move_gap_seconds)
    task["status"] = "moved"
    _apply_strm(decision, config, write=True, cancel=getattr(session, "cancel", None))
    _remember_strm(task, decision)


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
    try:
        for root in roots:
            if _cancelled(session):
                raise JobStopped("已停止")
            nodes.extend(_flatten_list(session, root, depth=6))
    except JobStopped:
        return {"ok": False, "error": "已停止", "counts": {}, "dry_run": config.dry_run, "moved": 0}
    if _cancelled(session):
        return {"ok": False, "error": "已停止", "counts": {}, "dry_run": config.dry_run, "moved": 0}
    proposals = propose(nodes, roots, config.min_episode_mb * 1024 * 1024)
    apply = (not config.dry_run) and config.flatten_apply
    moved = apply_proposals(proposals, session, apply, config.flatten_max_moves)
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


def _episode_blocked(seen: dict, pick: Pick, now: datetime) -> bool:
    hashes = seen.setdefault("hashes", {})
    episodes = seen.setdefault("episodes", {})
    if pick.torrent.infohash in hashes:
        return True
    key = f"{pick.show}|{pick.season}|{pick.episode}"
    previous = hashes.get(episodes.get(key, ""), {})
    if not previous:
        return False
    if previous.get("status") == "failed":
        return False
    submitted = _parse_time(previous.get("at"))
    if submitted and now - submitted > timedelta(days=3) and previous.get("status") != "moved":
        return False
    return True


def _remember(seen: dict, pick: Pick, now: datetime, folder: str) -> None:
    hashes = seen.setdefault("hashes", {})
    episodes = seen.setdefault("episodes", {})
    key = f"{pick.show}|{pick.season}|{pick.episode}"
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
        if session.list_dir(path):
            continue
        session.delete_dir(path)


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


def _accepted(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("state") in (False, 0, "false"):
        return False
    if payload.get("state") in (True, 1, "true"):
        return True
    code = payload.get("code")
    return code in (0, None) and "data" in payload


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


def _show_folder(session, library: str, show: str) -> tuple[str, str]:
    try:
        children = session.list_dir(library) or []
    except Exception:
        children = []
    names = [item.name for item in children if getattr(item, "is_dir", False)]
    return resolve_show_folder(names, show)


def _is_video_file(item: Node) -> bool:
    """字幕和同名附属文件不是视频。目录即使名叫 .mkv 也不是。"""
    if item.is_dir or _is_subtitle(item.name):
        return False
    return _suffix(item.name) in VIDEO_EXTS and "sample" not in item.name.lower()


def _is_subtitle(name: str) -> bool:
    return _suffix(name) in SUB_EXTS


def _episode_of_name(name: str) -> tuple[int, int] | None:
    """目录名常以 .mkv 结尾。去掉这一层再认集数。"""
    text = name or ""
    if _suffix(text) in VIDEO_EXTS or _suffix(text) in SUB_EXTS:
        text = text.rsplit(".", 1)[0]
    return parse_episode(text)


def _scope_to_pack(session, task: dict, offline: dict) -> tuple[list[Node], str, str, bool]:
    """只看这一条磁力自己的目录，不扫同一天里的其它包。

    最后一个值表示这一包是不是已经看到了。空列表也可能是路径不存在。
    """
    inbox = norm(task.get("folder") or "")
    if not inbox:
        return [], "", "没有离线目录", False
    wanted = _wanted_names(task, offline)
    file_id = _offline_file_id(offline)
    children = list(session.list_dir(inbox) or [])
    if _folder_is_pack(inbox, wanted):
        files = _flatten_list(session, inbox, depth=3)
        return files, inbox, "", _pack_seen(session, inbox, children, files)
    chosen, why = _choose_child(children, wanted, file_id)
    if why:
        return [], "", why, False
    if chosen is None and file_id:
        chosen, why = _choose_by_nested_id(session, children, file_id)
    if why:
        return [], "", why, False
    if chosen is not None:
        if chosen.is_dir:
            return _flatten_list(session, chosen.path, depth=3), chosen.path, "", True
        return _file_and_sidecars(children, chosen), chosen.path, "", True
    dirs = [item for item in children if item.is_dir]
    if not dirs:
        videos = _real_videos(children)
        if len(videos) > 1 and wanted:
            matched = _match_one(videos, wanted)
            if len(matched) == 1:
                return _file_and_sidecars(children, matched[0]), matched[0].path, "", True
        files = _flatten_list(session, inbox, depth=3)
        return files, inbox, "", _pack_seen(session, inbox, children, files)
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


def _pack_seen(session, folder: str, children: list, files: list) -> bool:
    """看到里面的文件，或父目录里有这一层，才算这一包存在。"""
    if children or files:
        return True
    return _dir_listed(session, folder)


def _dir_listed(session, folder: str) -> bool:
    folder = norm(folder)
    if not folder or folder == "/":
        return False
    parent = parent_path(folder)
    leaf = name_of(folder)
    try:
        children = session.list_dir(parent) or []
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


def _match_one(nodes: list[Node], wanted: list[str]) -> list[Node]:
    """按离线任务名、再按提交标题，各自单独对。对上一个就用它。"""
    for name in wanted:
        for mode in ("exact", "partial"):
            hits = _unique_paths(item for item in nodes if _name_hit(item.name, name, mode))
            if len(hits) == 1:
                return hits
    return []


def _choose_child(children: list[Node], wanted: list[str], file_id: str) -> tuple[Node | None, str]:
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
        partial = _prefer_dir([item for item in children if _name_hit(item.name, name, "partial")])
        if len(partial) == 1:
            return partial[0], ""
        if len(partial) > 1:
            ambiguous = partial
    if ambiguous:
        return None, _dir_reason(ambiguous)
    return None, ""


def _choose_by_nested_id(session, children: list[Node], file_id: str) -> tuple[Node | None, str]:
    """file_id 有时是外壳里面的正片，而不是外壳自己。"""
    owners = []
    for directory in children:
        if not directory.is_dir:
            continue
        nested = session.list_dir(directory.path) or []
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


def _videos_for_task(files: list[Node], task: dict) -> list[Node]:
    """这一包里只有一个正片就用它。多个正片时只留集数对得上的，不对跨包合并。"""
    videos = _real_videos(files)
    if len(videos) <= 1:
        return videos
    season = int(task.get("season") or 0)
    episode = int(task.get("episode") or 0)
    if episode > 0:
        named = [item for item in videos if _same_episode(_episode_of_name(item.name), season, episode)]
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


def _flatten_list(session, root: str, depth: int) -> list[Node]:
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
        for item in session.list_dir(current) or []:
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


def _decide_move(task: dict, files: list[Node], library: str) -> dict:
    videos = _videos_for_task(files, task)
    if not videos:
        present = [item for item in files if _is_video_file(item)]
        reason = "没有视频" if not present else "集数对不上"
        return {"action": "leave", "reason": reason, "task": task.get("infohash"), "nodes": []}
    if len(videos) != 1:
        listed = "、".join(item.name for item in videos[:8])
        return {"action": "leave", "reason": f"不止一个视频：{listed}", "task": task.get("infohash"), "nodes": []}
    video = videos[0]
    parsed = parse_episode(video.name) or parse_episode(task.get("title") or "")
    if parsed is None:
        parsed = (int(task.get("season") or 0), int(task.get("episode") or 0))
    season, episode = parsed
    if season <= 0 or episode <= 0:
        return {"action": "leave", "reason": "uncertain", "task": task.get("infohash"), "nodes": []}
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
    return {
        "action": "move",
        "reason": "ok",
        "task": task.get("infohash"),
        "dest_dir": dest_dir,
        "name": video.name,
        "season": season,
        "episode": episode,
        "nodes": [video, *sidecars],
    }


_SEASON_DIR = re.compile(r"^season\s*(\d{1,2})$", re.IGNORECASE)


def _apply_strm(decision: dict, config: JobConfig, write: bool, cancel=None) -> None:
    """整理成功后写 strm。strm 根为空就什么都不做。"""
    root = (config.strm_root or "").strip()
    if not root or decision.get("action") != "move":
        return
    video = next((item for item in decision.get("nodes") or [] if item.name == decision.get("name")), None)
    if video is None:
        decision["strm_error"] = "没有视频"
        return
    code = pickcode_of(video)
    if not code:
        decision["strm_error"] = "缺少 pickcode"
        return
    show = name_of(parent_path(decision["dest_dir"]))
    season = _season_dir_number(name_of(decision["dest_dir"])) or int(decision.get("season") or 0)
    episode = int(decision.get("episode") or 0)
    if season <= 0 or episode <= 0:
        decision["strm_error"] = "集数对不上"
        return
    relative = strm_relative(show, season, episode, video.name)
    try:
        url = _play_url(config, code, video.name)
    except PlayUrlError as exc:
        decision["strm_error"] = str(exc)
        return
    decision["strm_relative"] = relative
    decision["strm_url"] = url
    if not write:
        decision["strm"] = str(Path(root) / relative)
        return
    try:
        decision["strm"] = commit_strm(root, relative, url, cancel=cancel)
    except OSError as exc:
        decision["strm_error"] = str(exc)


def _library_videos(session, library: str, limit: str) -> list[dict]:
    """库根下 Show/Season/视频。limit 可以是某一剧的目录。"""
    library = norm(library)
    if limit and norm(limit) != library:
        shows = [Node(norm(limit), name_of(limit), True)]
    else:
        shows = [item for item in (session.list_dir(library) or []) if item.is_dir]
    rows = []
    for show in shows:
        for child in session.list_dir(show.path) or []:
            season_no = _season_dir_number(child.name) if child.is_dir else 0
            if season_no:
                for video in session.list_dir(child.path) or []:
                    if _is_video_file(video):
                        rows.append(_video_row(show.name, season_no, video))
                continue
            if _is_video_file(child):
                rows.append(_video_row(show.name, 0, child))
    return rows


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


def _season_dir_number(name: str) -> int:
    matched = _SEASON_DIR.match((name or "").strip())
    if not matched:
        return 0
    return int(matched.group(1))


def _public_move(decision: dict) -> dict:
    hidden = {"nodes", "strm_url", "strm_relative"}
    return {key: value for key, value in decision.items() if key not in hidden}


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
