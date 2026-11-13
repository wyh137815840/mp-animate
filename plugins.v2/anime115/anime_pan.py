"""115 Cookie 客户端。列目录和移动跟 115 AV刮削一样走 App / aps，添加和查询离线走 p115client。"""

from __future__ import annotations

import threading
import time

from .anime_flatten import Node
from .anime_names import join_path, norm

_APP = "https://proapi.115.com/android"
_APS_LIST = "https://aps.115.com/natsort/files.php"
# aps 被挡时的备用列目录接口
_PRO_LIST = "https://proapi.115.com/android/2.0/ufile/files"
_WEB_LIST = "https://webapi.115.com/files"
_FS_MKDIR = "https://webapi.115.com/files/add"
_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_PAGE = 500
_RETRY_WAITS = (5, 20, 40)
_RETRY_STATUS = {405, 429, 500, 502, 503, 504}
# 列目录时 405 不重试：aps 对这个账号回 405 时重试也没用，直接换接口。
_LIST_RETRY_STATUS = {429, 500, 502, 503, 504}
# 三个列目录接口都被挡时的冷却秒数：别再硬敲，把风控拖得更久
_LIST_BLOCKED_COOLDOWN = 600


class Pan115:
    """一条 Cookie 上的 115。不碰 MoviePilot 的开放平台存储。"""

    def __init__(self, cookie: str, http=None, gap: float = 2.5, client_factory=None):
        self.cookie = cookie
        self.user_id = _uid(cookie)
        self._dirs = {"/": "0"}
        self._list_url = _APS_LIST
        self._blocked_until = 0.0
        self._gap = gap
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.http = http if http is not None else _live_http(cookie)
        self._client_factory = client_factory or _p115_client

    def ensure_dir(self, path: str) -> str:
        """逐级保证目录存在。已有的目录直接返回 id，不再打 /folder/update。"""
        folder = norm(path)
        parent = "0"
        built = ""
        for part in folder.strip("/").split("/") if folder != "/" else []:
            built += "/" + part
            found = self._child_dir(built, parent, part, strict=True)
            if found:
                parent = found
                continue
            parent = self._mkdir(built, parent, part)
            self._dirs[built] = parent
        return parent

    def _mkdir(self, built: str, parent: str, name: str) -> str:
        """p115client.fs_mkdir：POST webapi /files/add。已存在则沿用现有 id。"""
        try:
            made = self._call(
                "POST",
                "/files/add",
                url=_FS_MKDIR,
                data={"pid": parent, "cname": name},
                headers={"User-Agent": _WEB_UA},
            )
        except RuntimeError as exc:
            if not _dir_exists_text(exc):
                raise RuntimeError(f"无法创建 115 目录 {built}：{exc}") from exc
            found = self._reuse_existing(built, parent, name)
            if found:
                return found
            raise RuntimeError(f"无法创建 115 目录 {built}：{exc}") from exc
        if _dir_exists_payload(made):
            found = self._reuse_existing(built, parent, name)
            if found:
                return found
            raise RuntimeError(f"无法创建 115 目录 {built}：{_err_text(made)}")
        file_id = _usable_id(made)
        if file_id:
            return file_id
        found = self._reuse_existing(built, parent, name)
        if found:
            return found
        raise RuntimeError(f"无法创建 115 目录 {built}：{_err_text(made)}")

    def _reuse_existing(self, built: str, parent: str, name: str) -> str:
        found = self._child_dir(built, parent, name, fresh=True)
        if found:
            _log(f"动漫 115 目录已存在，沿用 {built} {found}", level="info")
        return found

    def dir_id(self, path: str) -> str:
        folder = norm(path)
        if folder == "/":
            return "0"
        parent = "0"
        built = ""
        for part in folder.strip("/").split("/"):
            built += "/" + part
            parent = self._child_dir(built, parent, part)
            if not parent:
                return ""
        return parent

    def list_children(self, directory_id: str) -> list[dict]:
        items = []
        offset = 0
        while True:
            found = self._list_page(directory_id, offset)
            page = [_from_aps(row, directory_id) for row in _items(found)]
            items.extend(page)
            if len(page) < _PAGE:
                return items
            offset += _PAGE

    def _list_page(self, directory_id: str, offset: int) -> dict:
        """列一页目录。

        aps.115.com 偶尔会对这个账号回 405（返回的是网页而不是数据），
        这时换开放平台的列目录接口；换过之后这一轮都走新接口。
        三个接口都被挡的时候直接说被挡，别再硬敲，免得把风控拖得更久。
        """
        params = {
            "cid": directory_id,
            "limit": _PAGE,
            "offset": offset,
            "show_dir": 1,
            "aid": 1,
        }
        if self._blocked_until and time.monotonic() < self._blocked_until:
            raise RuntimeError(f"115 列目录接口被挡，冷却中（{int(self._blocked_until - time.monotonic())} 秒）")
        if self._list_url != _APS_LIST:
            return self._call("GET", "", url=self._list_url, params=params)
        try:
            return self._call("GET", "", url=_APS_LIST, params=params, retry_statuses=_LIST_RETRY_STATUS)
        except RuntimeError as exc:
            last = str(exc)
            _log(f"动漫 115 列目录失败（aps），换开放平台接口：{exc}", level="warning")
        for url in (_PRO_LIST, _WEB_LIST):
            try:
                found = self._call("GET", "", url=url, params=params, retry_statuses=_LIST_RETRY_STATUS)
            except RuntimeError as exc:
                last = str(exc)
                _log(f"动漫 115 列目录失败（{url}）：{exc}", level="warning")
                continue
            self._list_url = url
            _log(f"动漫 115 改用 {url} 列目录", level="info")
            return found
        # 三个都被挡了：这一轮剩下的任务不再去敲 115，等冷却
        self._blocked_until = time.monotonic() + _LIST_BLOCKED_COOLDOWN
        _log(f"动漫 115 列目录接口都被挡了，冷却 {_LIST_BLOCKED_COOLDOWN} 秒再试", level="warning")
        raise RuntimeError(f"115 列目录失败：{directory_id}（{last}）")

    def move(self, file_id: str, directory_id: str) -> None:
        self._post("/files/move", {"ids": file_id, "to_cid": directory_id, "user_id": self.user_id})

    def rename(self, file_id: str, new_name: str) -> None:
        self._post("/files/batch_rename", {f"files_new_name[{file_id}]": new_name, "user_id": self.user_id})

    def delete(self, file_id: str, parent_id: str) -> None:
        self._post("/rb/delete", {"pid": parent_id, "fid": file_id, "user_id": self.user_id})

    def add_magnet(self, magnet: str, directory_id: str) -> dict:
        """和 115 AV刮削一样，用 p115client 的云下载接口提交。"""
        client = self._client_factory(self.cookie)
        result = client.clouddownload_task_add_url({"url": magnet, "wp_path_id": str(directory_id)})
        return result if isinstance(result, dict) else {}

    def list_tasks(self, page: int = 1) -> dict:
        """云下载任务列表，收成和开放平台一样的 data.tasks。页码信息留下，整理才能翻完。"""
        client = self._client_factory(self.cookie)
        method = getattr(client, "clouddownload_task_list", None)
        if method is None:
            raise RuntimeError("p115client 没有 clouddownload_task_list")
        payload = method({"page": page}) or {}
        data = {"tasks": _task_rows(payload)}
        sources = []
        if isinstance(payload, dict):
            sources.append(payload)
            inner = payload.get("data")
            if isinstance(inner, dict):
                sources.append(inner)
        for source in sources:
            for key in ("page", "page_count", "count"):
                if key in source and key not in data:
                    data[key] = source[key]
        return {"data": data}

    def _child_dir(self, path: str, parent: str, name: str, fresh: bool = False, strict: bool = False) -> str:
        if not fresh and self._dirs.get(path):
            return self._dirs[path]
        list_error = None
        try:
            children = self.list_children(parent)
        except RuntimeError as exc:
            children = []
            list_error = exc
        found = _match_dir(children, name, parent, searched=False)
        if found:
            self._dirs[path] = found
            return found
        search_error = None
        if self._blocked_until and time.monotonic() < self._blocked_until:
            # 115 正在挡我们，别再一个目录一个目录地去搜，等冷却过去再说。
            search_error = RuntimeError("115 列目录接口被挡，冷却中")
        else:
            try:
                found = _match_dir(self.search(name), name, parent, searched=True)
            except RuntimeError as exc:
                found = ""
                search_error = exc
        if found:
            self._dirs[path] = found
            return found
        if strict and (list_error or search_error):
            detail = list_error or search_error
            raise RuntimeError(f"115 列目录失败 {path}：{detail}")
        return ""

    def search(self, keyword: str) -> list[dict]:
        found = self._get("/2.0/ufile/search", {"search_value": keyword, "limit": 100, "offset": 0, "aid": 1})
        return _items(found)

    def _get(self, path: str, params: dict) -> dict:
        return self._call("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict:
        return self._call("POST", path, data=data)

    def _call(self, method: str, path: str, url: str = "", retry_statuses=None, **kwargs) -> dict:
        target = url or _APP + path
        label = path or target
        statuses = _RETRY_STATUS if retry_statuses is None else set(retry_statuses)
        last = ""
        for wait in (0, *_RETRY_WAITS):
            if wait:
                time.sleep(wait)
            self._pace()
            try:
                response = self.http.request(method, target, **kwargs)
            except Exception as exc:
                if _is_transport(exc):
                    last = f"115 {label} 连接失败：{type(exc).__name__}"
                    continue
                raise
            status = getattr(response, "status_code", 200)
            if status in statuses:
                last = f"115 {label} 返回 {status}"
                continue
            return _checked(response, label)
        raise RuntimeError(last or f"115 {label} 失败")

    def _pace(self) -> None:
        if self._gap <= 0:
            return
        with self._lock:
            delay = self._last_call + self._gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last_call = time.monotonic()


class CookieSession:
    """给每日、整理、摊平用的 Cookie 会话。一轮里不要再去调开放平台。"""

    def __init__(self, cookie: str, cancel, pan: Pan115 | None = None):
        self.cookie = cookie
        self.cancel = cancel
        self.pan = pan or Pan115(cookie)

    def check_login(self) -> bool:
        return "UID=" in (self.cookie or "")

    def sleep(self, seconds: int) -> None:
        if seconds > 0 and self.cancel is not None:
            self.cancel.wait(seconds)

    def ensure_dir(self, path: str):
        fileid = self.pan.ensure_dir(path)
        return type("Folder", (), {"fileid": fileid, "path": path})()

    def list_dir(self, path: str) -> list[Node]:
        folder = norm(path)
        directory_id = "0" if folder == "/" else self.pan.dir_id(folder)
        if folder != "/" and not directory_id:
            return []
        nodes = []
        for child in self.pan.list_children(directory_id):
            name = _name(child)
            if not name:
                continue
            nodes.append(Node(
                path=join_path(folder, name),
                name=name,
                is_dir=_is_directory(child),
                size=_size(child),
                raw=child,
            ))
        return nodes

    def move(self, node: Node, dest_dir: str, new_name: str) -> bool:
        raw = node.raw if isinstance(node.raw, dict) else {}
        file_id = _file_id(raw)
        if not file_id:
            return False
        if new_name and new_name != node.name:
            self.pan.rename(file_id, new_name)
        self.pan.move(file_id, self.pan.ensure_dir(dest_dir))
        return True

    def delete_dir(self, path: str) -> bool:
        folder = norm(path)
        if folder in ("", "/"):
            return False
        file_id = self.pan.dir_id(folder)
        if not file_id:
            return False
        parent = folder.rsplit("/", 1)[0] or "/"
        parent_id = "0" if parent == "/" else (self.pan.dir_id(parent) or "0")
        self.pan.delete(file_id, parent_id)
        return True

    def add_offline(self, magnet: str, fileid: str):
        return self.pan.add_magnet(magnet, str(fileid))

    def list_offline(self, page: int = 1):
        return self.pan.list_tasks(page)

    def quota(self):
        """Cookie 路径不查开放平台配额。未知配额不拦提交。"""
        return {"data": {}}


def _live_http(cookie: str):
    import httpx

    return httpx.Client(
        timeout=20,
        follow_redirects=True,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) 115wangpan_android/36.2.0",
        },
    )


