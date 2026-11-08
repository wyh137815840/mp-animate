"""每日补磁力、整理和摊平的编排。会话由调用方传入。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .anime_dmhy import Pick, SearchOptions, select_shows
from .anime_flatten import Node, apply_proposals, propose
from .anime_names import Show, _fold_name, join_path, name_of, norm, parent_path, parse_episode, safe_dir_name, strip_mount_prefix, title_matches


SUB_EXTS = {".ass", ".srt", ".ssa", ".sup", ".sub", ".idx", ".vtt"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".m2ts"}
_SHOW_YEAR = re.compile(r"^(.+?) [(（](\d{4})[)）]$")


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
    if not session.check_login():
        return {"ok": False, "error": "115 未登录", "picks": [], "dry_run": False}
    quota = _quota_left(session.quota())
    if quota is not None and quota <= 0:
        return {"ok": False, "error": "离线配额用尽", "picks": [_pick_row(item) for item in chosen], "dry_run": False}
    folder = session.ensure_dir(inbox)
    fileid = getattr(folder, "fileid", "") if folder is not None else ""
    if not fileid:
        return {"ok": False, "error": "建不了当天目录", "inbox": inbox, "dry_run": False}
    for index, pick in enumerate(chosen):
        if quota is not None and quota <= 0:
            break
        if index:
            session.sleep(config.offline_gap_seconds)
        result = session.add_offline(pick.torrent.magnet, fileid)
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


def run_organize(tasks, session, config: JobConfig, report=None) -> dict:
    """只整理本插件提交过、且离线列表里已经完成的任务。"""
    library = strip_mount_prefix(config.library_root, config.mount_prefix) if config.library_root else ""
    if not library or library == "/":
        return {"ok": False, "error": "没有填写库根", "moves": [], "dry_run": config.dry_run}
    if not session.check_login():
        return {"ok": False, "error": "115 未登录", "moves": [], "dry_run": config.dry_run}
    done = _finished_tasks(session)
    work = []
    for task in tasks:
        if task.get("status") == "moved":
            continue
        infohash = str(task.get("infohash") or "").lower()
        if infohash not in done and not config.dry_run:
            continue
        if config.dry_run and infohash not in done:
            continue
        work.append(task)
    moves = []
    organized = skipped = failed = 0
    _tell(report, job="organize", phase="running", dry_run=config.dry_run, index=0, total=len(work), organized=0, skipped=0, failed=0, current="", result="")
    for index, task in enumerate(work, start=1):
        try:
            offline = done.get(str(task.get("infohash") or "").lower()) or {}
            _organize_one(task, session, library, config, moves, offline)
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
                result="ok" if last.get("action") == "move" else (last.get("reason") or "跳过"),
                organized=organized,
                skipped=skipped,
                failed=failed,
            )
    return {"ok": True, "dry_run": config.dry_run, "moves": [_public_move(item) for item in moves]}


def _organize_one(task, session, library, config, moves, offline=None) -> None:
    files, pack, why = _scope_to_pack(session, task, offline or {})
    if why:
        moves.append({"action": "leave", "reason": why, "task": task.get("infohash"), "nodes": [], "pack": pack})
        return
    decision = _decide_move(task, files, library)
    decision["pack"] = pack
    moves.append(decision)
    if decision["action"] == "move":
        show_folder, why = _show_folder(session, library, task.get("show") or "")
        if why:
            decision["action"] = "leave"
            decision["reason"] = why
        else:
            season_name = decision["dest_dir"].rsplit("/", 1)[-1]
            decision["dest_dir"] = join_path(join_path(library, show_folder), season_name)
    if decision["action"] != "move" or config.dry_run:
        return
    dest_dir = decision["dest_dir"]
    try:
        made = session.ensure_dir(dest_dir)
    except Exception:
        made = None
    if made is None or not str(getattr(made, "fileid", "") or ""):
        decision["action"] = "leave"
        decision["reason"] = "建不了目标目录"
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


def run_flatten(session, config: JobConfig, report=None) -> dict:
    """先分类。安全移动为 0，或预览、或没开摊平落盘时，不移动。"""
    roots = [strip_mount_prefix(item, config.mount_prefix) for item in config.flatten_roots if str(item).strip()]
    roots = [norm(item) for item in roots]
    if not roots or any(item == "/" for item in roots):
        return {"ok": False, "error": "摊平目录为空或是根目录", "counts": {}, "dry_run": config.dry_run}
    if not session.check_login():
        return {"ok": False, "error": "115 未登录", "counts": {}, "dry_run": config.dry_run}
    nodes = []
    for root in roots:
        nodes.extend(_flatten_list(session, root, depth=6))
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


def _finished_hashes(session) -> set[str]:
    return set(_finished_tasks(session))


def _finished_tasks(session) -> dict[str, dict]:
    """已完成的离线任务，按 infohash 记下名字和 file_id，用来认自己的那一包。"""
    found = {}
    for page in range(1, 6):
        payload = session.list_offline(page)
        tasks = _task_rows(payload)
        if not tasks:
            break
        for task in tasks:
            infohash = str(task.get("info_hash") or task.get("infohash") or "").lower()
            if infohash and _task_finished(task):
                found[infohash] = task
        if len(tasks) < 20:
            break
    return found


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


def _scope_to_pack(session, task: dict, offline: dict) -> tuple[list[Node], str, str]:
    """只看这一条磁力自己的目录，不扫同一天里的其它包。"""
    inbox = norm(task.get("folder") or "")
    if not inbox:
        return [], "", "没有离线目录"
    wanted = _wanted_names(task, offline)
    file_id = _offline_file_id(offline)
    children = list(session.list_dir(inbox) or [])
    if _folder_is_pack(inbox, wanted):
        return _flatten_list(session, inbox, depth=3), inbox, ""
    chosen, why = _choose_child(children, wanted, file_id)
    if why:
        return [], "", why
    if chosen is None and file_id:
        chosen, why = _choose_by_nested_id(session, children, file_id)
    if why:
        return [], "", why
    if chosen is not None:
        if chosen.is_dir:
            return _flatten_list(session, chosen.path, depth=3), chosen.path, ""
        return _file_and_sidecars(children, chosen), chosen.path, ""
    dirs = [item for item in children if item.is_dir]
    if not dirs:
        videos = _real_videos(children)
        if len(videos) > 1 and wanted:
            matched = _match_one(videos, wanted)
            if len(matched) == 1:
                return _file_and_sidecars(children, matched[0]), matched[0].path, ""
        return _flatten_list(session, inbox, depth=3), inbox, ""
    label = wanted[0] if wanted else (task.get("show") or task.get("infohash") or "")
    listed = "、".join(item.name for item in dirs[:8])
    return [], "", f"找不到这一包：{label}；现有 {listed}"


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
        "nodes": [video, *sidecars],
    }


def _public_move(decision: dict) -> dict:
    return {key: value for key, value in decision.items() if key != "nodes"}


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
