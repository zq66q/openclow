#!/usr/bin/env python3
"""OpenClaw 数据备份脚本（跨平台，SQLite 在线安全备份）。

用法:
    python scripts/backup.py                       # 默认备份到 ./backups
    python scripts/backup.py --dir /var/backups    # 指定备份输出目录
    python scripts/backup.py --data /srv/openclaw/data  # 指定数据源目录
    python scripts/backup.py --keep 7              # 仅保留最近 7 份

可用环境变量:
    OPENCLAW_DATA_DIR     数据目录    (默认 ./data)
    OPENCLAW_BACKUP_DIR   备份目录    (默认 ./backups)
    OPENCLAW_BACKUP_KEEP  保留份数    (默认 14)

特性:
    - SQLite 文件用连接级 backup API，服务运行时也能得到一致快照
    - 非 SQLite 文件（审计日志、上传文件）直接复制
    - 输出 tar.gz 压缩包，自动清理过期备份
    - Windows / Linux 均可运行（Linux cron 入口见 scripts/backup.sh）
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime
from pathlib import Path

DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
EXCLUDE_DIRS = {"__pycache__", ".git"}


def _is_db_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in DB_SUFFIXES


def _sqlite_backup(src: Path, dst: Path) -> None:
    """使用 SQLite 在线备份 API，运行中的数据库也能安全快照."""
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def copy_tree(src: Path, dst: Path) -> None:
    """递归复制目录；SQLite 文件走在线备份，其余直接复制."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in EXCLUDE_DIRS:
            continue
        target = dst / item.name
        if _is_db_file(item):
            _sqlite_backup(item, target)
        elif item.is_dir():
            copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenClaw 数据备份")
    parser.add_argument(
        "--dir",
        default=os.environ.get("OPENCLAW_BACKUP_DIR", "./backups"),
        help="备份输出目录（默认 ./backups）",
    )
    parser.add_argument(
        "--data",
        default=os.environ.get("OPENCLAW_DATA_DIR", "./data"),
        help="数据源目录（默认 ./data）",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=int(os.environ.get("OPENCLAW_BACKUP_KEEP", "14")),
        help="保留最近 N 份备份（默认 14）",
    )
    args = parser.parse_args()

    data_dir = Path(args.data).resolve()
    if not data_dir.is_dir():
        print(f"[ERROR] 数据目录不存在: {data_dir}", file=sys.stderr)
        return 1

    backup_dir = Path(args.dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 清理上次中断残留的临时目录（未压缩、不匹配 .tar.gz）。
    # 失败仅警告，不中断备份主流程（例如容器/沙箱内无删除权限）。
    for stale in backup_dir.glob("openclaw-backup-*"):
        if stale.is_dir():
            try:
                shutil.rmtree(stale, ignore_errors=True)
                print(f"[INFO] 已清理残留临时目录: {stale.name}")
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] 清理残留临时目录失败（不影响备份）: {stale.name}: {exc}", file=sys.stderr)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    staging = backup_dir / f"openclaw-backup-{timestamp}"

    try:
        copy_tree(data_dir, staging)

        archive = backup_dir / f"openclaw-backup-{timestamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname=staging.name)
        shutil.rmtree(staging, ignore_errors=True)

        size_kib = archive.stat().st_size / 1024
        print(f"[OK] 备份完成: {archive}")
        print(f"     大小: {size_kib:.1f} KiB")
    except Exception as exc:  # noqa: BLE001 - 备份失败应报错退出
        print(f"[ERROR] 备份失败: {exc}", file=sys.stderr)
        shutil.rmtree(staging, ignore_errors=True)
        return 1

    # 清理过期备份（单文件删除失败仅警告，不中断）
    old_archives = sorted(backup_dir.glob("openclaw-backup-*.tar.gz"), reverse=True)
    for old in old_archives[args.keep :]:
        try:
            old.unlink()
            print(f"[INFO] 已删除过期备份: {old.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 删除过期备份失败: {old.name}: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
