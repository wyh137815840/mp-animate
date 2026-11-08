"""插件详情页的进度和状态。句式跟 115 AV刮削的详情计数同一路。"""

from __future__ import annotations

_VERBS = {"daily": "每日", "organize": "整理", "flatten": "摊平"}


def format_state(snap: dict | None) -> str:
    """状态行。空闲、进行中、已完成、失败，并带预览或正式。"""
    data = snap or {}
    phase = data.get("phase") or ""
    mode = "预览" if data.get("dry_run") else "正式"
    if phase == "running":
        return f"状态：进行中（{mode}）"
    if phase == "done":
        return f"状态：已完成（{mode}）"
    if phase == "failed":
        return f"状态：失败（{mode}）"
    return "状态：空闲"


def format_progress(snap: dict | None) -> str:
    """进度行。当前条目在前，累计计数在后。"""
    data = snap or {}
    job = data.get("job") or ""
    if not job:
        return "进度：还没有运行记录"
    verb = _VERBS.get(job, "任务")
    index = int(data.get("index") or 0)
    total = int(data.get("total") or 0)
    current = str(data.get("current") or "")
    result = str(data.get("result") or "")
    line = f"进度：{verb} {index}/{total}"
    if current:
        line += f"：{current}"
        if result:
            line += f"，{result}"
    return line + "。" + _counters(data)


def advance(state: dict | None, patch: dict) -> dict:
    """把一次上报并进当前进度，并写好两行展示文案。"""
    merged = dict(state or {})
    merged.update(patch or {})
    merged["progress"] = format_progress(merged)
    merged["state"] = format_state(merged)
    return merged


def _counters(data: dict) -> str:
    skipped = int(data.get("skipped") or 0)
    failed = int(data.get("failed") or 0)
    job = data.get("job")
    if job == "organize":
        return f"已整理 {int(data.get('organized') or 0)}，跳过 {skipped}，失败 {failed}"
    if job == "flatten":
        return f"已移动 {int(data.get('moved') or 0)}，跳过 {skipped}，失败 {failed}"
    return (
        f"排队 {int(data.get('queued') or 0)}，已提交 {int(data.get('submitted') or 0)}，"
        f"跳过 {skipped}，失败 {failed}"
    )
