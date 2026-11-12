"""115 调用的限时封装。

宿主 U115Pan 遇到风控会在调用线程里 sleep 3600 秒；Cookie 路径一次列目录也可能
重试好几分钟。插件不能陪着一起等：所有会话调用都从这里走，超时就当这次调用失败，
任务继续往下走，不会卡死整轮。
"""

from __future__ import annotations

import threading
import time


# 单次存储调用（列目录、建目录、移动）的上限（秒）。
STORAGE_TIMEOUT = 60
# 离线列表、配额、登录检查这类调用给更短的时限。
OFFLINE_TIMEOUT = 45
# 一轮任务的总预算（秒）。超过就干净收尾，不让这一轮压住下一个周期。
ROUND_BUDGET = 900


class Stopped(Exception):
    """任务被要求停止，或宿主正在关停。"""


def system_stopped() -> bool:
    """宿主关停时会把全局标记置起来。没有宿主时当没停。"""
    try:
        from app.core.config import global_vars
    except Exception:
        return False
    try:
        return bool(getattr(global_vars, "is_system_stopped", False))
    except Exception:
        return False


def stopped(cancel) -> bool:
    """任务被要求停止，或宿主正在关停。"""
    if cancel is not None and getattr(cancel, "is_set", None) and cancel.is_set():
        return True
    return system_stopped()


def wait_call(cancel, func, timeout: float):
    """在限时内等 115 返回。

    超时抛 TimeoutError，停止抛 Stopped。宿主那个还在跑的线程不会被杀掉，
    但插件这一侧不会陪着一直等。
    """
    if stopped(cancel):
        raise Stopped("已停止")
    outcome: dict = {}

    def run():
        try:
            outcome["value"] = func()
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="anime115-115", daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while worker.is_alive():
        if stopped(cancel):
            raise Stopped("已停止")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"115 请求超过 {int(timeout)} 秒")
        worker.join(min(0.5, remaining))
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def call(session, func, timeout: float = STORAGE_TIMEOUT):
    """按会话自己的取消事件限时调用一次 115。"""
    return wait_call(getattr(session, "cancel", None), func, timeout)


def budget_exceeded(started_at: float, budget: float = ROUND_BUDGET) -> bool:
    """这一轮是不是已经用完时间预算。"""
    if not budget or budget <= 0:
        return False
    return (time.monotonic() - started_at) >= budget
