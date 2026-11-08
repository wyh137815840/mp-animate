"""115 Cookie 客户端。列目录和移动跟 115 AV刮削一样走 App / aps，添加和查询离线走 p115client。"""

from __future__ import annotations

import threading
import time

from .anime_flatten import Node
from .anime_names import join_path, norm

_APP = "https://proapi.115.com/android"
_APS_LIST = "https://aps.115.com/natsort/files.php"
_PAGE = 500
_RETRY_WAITS = (5, 20, 40)
_RETRY_STATUS = {405, 429, 500, 502, 503, 504}


class Pan115:
    """一条 Cookie 上的 115。不碰 MoviePilot 的开放平台存储。"""

    def __init__(self, cookie: str, http=None, gap: float = 2.5, client_factory=None):
        self.cookie = cookie
        self.user_id = _uid(cookie)
        self._dirs = {"/": "0"}
        self._gap = gap
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.http = http if http is not None else _live_http(cookie)
        self._client_factory = client_factory or _p115_client

    def ensure_dir(self, path: str) -> str:
        folder = norm(path)
        parent = "0"
        built = ""
        for part in folder.strip("/").split("/") if folder != "/" else []:
            built += "/" + part
            found = self._child_dir(built, parent, part)
            if found:
                parent = found
                continue
            made = self._post("/folder/update", {"name": part, "pid": parent, "user_id": self.user_id})
            parent = _id_of(made) or _id_of(made.get("data")) or self._child_dir(built, parent, part, fresh=True)
            if not parent:
                raise RuntimeError(f"无法创建 115 目录 {built}")
            self._dirs[built] = parent
        return parent

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
            found = self._call("GET", "", url=_APS_LIST, params={
                "cid": directory_id,
                "limit": _PAGE,
                "offset": offset,
                "show_dir": 1,
                "aid": 1,
            })
            page = [_from_aps(row, directory_id) for row in _items(found)]
            items.extend(page)
            if len(page) < _PAGE:
                return items
            offset += _PAGE

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
        """云下载任务列表，收成和开放平台一样的 data.tasks。"""
        client = self._client_factory(self.cookie)
        method = getattr(client, "clouddownload_task_list", None)
        if method is None:
            raise RuntimeError("p115client 没有 clouddownload_task_list")
        payload = method({"page": page})
        return {"data": {"tasks": _task_rows(payload)}}

    def _child_dir(self, path: str, parent: str, name: str, fresh: bool = False) -> str:
        if not fresh and path in self._dirs:
            return self._dirs[path]
        try:
            children = self.list_children(parent)
        except RuntimeError:
            children = []
        for child in children:
            if _is_directory(child) and _name(child) == name:
                self._dirs[path] = _file_id(child)
                return self._dirs[path]
        for item in self.search(name):
            if _is_directory(item) and _name(item) == name and str(item.get("pid") or "") == parent:
                self._dirs[path] = _file_id(item)
                return self._dirs[path]
        return ""

    def search(self, keyword: str) -> list[dict]:
        found = self._get("/2.0/ufile/search", {"search_value": keyword, "limit": 100, "offset": 0, "aid": 1})
        return _items(found)

    def _get(self, path: str, params: dict) -> dict:
        return self._call("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict:
        return self._call("POST", path, data=data)

    def _call(self, method: str, path: str, url: str = "", **kwargs) -> dict:
        target = url or _APP + path
        label = path or target
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
            if status in _RETRY_STATUS:
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
        message = payload.get("error") or payload.get("error_msg") or payload.get("message") or payload.get("errno")
        raise RuntimeError(f"115 {path}：{message}")
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
    """aps 的目录用 cid 当自己的 id。文件有 fid，名字在 n 或 fn。fc 为 0 才是目录。"""
    name = str(row.get("n") or row.get("fn") or "")
    fid = str(row.get("fid") or "")
    cid = str(row.get("cid") or "")
    is_file = _aps_is_file(row)
    if is_file:
        return {
            "fid": fid if fid and fid != "0" else cid,
            "fc": "1",
            "fn": name,
            "pid": cid or str(row.get("pid") or directory_id),
            "fs": row.get("s") or row.get("fs") or 0,
            "pc": row.get("pc") or "",
        }
    return {
        "fid": cid or (fid if fid and fid != "0" else ""),
        "fc": "0",
        "fn": name,
        "pid": str(row.get("pid") or directory_id),
        "pc": row.get("pc") or "",
    }


def _aps_is_file(row: dict) -> bool:
    """fc=0 是目录，哪怕名字以 .mkv 结尾。有 fc=1 或单独的 fid 才是文件。"""
    fc = row.get("fc")
    if fc is not None and str(fc) != "":
        return str(fc) != "0"
    fid = str(row.get("fid") or "")
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
