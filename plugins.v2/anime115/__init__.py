"""从动漫花园补连载磁力，并用 MoviePilot 已登录的 115 离线、整理、摊平。"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.chain.storage import StorageChain
from app.log import logger
from app.plugins import _PluginBase

from .anime_config import PLAY_BASE, PLAY_BASE_LAN, offline_directory, organize_schedule, scheduled_jobs, usable_cookie
from .anime_emby import refresh_library
from .anime_playurl import (
    DEFAULT_LAN_CIDRS,
    DEFAULT_PUBLIC_HOSTS,
    PlayUrlError,
    decide_location,
    nginx_snippet,
)
from .anime_pan import CookieSession
from .anime_progress import advance, format_progress, format_state
from .anime_dmhy import SearchOptions, fetch_list, fetch_rss, merge_sizes, parse_list_sizes, parse_rss
from .anime_flatten import Node
from .anime_jobs import (
    JobConfig,
    JobStopped,
    new_task,
    run_daily,
    run_flatten,
    run_organize,
    run_split,
    run_strm as write_library_strm,
)
from .anime_names import Show, _fold_name, norm, parse_watchlist
from .anime_subscribe import load_mp_subscriptions, shows_from_subscriptions
from .anime_tmdb import live_bounds, shanghai_today

# 剧名搜到的候选少于这个数时，用别名再搜一遍补齐。
_ALIAS_SEARCH_THRESHOLD = 3

_FORM_NOTE = (
    "先保持「只预览」。115 Cookie 含 UID= 时，本轮离线、整理和摊平都走这条 Cookie。"
    "留空或没有 UID= 时使用存储 ID 对应的已登录 115，默认 u115。"
    "strm 目录填这台 Emby 媒体库正在扫描的路径。整理成功后按季目录写 strm，不写 nfo。"
    "已经在库里的文件用 job=strm 重新生成 strm，不移动。strm 默认写内网播放地址，局域网不再走外网域名。"
    "外网播放地址留给公网 Emby 反代。两个地址都不能写 127.0.0.1。115 直链仍由 115网盘STRM助手签发。"
    "每日默认从 MoviePilot 订阅里只挑动漫去花园搜。电视剧和电影不搜。离线提交走已登录的存储，不使用 P115Client。"
    "打开「完成后刷新 Emby」时，整理或 strm 真正写盘后只刷新「Emby 媒体库名」这一库，找不到库就不刷，预览不刷。"
    "「库里已有同集不提交」默认开：选片时先看库根/剧名/季目录里有没有这一集，已有就跳过，不重复下载。"
    "离线整理留空则每 30 分钟自动跑，不用手点。每日和摊平留空则不定时。"
    "摊平即使关了预览，也要另开「摊平允许落盘」才会移动。"
)


class Anime115(_PluginBase):
    """动漫 115。"""

    plugin_name = "动漫 115"
    plugin_desc = "从动漫花园补连载磁力，用已登录的 115 离线，并在安全时摊平整包。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.54"
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
    _subscribe_source = "mp"
    _fansub_prefer = ""
    _resolution = "1080"
    _require_sub = True
    _allow_batch = False
    _lookback_hours = 36
    _min_episode_mb = 80
    _max_episode_gb = 8
    _max_pack_gb = 120
    _subtitle = True
    _offline_gap_seconds = 20
    _max_submit_per_run = 10
    _move_gap_seconds = 2
    _flatten_max_moves = 30
    _tmdb_api_key = ""
    _cookie = ""
    _refresh_emby = False
    _emby_library = "动漫"
    _skip_owned = True
    _strm_root = ""
    _play_base = PLAY_BASE
    _play_base_lan = PLAY_BASE_LAN
    _strm_for = "lan"
    _lan_cidrs = DEFAULT_LAN_CIDRS
    _public_hosts = DEFAULT_PUBLIC_HOSTS

    def init_plugin(self, config: dict = None):
        """读配置。勾了立即运行就跑一次每日任务。"""
        if not hasattr(self, "_lock"):
            self._lock = threading.Lock()
        previous = getattr(self, "_cancel", None)
        if previous is not None:
            previous.set()
            logger.info("动漫 115 重载：已要求上一轮任务结束")
        self._cancel = threading.Event()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._notify = bool(config.get("notify"))
        self._dry_run = bool(config.get("dry_run", True))
        self._flatten_apply = bool(config.get("flatten_apply"))
        self._storage = str(config.get("storage") or "u115").strip() or "u115"
        self._daily_cron = str(config.get("daily_cron") or "").strip()
        self._organize_cron = organize_schedule(config.get("organize_cron"))
        self._flatten_cron = str(config.get("flatten_cron") or "").strip()
        self._offline_root = offline_directory(config)
        self._cookie = usable_cookie(str(config.get("cookie") or ""))
        self._refresh_emby = bool(config.get("refresh_emby"))
        self._emby_library = str(config.get("emby_library") or "动漫").strip() or "动漫"
        self._skip_owned = bool(config.get("skip_owned", True))
        self._strm_root = str(config.get("strm_root") or "").strip()
        self._play_base = str(config.get("play_base") or PLAY_BASE).strip() or PLAY_BASE
        self._play_base_lan = str(config.get("play_base_lan") or PLAY_BASE_LAN).strip() or PLAY_BASE_LAN
        self._strm_for = _strm_mode(config.get("strm_for"))
        self._lan_cidrs = str(config.get("lan_cidrs") or DEFAULT_LAN_CIDRS).strip() or DEFAULT_LAN_CIDRS
        self._public_hosts = str(config.get("public_hosts") or DEFAULT_PUBLIC_HOSTS).strip() or DEFAULT_PUBLIC_HOSTS
        self._mount_prefix = str(config.get("mount_prefix") or "/115open").strip()
        self._library_root = str(config.get("library_root") or "").strip()
        self._flatten_roots = str(config.get("flatten_roots") or "")
        self._watchlist = str(config.get("watchlist") or "")
        self._subscribe_source = _subscribe_source(config.get("subscribe_source"))
        self._fansub_prefer = str(config.get("fansub_prefer") or "")
        self._resolution = str(config.get("resolution") or "1080").strip() or "1080"
        self._require_sub = bool(config.get("require_sub", True))
        self._allow_batch = bool(config.get("allow_batch"))
        self._lookback_hours = _as_int(config.get("lookback_hours"), 36)
        self._min_episode_mb = _as_int(config.get("min_episode_mb"), 80)
        self._max_episode_gb = _as_int(config.get("max_episode_gb"), 8)
        self._max_pack_gb = _as_int(config.get("max_pack_gb"), 120)
        self._subtitle = bool(config.get("subtitle", True))
        self._offline_gap_seconds = _as_int(config.get("offline_gap_seconds"), 20)
        self._max_submit_per_run = _as_int(config.get("max_submit_per_run"), 10)
        self._move_gap_seconds = _as_int(config.get("move_gap_seconds"), 2)
        self._flatten_max_moves = _as_int(config.get("flatten_max_moves"), 30)
        self._tmdb_api_key = str(config.get("tmdb_api_key") or "").strip()
        blank_organize = not str(config.get("organize_cron") or "").strip()
        migrate_organize = bool(config) and blank_organize and not getattr(self, "_organize_migrated", False)
        if migrate_organize:
            config["organize_cron"] = self._organize_cron
            self._organize_migrated = True
        if self._onlyonce:
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config)
            if self._enabled:
                threading.Thread(target=self.run_daily, name="anime115-once", daemon=True).start()
        elif migrate_organize:
            self.update_config(config)

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
                "description": "job=daily、organize、flatten、strm 或 split。strm/split 可带 root=某一剧目录",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "最近一次结果",
                "description": "返回最近一次任务摘要",
            },
            {
                "path": "/redirect_url",
                "endpoint": self.api_redirect,
                "methods": ["GET", "HEAD"],
                "summary": "按来源跳转播放地址",
                "description": "内网请求跳到内网 MoviePilot，外网请求跳到外网域名。",
                "allow_anonymous": True,
            },
            {
                "path": "/play_url",
                "endpoint": self.api_play_url,
                "methods": ["GET"],
                "summary": "查看本次会使用的播放地址",
                "description": "返回 intranet、reason 和 url，不发起 115 请求。",
                "allow_anonymous": True,
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """按 cron 注册任务。整理留空用每 30 分钟。每日和摊平留空不注册。"""
        from apscheduler.triggers.cron import CronTrigger

        funcs = {
            "run_daily": self.run_daily,
            "run_organize": self.run_organize,
            "run_flatten": self.run_flatten,
        }
        services = []
        for spec in scheduled_jobs(self.get_state(), self._daily_cron, self._organize_cron, self._flatten_cron):
            cron = spec["cron"]
            try:
                trigger = CronTrigger.from_crontab(cron)
            except Exception as exc:
                logger.warning(f"动漫 115 的定时表达式无效：{cron} ({exc})")
                continue
            services.append({
                "id": spec["id"],
                "name": spec["name"],
                "trigger": trigger,
                "func": funcs[spec["func"]],
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
                            _switch(3, "refresh_emby", "完成后刷新 Emby"),
                            _switch(3, "skip_owned", "库里已有同集不提交"),
                            _switch(3, "require_sub", "不要生肉"),
                            _switch(3, "allow_batch", "允许合集"),
                            _field(4, "daily_cron", "每日补磁力", "15 10 * * *，留空则不定时"),
                            _field(4, "organize_cron", "离线整理", "留空则每 30 分钟"),
                            _field(4, "flatten_cron", "整包摊平", "15 */2 * * *，留空则不定时"),
                            _field(6, "storage", "存储", "MoviePilot 里的 115 存储 ID，默认 u115"),
                            _area(
                                "cookie",
                                "115 Cookie",
                                "和 115网盘订阅助手一样，填写 UID=...; CID=...; SEID=...; KID=...",
                            ),
                            _field(12, "offline_dir", "离线下载目录", "默认 /离线下载/动漫，日期子目录自动加"),
                            _field(6, "strm_root", "strm 目录", "/media/动漫，这台 Emby 媒体库正在扫描的路径"),
                            _field(6, "emby_library", "Emby 媒体库名", "动漫"),
                            _field(12, "play_base_lan", "内网播放地址", "http://192.168.1.9:3000 或 http://192.168.1.10:8787/redirect?pickcode={pickcode}"),
                            _field(12, "play_base", "外网播放地址", PLAY_BASE),
                            _field(6, "strm_for", "strm 使用的地址", "lan、public 或 redirect"),
                            _field(6, "public_hosts", "外网域名", "mp.yuanhot.top"),
                            _field(12, "lan_cidrs", "内网网段", "192.168.0.0/16,10.0.0.0/8"),
                            _field(4, "resolution", "分辨率", "最低 1080，4K 也收；写成 4k 则只要 4K"),
                            _field(4, "lookback_hours", "回溯小时", "36"),
                            _field(6, "mount_prefix", "挂载前缀", "/115open，只用来剥路径"),
                            _field(6, "library_root", "库根", "/动漫"),
                            _field(6, "fansub_prefer", "优先字幕组", "多个用逗号"),
                            _field(6, "subscribe_source", "订阅来源", "mp=从 MP 订阅读取，watchlist=只读追更框，both=两边"),
                            _area("watchlist", "追更，每行一部", "来源不是 mp 时才用。剧名 | 别名 | group=字幕组 | tmdb_id=编号 | 连载中"),
                            _field(6, "tmdb_api_key", "TMDB API Key", "v3 密钥，留空则用 MoviePilot 的 TMDB_API_KEY", secret=True),
                            _area("flatten_roots", "摊平目录，每行一个", "/离线下载/动漫"),
                            _field(3, "min_episode_mb", "最小体积(MB)", "80"),
                            _field(3, "max_episode_gb", "最大体积(GB)", "8"),
                            _field(3, "max_pack_gb", "整包最大体积(GB)", "120，蓝光大包按单集算体积"),
                            _switch(3, "subtitle", "把字幕下载到 Emby 旁边"),
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
            "refresh_emby": False,
            "skip_owned": True,
            "dry_run": True,
            "flatten_apply": False,
            "require_sub": True,
            "allow_batch": False,
            "daily_cron": "",
            "organize_cron": "*/30 * * * *",
            "flatten_cron": "",
            "storage": "u115",
            "cookie": "",
            "offline_dir": "/离线下载/动漫",
            "strm_root": "",
            "emby_library": "动漫",
            "play_base": PLAY_BASE,
            "play_base_lan": PLAY_BASE_LAN,
            "strm_for": "lan",
            "lan_cidrs": DEFAULT_LAN_CIDRS,
            "public_hosts": DEFAULT_PUBLIC_HOSTS,
            "resolution": "1080",
            "lookback_hours": 36,
            "mount_prefix": "/115open",
            "library_root": "",
            "fansub_prefer": "",
            "subscribe_source": "mp",
            "watchlist": "",
            "tmdb_api_key": "",
            "flatten_roots": "",
            "min_episode_mb": 80,
            "max_episode_gb": 8,
            "max_pack_gb": 120,
            "subtitle": True,
            "max_submit_per_run": 10,
            "flatten_max_moves": 30,
        }

    def get_page(self) -> List[dict]:
        """详情页。开头两行是进度和状态，下面是本轮条目。"""
        text = self._overview_text()
        detail = self._page_text()
        if detail:
            text = text + "\n" + detail
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": text,
                    "style": "white-space: pre-line;",
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "class": "mt-3",
                    "text": self._play_note(),
                    "style": "white-space: pre-line;",
                },
            },
        ]

    def stop_service(self):
        """让正在跑的任务停下来并自己写下结束状态。"""
        cancel = getattr(self, "_cancel", None)
        if cancel is not None:
            cancel.set()
            logger.info("动漫 115 停止：已要求正在运行的任务结束")

    def api_run(self, job: str = "daily", root: str = "") -> dict:
        """按 job 启动任务。strm 的 root 只补那一部剧。"""
        if not self._enabled:
            return {"ok": False, "error": "插件未启用"}
        name = str(job or "daily")
        limit = str(root or "").strip()
        if name == "strm":
            threading.Thread(target=lambda: self.run_strm(limit), name="anime115-strm", daemon=True).start()
            return {"ok": True, "message": "已开始", "job": name}
        if name == "split":
            threading.Thread(target=lambda: self.run_split(limit), name="anime115-split", daemon=True).start()
            return {"ok": True, "message": "已开始", "job": name}
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

    def api_redirect(self, request: Request):
        """Emby 打开跳转地址时，302 到这次请求对应的播放地址。"""
        try:
            decision = self._decision(request)
        except PlayUrlError as exc:
            return Response(content=str(exc), status_code=409)
        logger.info(
            "动漫 115 播放跳转：%s %s -> %s",
            "内网" if decision.intranet else "外网",
            decision.reason,
            decision.url.split("?", 1)[0],
        )
        return RedirectResponse(url=decision.url, status_code=302)

    def api_play_url(self, request: Request):
        """返回本次判断，方便核对内网是否还会带上外网域名。"""
        try:
            decision = self._decision(request)
        except PlayUrlError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return {
            "ok": True,
            "intranet": decision.intranet,
            "reason": decision.reason,
            "url": decision.url,
        }

    def _decision(self, request: Request):
        peer = request.client.host if request.client else ""
        return decide_location(
            peer_ip=peer,
            host=request.headers.get("host", ""),
            headers=request.headers,
            public_template=self._play_base,
            lan_template=self._play_base_lan,
            pickcode=request.query_params.get("pickcode") or request.query_params.get("pc") or "",
            filename=request.query_params.get("file_name") or request.query_params.get("filename") or "",
            lan_cidrs=self._lan_cidrs,
            public_hosts=self._public_hosts,
            request_path=request.url.path,
        )

    def _play_note(self) -> str:
        """详情页上的内网播放说明和公网反代片段。"""
        try:
            snippet = nginx_snippet(self._play_base, self._play_base_lan)
        except PlayUrlError as exc:
            snippet = str(exc)
        return (
            f"strm 使用的地址：{self._strm_for}。默认 lan，文件里不写外网域名。\n"
            f"内网播放地址：{self._play_base_lan}\n"
            f"外网播放地址：{self._play_base}\n"
            "已有动漫 strm 要重新生成：POST /api/v1/plugin/Anime115/run?job=strm\n"
            "重载本插件即可，不必重启 MoviePilot。\n"
            "公网 Emby 反代片段：\n"
            f"{snippet}"
        )

    def run_daily(self) -> dict:
        """搜动漫花园，预览或提交离线。"""
        return self._guard("daily", self._daily)

    def run_organize(self) -> dict:
        """把已落地的离线任务搬进库目录，并在成功后写 strm。"""
        return self._guard("organize", self._organize)

    def run_flatten(self) -> dict:
        """给整包分类，满足安全门才移动。"""
        return self._guard("flatten", self._flatten)

    def run_strm(self, root: str = "") -> dict:
        """给库里已有的视频补 strm，不移动。"""
        self._strm_limit = root
        return self._guard("strm", self._strm)

    def run_split(self, root: str = "") -> dict:
        """把库里放错位置的整包拆成一集一集。"""
        self._split_limit = root
        return self._guard("split", self._split)

    def _guard(self, kind: str, func) -> dict:
        if not hasattr(self, "_lock"):
            self._lock = threading.Lock()
        if not self._lock.acquire(blocking=False):
            logger.info("动漫 115 已有任务在运行")
            return {"ok": False, "error": "已有任务在运行"}
        try:
            logger.info(f"动漫 115 {kind} 开始")
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
            try:
                self.save_data("last_run", payload)
                self._push_history(payload)
            except Exception as exc:
                # 记录失败不能把已经做完的活判成失败
                logger.warning(f"动漫 115 {kind} 结果落库失败：{exc}")
            self._announce(payload)
            detail = payload.get("error") or "完成"
            note = payload.get("strm_error") or ""
            if note:
                detail = f"{detail}，{note}"
            logger.info(f"动漫 115 {kind} 结束：{detail}")
            return payload
        except JobStopped:
            logger.info(f"动漫 115 {kind} 结束：已停止")
            self._report({"job": kind, "phase": "failed", "dry_run": self._dry_run, "result": "已停止"})
            payload = {
                "ok": False,
                "job": kind,
                "error": "已停止",
                "finished_at": _now(),
                "progress": self._progress.get("progress"),
                "state": self._progress.get("state"),
            }
            self.save_data("last_run", payload)
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
        prepared = self._prepare_shows()
        if prepared.get("error"):
            return {
                "ok": False,
                "error": prepared["error"],
                "dry_run": self._dry_run,
                "picks": [],
                "notes": prepared.get("skipped") or [],
                "submitted": [],
                "subscribes": [],
                "subscribe_source": prepared.get("source") or self._subscribe_source,
                "anime_count": 0,
            }
        shows = prepared["shows"]
        skipped = prepared["skipped"]
        if not shows:
            return {
                "ok": bool(skipped),
                "error": "" if skipped else "没有动漫订阅",
                "dry_run": self._dry_run,
                "picks": [],
                "notes": skipped,
                "submitted": [],
                "subscribes": [],
                "subscribe_source": prepared["source"],
                "anime_count": 0,
            }
        seen = self.get_data("seen") or {}
        tasks = list(self.get_data("tasks") or [])
        moment = datetime.now().astimezone()
        gap = {"done": False}
        config = self._config()
        config.search.bounds_of = live_bounds(self._tmdb_api_key, shanghai_today(moment))
        session = self._mp_session()
        cancel = getattr(session, "cancel", None)
        cookie_session = self._owned_session()
        result = run_daily(
            shows,
            session,
            lambda show: _entries_for(show, cancel, gap),
            seen,
            moment,
            config,
            report=self._report,
            owned_session=cookie_session,
            fallback_session=cookie_session,
        )
        self._save_alias_cache()
        result["notes"] = skipped + (result.get("notes") or [])
        result["subscribes"] = [
            {"show": show.keyword, "tmdb_id": show.tmdb_id, "season": show.season}
            for show in shows
        ]
        result["subscribe_source"] = prepared["source"]
        result["anime_count"] = len(shows)
        persist = result.pop("persist", False)
        if not result.get("dry_run") and (result.get("ok") or persist):
            self.save_data("seen", seen)
            for row in result.get("submitted") or []:
                tasks.append(new_task(row, result.get("inbox") or "", moment))
            self.save_data("tasks", tasks)
        return result

    def _prepare_shows(self) -> dict:
        """默认只读 MoviePilot 订阅，并且先滤成动漫。"""
        source = _subscribe_source(self._subscribe_source)
        skipped = []
        mp_shows = []
        if source in {"mp", "both"}:
            try:
                rows = load_mp_subscriptions()
            except Exception as exc:
                return {"error": f"读不到 MoviePilot 订阅：{exc}", "source": source, "skipped": []}
            mp_shows, skipped = shows_from_subscriptions(
                rows,
                genres_of=self._genre_lookup(),
                aliases_of=self._alias_lookup(),
            )
        manual = parse_watchlist(self._watchlist) if source in {"watchlist", "both"} else []
        if source == "watchlist":
            shows = manual
        elif source == "both":
            shows = _merge_shows(mp_shows, manual)
        else:
            shows = mp_shows
        return {"shows": shows, "skipped": skipped, "source": source}

    def _genre_lookup(self):
        """TMDB 电视剧没有分类时，用类型 16 判断是不是动画。查不到就不搜。"""
        from .anime_tmdb import configured_tmdb_key, tv_genre_ids

        key = configured_tmdb_key(self._tmdb_api_key)
        cache: dict[int, list[int] | None] = {}

        def lookup(tmdb_id: int):
            if not key:
                return None
            if tmdb_id not in cache:
                cache[tmdb_id] = tv_genre_ids(tmdb_id, key)
            return cache[tmdb_id]

        return lookup

    def _alias_lookup(self):
        """TMDB 别名，用来分辨同名别的剧。取不到就当没有别名。

        一天取一次，缓存在插件数据里。
        """
        from .anime_tmdb import alt_titles, configured_tmdb_key

        key = configured_tmdb_key(self._tmdb_api_key)
        if not key:
            return lambda _tmdb_id: []
        cache = self.get_data("tmdb_aliases") or {}
        if not isinstance(cache, dict):
            cache = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if cache.get("day") != today:
            cache = {"day": today, "titles": {}}
        titles = cache.get("titles") or {}

        def lookup(show):
            if show is None or not getattr(show, "tmdb_id", None):
                return []
            key_text = str(show.tmdb_id)
            if key_text not in titles:
                try:
                    found = alt_titles(int(show.tmdb_id), key)
                except Exception as exc:
                    logger.info(f"动漫 115 取 TMDB 别名失败 {show.tmdb_id}：{exc}")
                    found = []
                # 取失败或取到空时不要写进缓存，免得一次网络抖动毒化一整天
                if found:
                    titles[key_text] = found
                else:
                    titles.pop(key_text, None)
            return titles.get(key_text) or []

        self._alias_cache = (cache, titles)
        return lookup

    def _save_alias_cache(self) -> None:
        """把今天取到的别名存下来。"""
        cached = getattr(self, "_alias_cache", None)
        if not cached:
            return
        cache, titles = cached
        try:
            self.save_data("tmdb_aliases", {"day": cache.get("day"), "titles": titles})
        except Exception:
            return

    def _mp_session(self):
        """每日离线走 MoviePilot 已登录的 115，不建 P115Client。"""
        logger.info(f"动漫 115 每日离线使用已登录存储 {self._storage}")
        return _Mp115(self._storage, self._cancel)

    def _organize(self) -> dict:
        tasks = list(self.get_data("tasks") or [])
        seen = self.get_data("seen") or {}
        if not isinstance(seen, dict):
            seen = {}
        result = run_organize(tasks, self._session(), self._config(), report=self._report, seen=seen)
        changed = result.pop("seen_changed", False)
        persist = result.pop("persist", False)
        if not result.get("dry_run") and (result.get("ok") or persist):
            self.save_data("tasks", tasks)
            if changed:
                self.save_data("seen", seen)
        moves = result.get("moves") or []
        wrote = any(
            item.get("action") in {"move", "strm"} and not item.get("error") and not item.get("strm_error")
            for item in moves
            if isinstance(item, dict)
        )
        self._refresh_emby_result(result, wrote, bool(moves))
        return result

    def _flatten(self) -> dict:
        return run_flatten(self._session(), self._config(), report=self._report)

    def _strm(self) -> dict:
        limit = getattr(self, "_strm_limit", "") or ""
        result = write_library_strm(self._session(), self._config(), limit, report=self._report)
        wrote = not result.get("dry_run") and int(result.get("written") or 0) > 0
        self._refresh_emby_result(result, wrote, int(result.get("written") or 0) > 0)
        return result

    def _split(self) -> dict:
        limit = getattr(self, "_split_limit", "") or ""
        result = run_split(self._session(), self._config(), limit, report=self._report)
        moved = int(result.get("moved") or 0)
        if moved and not result.get("dry_run"):
            self._refresh_emby_result(result, wrote=True, planning=False)
        return result

    def _refresh_emby_result(self, payload: dict, wrote: bool, planning: bool) -> None:
        """整理或 strm 真正写盘后刷新指定 Emby 媒体库。失败只记字符串，不影响 ok。"""
        if payload.get("dry_run"):
            if planning:
                payload["emby"] = "预览不刷新 Emby"
            return
        if not self._refresh_emby or not wrote:
            return
        try:
            payload["emby"] = refresh_library(self._emby_library)
        except Exception as exc:
            payload["emby"] = f"刷新 Emby 失败：{exc}"
        logger.info(f"动漫 115 {payload['emby']}")

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
                max_pack_gb=self._max_pack_gb,
                fansub_prefer=prefer,
            ),
            offline_gap_seconds=self._offline_gap_seconds,
            max_submit_per_run=self._max_submit_per_run,
            move_gap_seconds=self._move_gap_seconds,
            min_episode_mb=self._min_episode_mb,
            strm_root=self._strm_root,
            play_base=self._play_base,
            play_base_lan=self._play_base_lan,
            strm_for=self._strm_for,
            public_hosts=self._public_hosts,
            skip_owned=self._skip_owned,
            subtitle_enabled=self._subtitle,
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

    def _owned_session(self):
        """查「库里已有同集」用的会话。

        开放平台的列表接口经常被 115 风控卡住，Cookie 通道不受影响，
        所以有 Cookie 时用 Cookie 查，离线提交仍然走已登录存储。
        """
        if not self._cookie_value():
            return None
        return CookieSession(self._cookie_value(), self._cancel)

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
            "emby": payload.get("emby") or "",
        })
        self.save_data("history", history[:50])

    def _announce(self, payload: dict) -> None:
        """成功和失败都发，预览且没有任何动作时保持安静。"""
        if not self._notify:
            return
        if payload.get("dry_run") and not (
            payload.get("picks")
            or payload.get("proposals")
            or payload.get("moves")
            or payload.get("items")
        ):
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
        if data.get("subscribe_source"):
            lines.append(f"订阅：{data.get('subscribe_source')}，动漫 {int(data.get('anime_count') or 0)}")
        if data.get("inbox"):
            lines.append(f"目录：{data['inbox']}")
        if getattr(self, "_strm_root", ""):
            lines.append(f"strm：{self._strm_root}")
        if getattr(self, "_refresh_emby", False):
            lines.append(f"Emby：完成后刷新「{self._emby_library}」")
        if data.get("emby"):
            lines.append(f"Emby：{data['emby']}")
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
            lines.append(
                f"整理  {item.get('action')}  {item.get('error') or item.get('reason')}  {item.get('pack') or ''}  {item.get('dest_dir') or ''}  {item.get('strm') or item.get('strm_error') or ''}".rstrip()
            )
        for item in (data.get("items") or [])[:20]:
            if data.get("job") != "strm":
                continue
            lines.append(f"strm  {item.get('action')}  {item.get('reason')}  {item.get('path') or ''}")
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
        # 宿主 move 在移动成功后还会回查目录并做同名 rename，任一步失败就返回 False。
        # 115 列表有延迟，稍等一秒再核一次目标目录，避免把已经搬走的任务记成失败。
        try:
            self.sleep(1)
            return any(child.name == new_name for child in self.list_dir(dest_dir))
        except Exception as exc:
            logger.info(f"动漫 115 移动失败后复查目标目录出错：{exc}")
            return False

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


def _short_keywords(show: Show) -> list[str]:
    """订阅名里截出来的短形式。

    MoviePilot 的订阅名常带一长串副标题（《才女的侍从 在满是高岭之花的…》），
    动漫花园那边按短名收录，截一段去搜更容易命中。
    """
    keyword = (getattr(show, "keyword", "") or "").strip()
    if not keyword:
        return []
    heads = []
    for sep in ("，", ",", "（", "(", "「", "『", "/", "：", ":", " ", "\t"):
        head = keyword.split(sep)[0].strip()
        head = head.strip("\u300c\u300d\u300e\u300f\"\u2018\u2019\"\u201c\u201d\uff08\uff09()【】[] ")
        if len(head) >= 2 and head != keyword and head not in heads:
            heads.append(head)
    # 短的更可能是花园那边收录的名字
    heads.sort(key=len)
    return heads[:2]


def _search_aliases(show: Show) -> list[str]:
    """挑几个可能好用的别名。

    TMDB 的别名顺序没意义（第一个常是西班牙语长名），而且有的别名只是剧名本身的
    变体（包含剧名或是剧名的子串），拿它们去搜等于白搜。这里按「和剧名差得多、
    又比较短」来排。
    """
    keyword = (getattr(show, "keyword", "") or "").strip()
    folded_keyword = _fold_name(keyword)
    usable = []
    for alias in list(getattr(show, "extra_aliases", None) or []):
        text = str(alias).strip()
        if not text or text == keyword:
            continue
        folded = _fold_name(text)
        if not folded or folded == folded_keyword:
            continue
        if folded_keyword and folded_keyword in folded:
            # 只是剧名加了一堆后缀，搜它等于白搜
            continue
        usable.append(text)
    usable.sort(key=len)
    return usable[:5]


def _fetch_candidates(keyword: str, cancel) -> list:
    """拉一次动漫花园。失败重试一次；还不行就当这部剧没有候选，不拖垮整轮。"""
    for attempt in range(2):
        if _event_set(cancel):
            return []
        try:
            return parse_rss(fetch_rss(keyword))
        except Exception as exc:
            logger.info(f"动漫 115 拉取 {keyword} 失败（第 {attempt + 1} 次）：{type(exc).__name__}")
            if attempt == 0:
                _pause(cancel, 3)
    return []


def _entries_for(show: Show, cancel, gap: dict) -> list:
    """每部剧查一次 RSS。体积还没有时再查列表页。相邻请求至少隔 2 秒。"""
    if _event_set(cancel):
        return []
    if gap.get("done"):
        _pause(cancel, 2)
    if _event_set(cancel):
        return []
    gap["done"] = True
    torrents = _fetch_candidates(show.keyword, cancel)
    if len(torrents) < _ALIAS_SEARCH_THRESHOLD:
        # 剧名搜到的太少时，用「剧名的短形式」和 TMDB 的别名再搜并合并。
        # 订阅名常常很长（《才女的侍从 在满是…》），花园那边按短名收录。
        known = {item.title for item in torrents}
        for alias in _short_keywords(show) + _search_aliases(show):
            if _event_set(cancel):
                return torrents
            _pause(cancel, 2)
            if _event_set(cancel):
                return torrents
            extra = _fetch_candidates(alias, cancel)
            if not extra:
                continue
            fresh = [item for item in extra if item.title not in known]
            if fresh:
                torrents.extend(fresh)
                known.update(item.title for item in fresh)
                logger.info(f"动漫 115 {show.keyword} 用别名 {alias} 补到 {len(fresh)} 条")
    if _event_set(cancel) or all(item.size for item in torrents):
        return torrents
    _pause(cancel, 2)
    if _event_set(cancel):
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


def _event_set(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _pause(cancel, seconds: float) -> None:
    if cancel is not None and hasattr(cancel, "wait"):
        cancel.wait(seconds)
        return
    time.sleep(seconds)


def _subscribe_source(value) -> str:
    text = str(value or "mp").strip().lower()
    if text in {"mp", "watchlist", "both"}:
        return text
    return "mp"


def _merge_shows(primary: list, extra: list) -> list:
    found = []
    seen = set()
    for show in [*primary, *extra]:
        key = (show.tmdb_id or 0, show.season or 0, show.keyword)
        if key in seen:
            continue
        seen.add(key)
        found.append(show)
    return found


def _strm_mode(value) -> str:
    """旧配置没有这一项时，strm 改走内网地址。"""
    text = str(value or "").strip().lower()
    if text in {"lan", "public", "redirect"}:
        return text
    return "lan"


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
