"""日志系统 - loguru + trace_id 全链路追踪."""

import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from loguru import logger

# 上下文变量：trace_id
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def get_trace_id() -> str:
    """获取当前上下文 trace_id."""
    return _trace_id.get()


def set_trace_id(trace_id: str | None = None) -> str:
    """设置 trace_id，若未传入则自动生成 UUID."""
    import uuid

    if trace_id is None:
        trace_id = str(uuid.uuid4())[:16]
    _trace_id.set(trace_id)
    return trace_id


class _TraceIdFilter:
    """loguru 过滤器：自动注入 trace_id 到每条日志."""

    def __call__(self, record: Any) -> bool:
        record["extra"]["trace_id"] = get_trace_id()
        return True


def init_logger(
    log_level: str = "INFO",
    log_dir: str = "./data/audit_logs",
) -> None:
    """初始化日志系统.

    控制台：彩色文本格式，便于开发查看；
    `OPENCLAW_LOG_FORMAT=json` 时控制台输出 JSON（便于容器内被采集）。
    文件：JSON 格式（serialize=True），便于审计分析。
    """
    import os

    console_json = os.getenv("OPENCLAW_LOG_FORMAT", "text").strip().lower() == "json"
    logger.remove()

    # 控制台输出
    if console_json:
        logger.add(sys.stdout, level=log_level, serialize=True, filter=_TraceIdFilter())
    else:
        console_fmt = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[trace_id]}</cyan> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        )
        logger.add(sys.stdout, level=log_level, format=console_fmt, filter=_TraceIdFilter())

    # JSON 审计日志文件
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        logger.add(
            Path(log_dir) / "audit_{time:YYYY-MM-DD}.jsonl",
            level=log_level,
            serialize=True,
            filter=_TraceIdFilter(),
            rotation="1 day",
        )


# 模块导入时自动初始化
init_logger()
