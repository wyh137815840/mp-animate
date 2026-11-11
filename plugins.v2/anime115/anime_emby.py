"""通知 MoviePilot 里的 Emby 刷新指定媒体库。失败只返回中文句子，不抛异常。"""

from __future__ import annotations


def _running_media_server_modules():
    """延迟导入宿主，只在调用方没有注入 modules 时走这里。"""
    from app.core.module import ModuleManager
    from app.schemas.types import ModuleType

    return ModuleManager().get_running_type_modules(ModuleType.MediaServer)


def refresh_library(library_name: str, modules=None) -> str:
    """刷新 MoviePilot 里已登录 Emby 的指定媒体库，返回给详情页看的中文结果。

    :param library_name: 媒体库名称，忽略首尾空白、按大小写不敏感匹配
    :param modules: 可选的媒体服务器模块可迭代对象；只在单测里注入，
                    注入时完全不导入 MoviePilot 宿主
    :return: 中文结果说明。找不到库或刷新失败都只返回句子，绝不抛异常，
             也绝不回退成刷新全部媒体库
    """
    try:
        wanted = str(library_name or "").strip()
        candidates = _running_media_server_modules() if modules is None else modules
        found = False
        for module in candidates or []:
            if module.__class__.__name__ != "EmbyModule":
                continue
            found = True
            for instance_name, server in (module.get_instances() or {}).items():
                libraries = server.get_librarys() or []
                target = next(
                    (
                        library
                        for library in libraries
                        if str(getattr(library, "name", "") or "").casefold() == wanted.casefold()
                    ),
                    None,
                )
                if target is None:
                    continue
                refresh_one = getattr(server, "_Emby__refresh_emby_library_by_id", None)
                if not refresh_one:
                    return f"{instance_name} 不支持按库刷新"
                if refresh_one(str(getattr(target, "id", ""))):
                    return f"已通知 {instance_name} 刷新媒体库 {target.name}"
                return f"{instance_name} 刷新媒体库 {target.name} 失败"
        if not found:
            return "MoviePilot 里没有可用的 Emby"
        return f"没有找到名为 {wanted} 的 Emby 媒体库"
    except Exception as exc:
        return f"刷新 Emby 失败：{exc}"
