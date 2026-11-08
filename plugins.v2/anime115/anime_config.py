"""和 115 AV刮削对齐的配置读取。不访问 115。"""

from __future__ import annotations

PLAY_BASE = (
    "https://mp.yuanhot.top/api/v1/plugin/P115StrmHelper/redirect_url"
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
