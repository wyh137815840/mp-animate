"""整包摊平。先分类，只有安全移动才允许执行。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from anime_names import join_path, name_of, norm, parent_path


VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".m2ts"}
SUB_EXTS = {".ass", ".srt", ".ssa", ".sup"}
_SEASON = re.compile(r"^season\s*(\d{1,2})$", re.IGNORECASE)
_PREVIEW = ("sample", "trailer", "preview", "预告")
_BRACKET = re.compile(r"^[\[【].*?[\]】]\s*")


@dataclass
class Node:
    """目录里的一项。raw 留给真正的 115 文件对象。"""

    path: str
    name: str
    is_dir: bool
    size: int = 0
    raw: object = None


@dataclass
class Proposal:
    """一次建议的移动。"""

    kind: str
    source: str
    dest: str
    reason: str
    node: Node | None = None


@dataclass
class FlattenResult:
    """一次摊平的分类结果。"""

    proposals: list[Proposal] = field(default_factory=list)
    moved: int = 0
    error: str = ""

    def counts(self) -> dict[str, int]:
        """四类各有多少。"""
        counts = {"ok_eligible_non_collision": 0, "collision": 0, "uncertain": 0, "skip": 0}
        for item in self.proposals:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return counts


def propose(nodes: list[Node], roots: list[str], min_bytes: int) -> list[Proposal]:
    """根据当前目录树给出提案，不改任何文件。"""
    roots = [norm(item) for item in roots if str(item).strip()]
    by_parent: dict[str, list[Node]] = {}
    by_path = {}
    for node in nodes:
        path = norm(node.path)
        by_path[path] = node
        by_parent.setdefault(parent_path(path), []).append(node)
    proposals = []
    lifted = set()
    for path, node in by_path.items():
        if not node.is_dir or path in roots:
            continue
        children = by_parent.get(path, [])
        subdirs = [item for item in children if item.is_dir]
        files = [item for item in children if not item.is_dir]
        if len(subdirs) != 1 or files:
            continue
        child = subdirs[0]
        if _folded(name_of(path)) != _folded(child.name):
            continue
        for item in by_parent.get(norm(child.path), []):
            dest = join_path(path, item.name)
            proposals.append(_classify(item, dest, by_path, min_bytes, "同名包装目录上提"))
            lifted.add(norm(item.path))
    for node in nodes:
        path = norm(node.path)
        if node.is_dir or path in lifted:
            continue
        if _inside_season(path):
            continue
        grand = parent_path(parent_path(path))
        seasons = [item for item in by_parent.get(grand, []) if item.is_dir and _season_number(item.name) is not None]
        if len(seasons) != 1:
            if len(seasons) > 1 and _is_media(node, min_bytes):
                proposals.append(Proposal("uncertain", path, "", "有多个 Season 目录", node))
            continue
        dest = join_path(seasons[0].path, node.name)
        proposals.append(_classify(node, dest, by_path, min_bytes, "抬进已有 Season"))
    return proposals


def apply_proposals(proposals: list[Proposal], session, apply: bool, limit: int) -> int:
    """没有安全移动时什么都不做。有的话只移动安全的那些。"""
    safe = [item for item in proposals if item.kind == "ok_eligible_non_collision" and item.node is not None]
    if not safe or not apply:
        return 0
    moved = 0
    for item in safe:
        if moved >= limit:
            break
        if session.move(item.node, parent_path(item.dest), name_of(item.dest)):
            moved += 1
    return moved


def _classify(node: Node, dest: str, by_path: dict, min_bytes: int, reason: str) -> Proposal:
    path = norm(node.path)
    if not _is_media(node, min_bytes) and not (node.is_dir and _folded(node.name) == _folded(name_of(parent_path(path)))):
        if _is_preview(node) or (not node.is_dir and not _is_sidecar(node) and not _is_video(node)):
            return Proposal("skip", path, dest, "不是要搬的视频或字幕", node)
        if not node.is_dir and _is_video(node) and node.size and node.size < min_bytes:
            return Proposal("skip", path, dest, "体积过小", node)
    if not node.is_dir and not _is_video(node) and not _is_sidecar(node):
        return Proposal("skip", path, dest, "不是视频或字幕", node)
    if norm(dest) in by_path:
        return Proposal("collision", path, dest, "目标已存在", node)
    return Proposal("ok_eligible_non_collision", path, dest, reason, node)


def _is_media(node: Node, min_bytes: int) -> bool:
    if node.is_dir or not _is_video(node) or _is_preview(node):
        return False
    if node.size and node.size < min_bytes:
        return False
    return True


def _is_video(node: Node) -> bool:
    return _suffix(node.name) in VIDEO_EXTS


def _is_sidecar(node: Node) -> bool:
    return _suffix(node.name) in SUB_EXTS


def _is_preview(node: Node) -> bool:
    lower = node.name.lower()
    return any(word in lower for word in _PREVIEW)


def _inside_season(path: str) -> bool:
    return _season_number(name_of(parent_path(path))) is not None


def _season_number(name: str) -> int | None:
    matched = _SEASON.match((name or "").strip())
    if not matched:
        return None
    return int(matched.group(1))


def _folded(name: str) -> str:
    text = _BRACKET.sub("", name or "").casefold()
    return re.sub(r"\s+", "", text)


def _suffix(name: str) -> str:
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()
