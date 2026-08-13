"""文件读写工具 — 限定安全目录内操作。

防止路径遍历攻击，默认 safe_root 为当前工作目录。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp_tools.base import Tool, ToolMeta, ToolDangerLevel
from mcp_tools.registry import register_tool


@register_tool
class ReadFileTool(Tool):
    """读取文本文件内容，支持行号范围。"""

    name = "read_file"
    description = (
        "读取文本文件内容。支持按行号范围读取（offset + limit），"
        "支持自动检测编码。文件路径必须在项目安全目录内。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件相对路径，例如 src/core/settings.py。",
            },
            "encoding": {
                "type": "string",
                "default": "auto",
                "description": "文件编码，auto 表示自动检测。",
            },
            "offset": {
                "type": "integer",
                "default": 0,
                "description": "跳过的行数（从 0 开始）。",
            },
            "limit": {
                "type": "integer",
                "default": 100,
                "description": "最多读取多少行。",
            },
        },
        "required": ["path"],
    }
    meta = ToolMeta(timeout=10, danger_level=ToolDangerLevel.SAFE, tags=["file", "io"])

    def _safe_path(self, raw_path: str) -> Path:
        safe_root = Path.cwd().resolve()
        target = (safe_root / raw_path).resolve()
        if not str(target).startswith(str(safe_root)):
            raise PermissionError(f"路径不在安全目录范围内: {raw_path}")
        return target

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        raw_path: str = kwargs["path"]
        encoding: str = kwargs.get("encoding", "auto")
        offset: int = kwargs.get("offset", 0)
        limit: int = kwargs.get("limit", 100)

        file_path = self._safe_path(raw_path)

        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {raw_path}")

        # 自动检测编码
        if encoding == "auto":
            try:
                with open(file_path, "rb") as f:
                    raw = f.read()
                    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
            except Exception:
                encoding = "utf-8"

        size_bytes = file_path.stat().st_size

        with open(file_path, encoding=encoding, errors="replace") as f:
            if offset > 0:
                for _ in range(offset):
                    f.readline()

            lines = []
            for _ in range(limit):
                line = f.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n\r"))

            # 判断是否还有更多内容
            next_line = f.readline()
            truncated = next_line != ""

        return {
            "path": str(file_path),
            "size_bytes": size_bytes,
            "encoding": encoding,
            "offset": offset,
            "limit": limit,
            "line_count": len(lines),
            "content": "\n".join(lines),
            "truncated": truncated,
        }


@register_tool
class WriteFileTool(Tool):
    """写入或追加文件内容，限定安全目录。"""

    name = "write_file"
    description = (
        "写入或追加文件内容。mode=overwrite 覆盖写入，mode=append 追加写入。"
        "文件路径必须在项目安全目录内。写入操作需要人工审批。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件相对路径。",
            },
            "content": {
                "type": "string",
                "description": "要写入的内容。",
            },
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "default": "overwrite",
                "description": "写入模式。",
            },
            "encoding": {
                "type": "string",
                "default": "utf-8",
                "description": "文件编码。",
            },
        },
        "required": ["path", "content"],
    }
    meta = ToolMeta(
        timeout=10,
        danger_level=ToolDangerLevel.WRITE,
        tags=["file", "io", "write"],
        need_approval=True,
    )

    def _safe_path(self, raw_path: str) -> Path:
        safe_root = Path.cwd().resolve()
        target = (safe_root / raw_path).resolve()
        if not str(target).startswith(str(safe_root)):
            raise PermissionError(f"路径不在安全目录范围内: {raw_path}")
        return target

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        raw_path: str = kwargs["path"]
        content: str = kwargs["content"]
        mode: str = kwargs.get("mode", "overwrite")
        encoding: str = kwargs.get("encoding", "utf-8")

        file_path = self._safe_path(raw_path)

        # 确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)

        write_mode = "a" if mode == "append" else "w"
        with open(file_path, write_mode, encoding=encoding, newline="") as f:
            f.write(content)
            bytes_written = f.tell() if mode == "append" else len(content.encode(encoding))

        return {
            "path": str(file_path),
            "mode": mode,
            "bytes_written": bytes_written,
            "encoding": encoding,
        }
