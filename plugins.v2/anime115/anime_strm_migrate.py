"""把历史 strm 的外网入口从 443 换成 8443（备案期避开 443）。

只改 URL 的 origin：``https://mp.yuanhot.top/...`` → ``https://mp.yuanhot.top:8443/...``。
``pickcode``、``file_name`` 和其他 query 参数原样保留；内网地址（``192.168.1.9:3000``）
不动，避免把刻意写成内网的 strm 改成外网。
"""

from __future__ import annotations

from pathlib import Path

OLD_PREFIX = "https://mp.yuanhot.top"
NEW_PREFIX = "https://mp.yuanhot.top:8443"


def normalize_url(text: str) -> str:
    """返回迁移后的 strm 内容。已经是 :8443 或内网地址的原样返回。"""
    url = (text or "").strip()
    if url.startswith(OLD_PREFIX + "/") or url.startswith(OLD_PREFIX + "?"):
        return NEW_PREFIX + url[len(OLD_PREFIX):]
    return url


def migrate(root: str | Path, dry_run: bool = False) -> dict:
    """遍历 root 下所有 *.strm，只重写需要改的文件。"""
    root_path = Path(root)
    changed = total = 0
    changed_paths: list[str] = []
    for path in sorted(root_path.rglob("*.strm")):
        total += 1
        try:
            old = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        new = normalize_url(old)
        if new == old:
            continue
        changed += 1
        changed_paths.append(str(path))
        if not dry_run:
            path.write_text(new + "\n", encoding="utf-8")
    return {"total": total, "changed": changed, "dry_run": dry_run, "paths": changed_paths}


def main(argv: list[str] | None = None) -> int:
    """命令行入口：默认 dry-run，加 --apply 才落盘。"""
    import argparse

    parser = argparse.ArgumentParser(description="把 strm 外网入口从 443 迁到 8443")
    parser.add_argument("root", help="strm 根目录，例如 /media/动漫")
    parser.add_argument("--apply", action="store_true", help="真正写盘（默认只统计）")
    args = parser.parse_args(argv)
    result = migrate(args.root, dry_run=not args.apply)
    mode = "dry-run" if result["dry_run"] else "applied"
    print(f"{mode}: total={result['total']} changed={result['changed']}")
    for path in result["paths"][:5]:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
