"""和 115 AV刮削对齐的配置读取。不访问 115。"""

from __future__ import annotations

PLAY_BASE = (
    "https://mp.yuanhot.top/api/v1/plugin/P115StrmHelper/redirect_url"
    "?pickcode={pickcode}&file_name={filename}"
)
PLAY_BASE_LAN = (
    "http://192.168.1.9:3000/api/v1/plugin/P115StrmHelper/redirect_url"
    "?pickcode={pickcode}&file_name={filename}"
)


def usable_cookie(value: str) -> str:
    """只认带 UID= 的 115 Cookie。空字符串表示改用已登录的存储。"""
    text = (value or "").strip()
    if "UID=" in text:
        return text
    return ""


def offline_directory(config: dict | None) -> str:
    """表单键是 offline_dir。旧配置里的 offline_root 在重新保存前仍然有效。"""
    data = config or {}
    chosen = str(data.get("offline_dir") or data.get("offline_root") or "").strip()
    return chosen or "/离线下载/动漫"


# 宿主机时区下每 30 分钟。MoviePilot 的调度器和每日任务用同一个时区。
DEFAULT_ORGANIZE_CRON = "*/30 * * * *"


def organize_schedule(value) -> str:
    """离线整理留空就用默认周期。每日和摊平仍是留空不注册。"""
    text = str(value or "").strip()
    return text or DEFAULT_ORGANIZE_CRON


def scheduled_jobs(enabled, daily, organize, flatten) -> list[dict]:
    """要注册的定时任务。插件没启用时一个都不注册。"""
    if not enabled:
        return []
    jobs = []
    daily_text = str(daily or "").strip()
    if daily_text:
        jobs.append({
            "id": "Anime115",
            "name": "动漫每日补磁力",
            "cron": daily_text,
            "func": "run_daily",
        })
    jobs.append({
        "id": "Anime115.Organize",
        "name": "动漫离线整理",
        "cron": organize_schedule(organize),
        "func": "run_organize",
    })
    flatten_text = str(flatten or "").strip()
    if flatten_text:
        jobs.append({
            "id": "Anime115.Flatten",
            "name": "动漫整包摊平",
            "cron": flatten_text,
            "func": "run_flatten",
        })
    return jobs
