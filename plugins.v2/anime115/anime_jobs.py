"""每日补磁力、整理和摊平的编排。会话由调用方传入。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from anime_dmhy import Pick, SearchOptions, select_shows
from anime_flatten import Node, apply_proposals, propose
from anime_names import join_path, name_of, norm, parent_path, parse_episode, safe_dir_name, strip_mount_prefix


SUB_EXTS = {".ass", ".srt", ".ssa", ".sup"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".m2ts"}


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


def run_daily(shows, session, entries_of, seen, now: datetime, config: JobConfig) -> dict:
    """搜索并在非预览时提交离线。预览不写 seen。"""
    if not shows:
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
    if config.dry_run:
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
            notes.append({"show": pick.show, "title": pick.torrent.title, "reason": "离线提交失败"})
            continue
        if _landed_in_cloud_download(result):
            notes.append({"show": pick.show, "title": pick.torrent.title, "reason": "落到了云下载"})
            continue
        _remember(seen, pick, now, inbox)
        submitted.append(_pick_row(pick))
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


def run_organize(tasks, session, config: JobConfig) -> dict:
    """只整理本插件提交过、且离线列表里已经完成的任务。"""
    library = strip_mount_prefix(config.library_root, config.mount_prefix) if config.library_root else ""
    if not library or library == "/":
        return {"ok": False, "error": "没有填写库根", "moves": [], "dry_run": config.dry_run}
    if not session.check_login():
        return {"ok": False, "error": "115 未登录", "moves": [], "dry_run": config.dry_run}
    done = _finished_hashes(session)
    moves = []
    for task in tasks:
        if task.get("status") == "moved":
            continue
        if task.get("infohash") not in done and not config.dry_run:
            continue
        if config.dry_run and task.get("infohash") not in done:
            continue
        folder = task.get("folder") or ""
        files = _flatten_list(session, folder, depth=3)
        decision = _decide_move(task, files, library)
        moves.append(decision)
        if config.dry_run or decision["action"] != "move":
            continue
        dest_dir = decision["dest_dir"]
        if session.ensure_dir(dest_dir) is None:
            decision["action"] = "leave"
            decision["reason"] = "建不了目标目录"
            continue
        existing = {item.name for item in session.list_dir(dest_dir)}
        if decision["name"] in existing:
            decision["action"] = "leave"
            decision["reason"] = "collision"
            continue
        ok = True
        for item in decision["nodes"]:
            if not session.move(item, dest_dir, item.name):
                ok = False
                break
            session.sleep(config.move_gap_seconds)
        if ok:
            task["status"] = "moved"
        else:
            decision["action"] = "leave"
            decision["reason"] = "移动失败"
    return {"ok": True, "dry_run": config.dry_run, "moves": [_public_move(item) for item in moves]}


def run_flatten(session, config: JobConfig) -> dict:
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
    for item in proposals:
        counts[item.kind] = counts.get(item.kind, 0) + 1
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
    found = set()
    for page in range(1, 6):
        payload = session.list_offline(page)
        tasks = _task_rows(payload)
        if not tasks:
            break
        for task in tasks:
            infohash = str(task.get("info_hash") or task.get("infohash") or "").lower()
            if infohash and _task_finished(task):
                found.add(infohash)
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
    videos = [item for item in files if not item.is_dir and _suffix(item.name) in VIDEO_EXTS and "sample" not in item.name.lower()]
    if len(videos) != 1:
        return {"action": "leave", "reason": "uncertain", "task": task.get("infohash"), "nodes": []}
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
    sidecars = [
        item for item in files
        if not item.is_dir and _suffix(item.name) in SUB_EXTS and item.name.rsplit(".", 1)[0] == stem
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
