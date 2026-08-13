"""应答生成器 — 将 AgentResult 格式化为用户可读的最终输出。

支持多种输出格式：
  TEXT        — 纯文本
  MARKDOWN    — Markdown 格式化（含步骤引用）
  STRUCTURED  — 结构化 JSON（适合 API 返回）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.base import AgentResult


class ResponseFormat(str, Enum):
    """应答输出格式。"""

    TEXT = "text"
    MARKDOWN = "markdown"
    STRUCTURED = "structured"


@dataclass
class Response:
    """格式化后的应答。"""

    content: str = ""
    format: ResponseFormat = ResponseFormat.TEXT
    metadata: dict = field(default_factory=dict)
    citations: list[str] = field(default_factory=list)


class ResponseGenerator:
    """应答生成器（纯静态方法）。"""

    @staticmethod
    def generate(
        result: AgentResult,
        fmt: ResponseFormat = ResponseFormat.TEXT,
        *,
        include_steps: bool = False,
        include_tokens: bool = False,
    ) -> Response:
        """将 AgentResult 格式化为指定格式的最终输出。

        Args:
            result: Agent 执行结果
            fmt: 输出格式
            include_steps: 是否包含推理步骤
            include_tokens: 是否包含 token 统计

        Returns:
            Response
        """
        if fmt == ResponseFormat.MARKDOWN:
            content = ResponseGenerator._to_markdown(result, include_steps, include_tokens)
        elif fmt == ResponseFormat.STRUCTURED:
            content = ResponseGenerator._to_structured(result, include_steps, include_tokens)
        else:
            content = ResponseGenerator._to_text(result, include_steps, include_tokens)

        return Response(
            content=content,
            format=fmt,
            metadata={
                "success": result.success,
                "loop_type": result.loop_type,
                "tool_calls": result.tool_calls_count,
                "elapsed_ms": result.total_elapsed_ms,
            },
        )

    # ------------------------------------------------------------------
    # 格式转换
    # ------------------------------------------------------------------

    @staticmethod
    def _to_text(
        result: AgentResult,
        include_steps: bool,
        include_tokens: bool,
    ) -> str:
        """纯文本格式。"""
        if not result.success:
            return f"[错误] {result.error}"

        lines = [result.answer]
        if include_steps and result.steps:
            lines.append("\n--- 推理步骤 ---")
            for s in result.steps:
                lines.append(f"步骤 {s.step_num}: Thought={s.thought[:60]}, Action={s.action}, {s.elapsed_ms}ms")
        if include_tokens:
            lines.append(
                f"\nToken: {result.token_usage.get('total', 0)} "
                f"(prompt={result.token_usage.get('prompt', 0)}, "
                f"completion={result.token_usage.get('completion', 0)})"
            )
        return "\n".join(lines)

    @staticmethod
    def _to_markdown(
        result: AgentResult,
        include_steps: bool,
        include_tokens: bool,
    ) -> str:
        """Markdown 格式。"""
        if not result.success:
            return f"> **⚠️ 错误:** {result.error}"

        lines: list[str] = [result.answer]

        if include_steps and result.steps:
            lines.append("\n---")
            lines.append("### 推理步骤")
            for s in result.steps:
                lines.append(f"- **步骤 {s.step_num}** ({s.elapsed_ms}ms)")
                if s.thought:
                    lines.append(f"  - 思考: {s.thought[:80]}")
                if s.action:
                    lines.append(f"  - 工具: `{s.action}`")
                if s.observation:
                    lines.append(f"  - 结果: {s.observation[:100]}")

        if include_tokens:
            lines.append("\n---")
            lines.append(f"*Token 用量: {result.token_usage.get('total', 0)}*")

        return "\n".join(lines)

    @staticmethod
    def _to_structured(
        result: AgentResult,
        include_steps: bool,
        include_tokens: bool,
    ) -> str:
        """结构化 JSON 格式。"""
        output: dict = {
            "success": result.success,
            "answer": result.answer if result.success else None,
            "error": result.error if not result.success else None,
            "loop_type": result.loop_type,
            "tool_calls": result.tool_calls_count,
            "elapsed_ms": result.total_elapsed_ms,
        }
        if include_steps:
            output["steps"] = [
                {
                    "step": s.step_num,
                    "thought": s.thought,
                    "action": s.action,
                    "action_input": s.action_input,
                    "observation": s.observation[:200] if s.observation else "",
                    "elapsed_ms": s.elapsed_ms,
                }
                for s in result.steps
            ]
        if include_tokens:
            output["token_usage"] = result.token_usage
        return json.dumps(output, ensure_ascii=False, indent=2)
