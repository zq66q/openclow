"""Tool 基类 + ToolMeta + 工具元数据定义。

所有 Skill 必须继承 Tool 基类，覆盖 name/description/parameters/execute。
call() 方法提供统一包装：参数校验 → 超时控制 → 日志 → 异常转换。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.exceptions import ToolError, ToolParamError, ToolTimeoutError
from core.logger import get_trace_id, logger


class ToolDangerLevel(str, Enum):
    """工具危险等级。"""

    SAFE = "safe"  # 只读操作，直接执行
    WRITE = "write"  # 写入操作，需确认
    DANGEROUS = "dangerous"  # 高危操作，必须人在回路审批


@dataclass
class ToolMeta:
    """工具运行时元数据。"""

    timeout: int = 30  # 单次调用超时（秒）
    max_retries: int = 3  # 最大重试次数
    danger_level: ToolDangerLevel = ToolDangerLevel.SAFE
    need_approval: bool = False  # 是否需要人工审批
    tags: list[str] = field(default_factory=list)  # 分类标签


class Tool(ABC):
    """Tool 抽象基类。子类覆盖类属性 + execute 方法即可。"""

    # ---- 子类必须覆盖 ----
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    meta: ToolMeta = ToolMeta()

    @abstractmethod
    def execute(self, **kwargs: Any) -> Any:
        """实际执行逻辑。参数由 LLM 根据 parameters JSON Schema 生成。"""
        ...

    # ---- 统一调用包装 ----

    def call(self, **kwargs: Any) -> dict[str, Any]:
        """带校验、超时、日志、异常转换的统一入口。

        Returns:
            {"success": bool, "result": Any, "error": str|None, "elapsed_ms": float}
        """
        trace_id = get_trace_id()

        # 1. 参数基础校验（必填字段）
        required = self.parameters.get("required", [])
        for key in required:
            if key not in kwargs:
                raise ToolParamError(f"工具 [{self.name}] 缺少必填参数: {key}")

        # 2. 危险等级预检（仅记录，实际审批由上层 Agent 决定）
        if self.meta.danger_level == ToolDangerLevel.DANGEROUS:
            logger.warning(
                f"Tool [{self.name}] is DANGEROUS",
                extra={"trace_id": trace_id, "args": kwargs},
            )

        # 3. 执行（带超时）
        logger.info(
            f"Tool [{self.name}] start",
            extra={"trace_id": trace_id, "args": kwargs, "timeout": self.meta.timeout},
        )
        start = time.perf_counter()

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.execute, **kwargs)
                result = future.result(timeout=self.meta.timeout)

            elapsed = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                f"Tool [{self.name}] success",
                extra={"trace_id": trace_id, "elapsed_ms": elapsed},
            )
            return {
                "success": True,
                "result": result,
                "error": None,
                "elapsed_ms": elapsed,
            }

        except FutureTimeoutError:
            elapsed = round((time.perf_counter() - start) * 1000, 2)
            logger.error(
                f"Tool [{self.name}] timeout",
                extra={"trace_id": trace_id, "timeout": self.meta.timeout, "elapsed_ms": elapsed},
            )
            raise ToolTimeoutError(
                f"工具 [{self.name}] 执行超时 ({self.meta.timeout}s)",
                recoverable=True,
            ) from None

        except ToolError:
            # Tool 内部已抛出的特定异常，直接透传
            raise

        except Exception as exc:
            elapsed = round((time.perf_counter() - start) * 1000, 2)
            logger.error(
                f"Tool [{self.name}] failed",
                extra={"trace_id": trace_id, "error": str(exc), "elapsed_ms": elapsed},
            )
            raise ToolError(f"工具 [{self.name}] 执行失败: {exc}") from exc

    def to_openai_schema(self) -> dict[str, Any]:
        """生成 OpenAI Function Calling 格式的工具定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