def _p115_client(cookie: str):
    from p115client import P115Client

    return P115Client(cookie, app="web")


def _uid(cookie: str) -> str:
    for part in (cookie or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == "UID":
            return value.split("_", 1)[0]
    return ""


def _is_transport(exc: Exception) -> bool:
    name = type(exc).__name__
    return "Transport" in name or "Timeout" in name or "Connection" in name


def _checked(response, path: str) -> dict:
    status = getattr(response, "status_code", 200)
    if status != 200:
        raise RuntimeError(f"115 {path} 返回 {status}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"115 {path} 返回的不是 JSON") from exc
    if not isinstance(payload, dict):
        return {}
    if payload.get("state") in (False, 0) and payload.get("errno") not in (None, 0, "0"):
        raise RuntimeError(f"115 {path}：{_err_text(payload)}")
    return payload


def _items(payload: dict) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else []
    if isinstance(data, dict):
        data = data.get("list") or data.get("data") or data.get("tasks") or []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _task_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("tasks", "list", "task"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
    return []


def _from_aps(row: dict, directory_id: str) -> dict:
    """fc 为 0 才是目录。目录 id 用自己的 fid；cid 有时只是父目录，不能拿来当这一级。"""
    name = str(row.get("n") or row.get("fn") or row.get("file_name") or row.get("name") or "")
    fid = str(row.get("fid") or row.get("file_id") or "")
    cid = str(row.get("cid") or row.get("category_id") or "")
    parent = str(row.get("pid") or directory_id)
    code = str(row.get("pc") or row.get("pickcode") or row.get("pick_code") or "")
    if _aps_is_file(row):
        return {
            "fid": fid if fid and fid != "0" else cid,
            "fc": "1",
            "fn": name,
            "pid": cid or parent,
            "fs": row.get("s") or row.get("fs") or 0,
            "pc": code,
        }
    return {
        "fid": _own_id(fid, cid, parent),
        "fc": "0",
        "fn": name,
        "pid": parent,
        "pc": code,
    }


def _aps_is_file(row: dict) -> bool:
    """fc=0 是目录，哪怕名字以 .mkv 结尾。有 fc=1 或单独的 fid 才是文件。"""
    ico = str(row.get("ico") or "").lower()
    if ico in {"folder", "dir", "directory"}:
        return False
    fc = row.get("fc")
    if fc is not None and str(fc) != "":
        return str(fc) != "0"
    fid = str(row.get("fid") or row.get("file_id") or "")
    return bool(fid) and fid != "0"


def _id_of(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("category_id")
        or payload.get("id")
        or payload.get("cid")
        or payload.get("file_id")
        or payload.get("fid")
        or ""
    )


def _usable_id(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    found = _id_of(payload)
    if found and found != "0":
        return found
    data = payload.get("data")
    if isinstance(data, dict):
        found = _id_of(data)
        if found and found != "0":
            return found
    return ""


def _own_id(fid: str, cid: str, parent: str) -> str:
    """目录自己的 id。和父目录相同的 cid 不能当成这一级。"""
    parent = str(parent or "")
    for candidate in (str(fid or ""), str(cid or "")):
        if candidate and candidate != "0" and candidate != parent:
            return candidate
    return ""


def _err_text(payload: dict) -> str:
    message = payload.get("error") or payload.get("error_msg") or payload.get("message") or ""
    errno = payload.get("errno")
    if errno in (None, "", 0, "0"):
        errno = payload.get("errNo") or payload.get("errcode") or payload.get("code")
    return f"{message} errno={errno} body={payload}"


def _dir_exists_text(exc: BaseException) -> bool:
    text = str(exc)
    return "已存在" in text or "errno=20004" in text


def _dir_exists_payload(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("errno") or "") == "20004":
        return True
    message = str(payload.get("error") or payload.get("error_msg") or payload.get("message") or "")
    return "已存在" in message


def _same_dir_name(left: str, right: str) -> bool:
    return (left or "").strip() == (right or "").strip()


def _match_dir(rows, name: str, parent: str, searched: bool) -> str:
    for item in rows:
        if not isinstance(item, dict) or not _is_directory(item):
            continue
        if not _same_dir_name(_name(item), name):
            continue
        if searched and str(item.get("pid") or "") != str(parent):
            continue
        if searched:
            file_id = _own_id(
                str(item.get("fid") or item.get("file_id") or ""),
                str(item.get("cid") or item.get("category_id") or ""),
                str(item.get("pid") or parent),
            )
        else:
            file_id = _file_id(item)
        if file_id and file_id != "0" and file_id != str(parent):
            return file_id
    return ""


def _log(message: str, level: str = "warning") -> None:
    try:
        from app.log import logger
        getattr(logger, level, logger.warning)(message)
    except Exception:
        import logging
        getattr(logging.getLogger("anime115"), level, logging.warning)(message)


def _file_id(item: dict) -> str:
    return str(item.get("fid") or item.get("file_id") or item.get("cid") or "")


def _name(item: dict) -> str:
    return str(item.get("fn") or item.get("n") or item.get("file_name") or item.get("name") or "")


def _size(item: dict) -> int:
    try:
        return int(item.get("fs") or item.get("s") or item.get("size") or item.get("file_size") or 0)
    except (TypeError, ValueError):
        return 0


def _is_directory(item: dict) -> bool:
    if "fc" in item:
        return str(item.get("fc")) == "0"
    if "fid" not in item and "cid" in item:
        return True
    return False
