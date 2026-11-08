"""从动漫花园补连载磁力，并用 MoviePilot 已登录的 115 离线、整理、摊平。"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.chain.storage import StorageChain
from app.log import logger
from app.plugins import _PluginBase

from .anime_config import PLAY_BASE, offline_directory, usable_cookie
from .anime_pan import CookieSession
from .anime_progress import advance, format_progress, format_state
from .anime_dmhy import SearchOptions, fetch_list, fetch_rss, merge_sizes, parse_list_sizes, parse_rss
from .anime_flatten import Node
from .anime_jobs import JobConfig, new_task, run_daily, run_flatten, run_organize
from .anime_names import Show, norm, parse_watchlist
from .anime_tmdb import live_bounds, shanghai_today

_FORM_NOTE = (
    "先保持「只预览」。115 Cookie 含 UID= 时，本轮离线、整理和摊平都走这条 Cookie。"
    "留空或没有 UID= 时使用存储 ID 对应的已登录 115，默认 u115。"
    "strm 目录填这台 Emby 媒体库正在扫描的路径。"
    "播放地址走 MoviePilot 上已启用的 115网盘STRM助手跳转，填外网域名，不能写 127.0.0.1。"
    "摊平即使关了预览，也要另开「摊平允许落盘」才会移动。"
)


class Anime115(_PluginBase):
    """动漫 115。"""

    plugin_name = "动漫 115"
    plugin_desc = "从动漫花园补连载磁力，用已登录的 115 离线，并在安全时摊平整包。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.11"
    plugin_author = "local"
    author_url = "https://github.com/wyh137815840/mp-animate"
    plugin_config_prefix = "anime115_"
    plugin_order = 21
    auth_level = 1

    _enabled = False
    _onlyonce = False
    _notify = False
    _dry_run = True
    _flatten_apply = False
    _storage = "u115"
    _daily_cron = ""
    _organize_cron = ""
    _flatten_cron = ""
    _offline_root = "/离线下载/动漫"
    _mount_prefix = "/115open"
    _library_root = ""
    _flatten_roots = ""
    _watchlist = ""
    _fansub_prefer = ""
    _resolution = "1080"
    _require_sub = True
    _allow_batch = False
    _lookback_hours = 36
    _min_episode_mb = 80
    _max_episode_gb = 8
    _offline_gap_seconds = 20
    _max_submit_per_run = 10
    _move_gap_seconds = 2
    _flatten_max_moves = 30
    _tmdb_api_key = ""
    _cookie = ""
    _strm_root = ""
    _play_base = PLAY_BASE

    def init_plugin(self, config: dict = None):
        """读配置。勾了立即运行就跑一次每日任务。"""
        if not hasattr(self, "_lock"):
            self._lock = threading.Lock()
        previous = getattr(self, "_cancel", None)
        if previous is not None:
            previous.set()
        self._cancel = threading.Event()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._notify = bool(config.get("notify"))
        self._dry_run = bool(config.get("dry_run", True))
        self._flatten_apply = bool(config.get("flatten_apply"))
        self._storage = str(config.get("storage") or "u115").strip() or "u115"
        self._daily_cron = str(config.get("daily_cron") or "").strip()
        self._organize_cron = str(config.get("organize_cron") or "").strip()
        self._flatten_cron = str(config.get("flatten_cron") or "").strip()
        self._offline_root = offline_directory(config)
        self._cookie = usable_cookie(str(config.get("cookie") or ""))
        self._strm_root = str(config.get("strm_root") or "").strip()
        self._play_base = str(config.get("play_base") or PLAY_BASE).strip() or PLAY_BASE
        self._mount_prefix = str(config.get("mount_prefix") or "/115open").strip()
        self._library_root = str(config.get("library_root") or "").strip()
        self._flatten_roots = str(config.get("flatten_roots") or "")
        self._watchlist = str(config.get("watchlist") or "")
        self._fansub_prefer = str(config.get("fansub_prefer") or "")
        self._resolution = str(config.get("resolution") or "1080").strip() or "1080"
        self._require_sub = bool(config.get("require_sub", True))
        self._allow_batch = bool(config.get("allow_batch"))
        self._lookback_hours = _as_int(config.get("lookback_hours"), 36)
        self._min_episode_mb = _as_int(config.get("min_episode_mb"), 80)
        self._max_episode_gb = _as_int(config.get("max_episode_gb"), 8)
        self._offline_gap_seconds = _as_int(config.get("offline_gap_seconds"), 20)
        self._max_submit_per_run = _as_int(config.get("max_submit_per_run"), 10)
        self._move_gap_seconds = _as_int(config.get("move_gap_seconds"), 2)
        self._flatten_max_moves = _as_int(config.get("flatten_max_moves"), 30)
        self._tmdb_api_key = str(config.get("tmdb_api_key") or "").strip()
        if self._onlyonce:
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config)
            if self._enabled:
                threading.Thread(target=self.run_daily, name="anime115-once", daemon=True).start()

    def get_state(self) -> bool:
        """插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """没有远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """手动运行和查看最近结果。"""
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "运行任务",
                "description": "job=daily、organize 或 flatten",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "最近一次结果",
                "description": "返回最近一次任务摘要",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """按填写的 cron 注册任务。空的或无效的不注册。"""
        if not self.get_state():
            return []
        from apscheduler.triggers.cron import CronTrigger

        services = []
        for service_id, name, cron, func in (
            ("Anime115", "动漫每日补磁力", self._daily_cron, self.run_daily),
            ("Anime115.Organize", "动漫离线整理", self._organize_cron, self.run_organize),
            ("Anime115.Flatten", "动漫整包摊平", self._flatten_cron, self.run_flatten),
        ):
            if not cron:
                continue
            try:
                trigger = CronTrigger.from_crontab(cron)
            except Exception as exc:
                logger.warning(f"动漫 115 的定时表达式无效：{cron} ({exc})")
                continue
            services.append({
                "id": service_id,
                "name": name,
                "trigger": trigger,
                "func": func,
                "kwargs": {},
            })
        return services

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页。默认只预览。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": _FORM_NOTE,
                            "class": "mb-4",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _switch(3, "enabled", "启用插件"),
                            _switch(3, "onlyonce", "保存后立即跑一次每日任务"),
                            _switch(3, "dry_run", "只预览，不提交不移动"),
                            _switch(3, "flatten_apply", "摊平允许落盘"),
                            _switch(3, "notify", "发送通知"),
                            _switch(3, "require_sub", "不要生肉"),
                            _switch(3, "allow_batch", "允许合集"),
                            _field(4, "daily_cron", "每日补磁力", "15 10 * * *，留空则不定时"),
                            _field(4, "organize_cron", "离线整理", "*/30 * * * *，留空则不定时"),
                            _field(4, "flatten_cron", "整包摊平", "15 */2 * * *，留空则不定时"),
                            _field(6, "storage", "存储", "MoviePilot 里的 115 存储 ID，默认 u115"),
                            _area(
                                "cookie",
                                "115 Cookie",
                                "和 115网盘订阅助手一样，填写 UID=...; CID=...; SEID=...; KID=...",
                            ),
                            _field(12, "offline_dir", "离线下载目录", "默认 /离线下载/动漫，日期子目录自动加"),
                            _field(6, "strm_root", "strm 目录", "/media/动漫，这台 Emby 媒体库正在扫描的路径"),
                            _field(12, "play_base", "播放地址模板", PLAY_BASE),
                            _field(4, "resolution", "分辨率", "1080"),
                            _field(4, "lookback_hours", "回溯小时", "36"),
                            _field(6, "mount_prefix", "挂载前缀", "/115open，只用来剥路径"),
                            _field(6, "library_root", "库根", "/动漫"),
                            _field(6, "fansub_prefer", "优先字幕组", "多个用逗号"),
                            _area("watchlist", "追更，每行一部", "剧名 | 别名 | group=字幕组 | tmdb_id=编号 | 连载中"),
                            _field(6, "tmdb_api_key", "TMDB API Key", "v3 密钥，留空则用 MoviePilot 的 TMDB_API_KEY", secret=True),
                            _area("flatten_roots", "摊平目录，每行一个", "/离线下载/动漫"),
                            _field(3, "min_episode_mb", "最小体积(MB)", "80"),
                            _field(3, "max_episode_gb", "最大体积(GB)", "8"),
                            _field(3, "max_submit_per_run", "每次最多提交", "10"),
                            _field(3, "flatten_max_moves", "每次最多摊平", "30"),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "dry_run": True,
            "flatten_apply": False,
            "require_sub": True,
            "allow_batch": False,
            "daily_cron": "",
            "organize_cron": "",
            "flatten_cron": "",
            "storage": "u115",
            "cookie": "",
            "offline_dir": "/离线下载/动漫",
            "strm_root": "",
            "play_base": PLAY_BASE,
            "resolution": "1080",
            "lookback_hours": 36,
            "mount_prefix": "/115open",
            "library_root": "",
            "fansub_prefer": "",
            "watchlist": "",
            "tmdb_api_key": "",
            "flatten_roots": "",
            "min_episode_mb": 80,
            "max_episode_gb": 8,
            "max_submit_per_run": 10,
            "flatten_max_moves": 30,
        }

    def get_page(self) -> List[dict]:
        """详情页。开头两行是进度和状态，下面是本轮条目。"""
        text = self._overview_text()
        detail = self._page_text()
        if detail:
            text = text + "\n" + detail
        return [{
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "text": text,
                "style": "white-space: pre-line;",
            },
        }]

    def stop_service(self):
        """让正在跑的循环在下一轮停下。"""
        cancel = getattr(self, "_cancel", None)
        if cancel is not None:
            cancel.set()

    def api_run(self, job: str = "daily", **kwargs) -> dict:
        """按 job 启动任务。"""
        if not self._enabled:
            return {"ok": False, "error": "插件未启用"}
        name = str(kwargs.get("job") or job or "daily")
        target = {"daily": self.run_daily, "organize": self.run_organize, "flatten": self.run_flatten}.get(name)
        if target is None:
            return {"ok": False, "error": "未知任务"}
        threading.Thread(target=target, name=f"anime115-{name}", daemon=True).start()
        return {"ok": True, "message": "已开始", "job": name}

    def api_status(self) -> dict:
        """最近一次摘要，带上和详情页一样的进度、状态两行。"""
        payload = dict(self.get_data("last_run") or {})
        overview = self._progress_snapshot()
        if overview:
            payload["progress"] = overview.get("progress")
            payload["state"] = overview.get("state")
        if not payload:
            return {"ok": False, "error": "还没有运行记录", "state": format_state(None), "progress": format_progress(None)}
        return payload

    def run_daily(self) -> dict:
        """搜动漫花园，预览或提交离线。"""
        return self._guard("daily", self._daily)

    def run_organize(self) -> dict:
        """把已完成的离线任务搬进库目录。"""
        return self._guard("organize", self._organize)

    def run_flatten(self) -> dict:
        """给整包分类，满足安全门才移动。"""
        return self._guard("flatten", self._flatten)

    def _guard(self, kind: str, func) -> dict:
        if not hasattr(self, "_lock"):
            self._lock = threading.Lock()
        if not self._lock.acquire(blocking=False):
            logger.info("动漫 115 已有任务在运行")
            return {"ok": False, "error": "已有任务在运行"}
        try:
            payload = func()
            payload["job"] = kind
            payload["finished_at"] = _now()
            self._report({
                "job": kind,
                "phase": "done" if payload.get("ok") else "failed",
                "dry_run": bool(payload.get("dry_run", self._dry_run)),
            })
            payload["progress"] = self._progress.get("progress")
            payload["state"] = self._progress.get("state")
            self.save_data("last_run", payload)
            self._push_history(payload)
            self._announce(payload)
            logger.info(f"动漫 115 {kind} 结束：{payload.get('error') or '完成'}")
            return payload
        except Exception as exc:
            logger.error(f"动漫 115 {kind} 失败：{exc}", exc_info=True)
            self._report({"job": kind, "phase": "failed", "dry_run": self._dry_run, "result": "失败"})
            payload = {
                "ok": False,
                "job": kind,
                "error": str(exc),
                "finished_at": _now(),
                "progress": self._progress.get("progress"),
                "state": self._progress.get("state"),
            }
            self.save_data("last_run", payload)
            return payload
        finally:
            self._lock.release()

    def _daily(self) -> dict:
        shows = parse_watchlist(self._watchlist)
        seen = self.get_data("seen") or {}
        tasks = list(self.get_data("tasks") or [])
        moment = datetime.now().astimezone()
        gap = {"done": False}
        config = self._config()
        config.search.bounds_of = live_bounds(self._tmdb_api_key, shanghai_today(moment))
        result = run_daily(
            shows,
            self._session(),
            lambda show: _entries_for(show, self._stopped, gap),
            seen,
            moment,
            config,
            report=self._report,
        )
        if not result.get("dry_run") and result.get("ok"):
            self.save_data("seen", seen)
            for row in result.get("submitted") or []:
                tasks.append(new_task(row, result.get("inbox") or "", moment))
            self.save_data("tasks", tasks)
        return result

    def _organize(self) -> dict:
        tasks = list(self.get_data("tasks") or [])
        result = run_organize(tasks, self._session(), self._config(), report=self._report)
        if not result.get("dry_run") and result.get("ok"):
            self.save_data("tasks", tasks)
        return result

    def _flatten(self) -> dict:
        return run_flatten(self._session(), self._config(), report=self._report)

    def _config(self) -> JobConfig:
        prefer = [item.strip() for item in self._fansub_prefer.split(",") if item.strip()]
        roots = []
        for line in self._flatten_roots.splitlines():
            text = line.strip()
            if text and not text.startswith("#"):
                roots.append(text)
        return JobConfig(
            dry_run=self._dry_run,
            storage=self._storage,
            offline_root=self._offline_root,
            mount_prefix=self._mount_prefix,
            library_root=self._library_root,
            flatten_roots=roots,
            flatten_apply=self._flatten_apply,
            flatten_max_moves=self._flatten_max_moves,
            search=SearchOptions(
                resolution=self._resolution,
                require_sub=self._require_sub,
                allow_batch=self._allow_batch,
                lookback_hours=self._lookback_hours,
                min_episode_mb=self._min_episode_mb,
                max_episode_gb=self._max_episode_gb,
                fansub_prefer=prefer,
            ),
            offline_gap_seconds=self._offline_gap_seconds,
            max_submit_per_run=self._max_submit_per_run,
            move_gap_seconds=self._move_gap_seconds,
            min_episode_mb=self._min_episode_mb,
        )

    def _report(self, patch: dict) -> None:
        """刷新详情页开头的进度和状态。"""
        self._progress = advance(getattr(self, "_progress", None), patch)
        try:
            self.save_data("progress", self._progress)
        except Exception:
            return

    def _progress_snapshot(self) -> dict:
        current = getattr(self, "_progress", None)
        if current:
            return current
        try:
            saved = self.get_data("progress") or {}
        except Exception:
            saved = {}
        return saved if isinstance(saved, dict) else {}

    def _overview_text(self) -> str:
        snap = self._progress_snapshot()
        return f"{snap.get('progress') or format_progress(snap)}\n{snap.get('state') or format_state(snap)}"

    def _cookie_value(self) -> str:
        """填了 UID= 才算 Cookie。留空则调用方改用已登录存储。"""
        return usable_cookie(getattr(self, "_cookie", ""))

    def _session(self):
        """一轮只用一种 115 凭据。有 UID= 走 Cookie，否则走已登录存储。"""
        cookie = self._cookie_value()
        if cookie:
            logger.info("动漫 115 本轮使用 115 Cookie")
            return CookieSession(cookie, self._cancel)
        logger.info(f"动漫 115 本轮使用已登录存储 {self._storage}")
        return _Mp115(self._storage, self._cancel)

    def _stopped(self) -> bool:
        cancel = getattr(self, "_cancel", None)
        return cancel is not None and cancel.is_set()

    def _push_history(self, payload: dict) -> None:
        try:
            history = list(self.get_data("history") or [])
        except Exception:
            history = []
        history.insert(0, {
            "job": payload.get("job"),
            "finished_at": payload.get("finished_at"),
            "ok": payload.get("ok"),
            "dry_run": payload.get("dry_run"),
            "error": payload.get("error") or "",
            "submitted": len(payload.get("submitted") or []),
            "picks": len(payload.get("picks") or []),
            "moved": payload.get("moved", 0),
            "counts": payload.get("counts") or {},
        })
        self.save_data("history", history[:50])

    def _announce(self, payload: dict) -> None:
        if not self._notify or not payload.get("ok"):
            return
        if payload.get("dry_run") and not (payload.get("picks") or payload.get("proposals") or payload.get("moves")):
            return
        if payload.get("error"):
            return
        try:
            from app.schemas.types import NotificationType

            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"【动漫 115】{payload.get('job')}",
                text=self._page_text(),
            )
        except Exception as exc:
            logger.info(f"动漫 115 通知失败：{exc}")

    def _page_text(self) -> str:
        try:
            data = self.get_data("last_run") or {}
        except Exception:
            data = {}
        if not data:
            return "还没有运行记录。建议先开着「只预览」。"
        lines = [
            f"任务：{data.get('job') or ''}",
            f"时间：{data.get('finished_at') or ''}",
            f"模式：{'预览' if data.get('dry_run') else '正式'}",
        ]
        if data.get("error"):
            lines.append(f"错误：{data['error']}")
        if data.get("inbox"):
            lines.append(f"目录：{data['inbox']}")
        if getattr(self, "_strm_root", ""):
            lines.append(f"strm：{self._strm_root}")
        lines.append("115：Cookie" if self._cookie_value() else "115：存储 " + self._storage)
        counts = data.get("counts") or {}
        if counts:
            lines.append(
                "摊平 安全 {ok_eligible_non_collision}，碰撞 {collision}，不确定 {uncertain}，跳过 {skip}，已移动 {moved}".format(
                    moved=data.get("moved", 0),
                    **counts,
                )
            )
        for item in (data.get("picks") or [])[:20]:
            lines.append(f"选中  {item.get('show')}  S{item.get('season'):02d}E{item.get('episode'):02d}  {item.get('title')}")
        for item in (data.get("notes") or [])[:20]:
            lines.append(f"跳过  {item.get('show')}  {item.get('reason')}  {item.get('title')}")
        for item in (data.get("moves") or [])[:20]:
            lines.append(f"整理  {item.get('action')}  {item.get('reason')}  {item.get('dest_dir') or ''}")
        return "\n".join(lines)


class _Mp115:
    """MoviePilot 已登录的 115。不另建客户端。"""

    def __init__(self, storage: str, cancel):
        self.storage = storage or "u115"
        self.cancel = cancel

    def check_login(self) -> bool:
        from app.modules.filemanager.storages.u115 import U115Pan

        return bool(U115Pan().check())

    def sleep(self, seconds: int) -> None:
        if seconds > 0 and not self.cancel.wait(seconds):
            return

    def ensure_dir(self, path: str):
        return StorageChain().get_folder(self.storage, Path(norm(path)))

    def list_dir(self, path: str) -> List[Node]:
        chain = StorageChain()
        item = chain.get_file_item(self.storage, Path(norm(path)))
        if item is None:
            return []
        children = chain.list_files(item, recursion=False) or []
        nodes = []
        for child in children:
            nodes.append(Node(
                path=norm(str(child.path or "")),
                name=child.name or "",
                is_dir=child.type == "dir",
                size=int(child.size or 0),
                raw=child,
            ))
        return nodes

    def move(self, node: Node, dest_dir: str, new_name: str) -> bool:
        from app.modules.filemanager.storages.u115 import U115Pan

        item = node.raw
        if item is None:
            return False
        moved = bool(U115Pan().move(item, Path(norm(dest_dir)), new_name))
        if moved:
            return True
        if new_name != node.name:
            return False
        landed = StorageChain().get_file_item(self.storage, Path(norm(dest_dir)) / new_name)
        return landed is not None

    def delete_dir(self, path: str) -> bool:
        chain = StorageChain()
        item = chain.get_file_item(self.storage, Path(norm(path)))
        if item is None or item.type != "dir":
            return False
        return bool(chain.delete_file(item))

    def add_offline(self, magnet: str, fileid: str):
        from app.modules.filemanager.storages.u115 import U115Pan

        target = int(fileid) if str(fileid).isdigit() else fileid
        return U115Pan()._request_api(
            "POST",
            "/open/offline/add_task_urls",
            data={"urls": magnet, "wp_path_id": target},
        )

    def list_offline(self, page: int = 1):
        from app.modules.filemanager.storages.u115 import U115Pan

        return U115Pan()._request_api(
            "GET",
            "/open/offline/get_task_list",
            params={"page": page},
        )

    def quota(self):
        from app.modules.filemanager.storages.u115 import U115Pan

        return U115Pan()._request_api("GET", "/open/offline/get_quota_info")


def _entries_for(show: Show, stopped, gap: dict) -> list:
    """每部剧查一次 RSS。体积还没有时再查列表页。相邻请求至少隔 2 秒。"""
    if stopped():
        return []
    if gap.get("done"):
        time.sleep(2)
    gap["done"] = True
    torrents = parse_rss(fetch_rss(show.keyword))
    if stopped() or all(item.size for item in torrents):
        return torrents
    time.sleep(2)
    if stopped():
        return torrents
    try:
        sizes = parse_list_sizes(fetch_list(show.keyword))
    except Exception as exc:
        logger.info(f"动漫花园列表页体积没补上: {exc}")
        return torrents
    return merge_sizes(torrents, sizes)


def _switch(cols: int, model: str, label: str) -> dict:
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": cols},
        "content": [{
            "component": "VSwitch",
            "props": {"model": model, "label": label},
        }],
    }


def _field(cols: int, model: str, label: str, placeholder: str, secret: bool = False) -> dict:
    props = {"model": model, "label": label, "placeholder": placeholder}
    if secret:
        props["type"] = "password"
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": cols},
        "content": [{
            "component": "VTextField",
            "props": props,
        }],
    }


def _area(model: str, label: str, placeholder: str) -> dict:
    return {
        "component": "VCol",
        "props": {"cols": 12},
        "content": [{
            "component": "VTextarea",
            "props": {"model": model, "label": label, "placeholder": placeholder, "rows": 4},
        }],
    }


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
