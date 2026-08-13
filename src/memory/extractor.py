"""记忆抽取器 — 从对话中自动提取结构化事实和用户偏好。

调用 LLM 分析对话文本，输出 [{key, value, type, importance}]，
并自动写入 MemoryManager 的长期记忆。

异步抽取 + 同步降级双模式。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from core.logger import logger

if TYPE_CHECKING:
    from memory.memory_manager import MemoryManager
    from core.llm_client import BaseLLMClient

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """从以下对话中提取用户的关键信息和偏好。
以 JSON 数组返回，每个元素格式：
{{
  "key": "简短_英文_key",
  "value": "中文值",
  "type": "fact|preference|knowledge",
  "importance": 0.0-1.0
}}

规则：
- 只提取明确提到的信息，不要臆测
- importance 按重要性打分：随口提到=0.3，明确陈述=0.6，反复强调=0.9
- 无有效信息则返回空数组 []

对话：
{dialog}

JSON:"""

# ---------------------------------------------------------------------------
# MemoryExtractor
# ---------------------------------------------------------------------------


class MemoryExtractor:
    """对话信息抽取器 — 把自然语言对话变成结构化记忆。

    用法:
        extractor = MemoryExtractor(llm_client, memory_manager)

        # 同步抽取（阻塞）
        count = extractor.extract_and_save("用户: 我叫张三，家住北京...")

        # 异步抽取
        facts = await extractor.extract("用户: 我在学 Python...")
    """

    def __init__(
        self,
        llm_client: BaseLLMClient,
        memory_manager: MemoryManager,
    ) -> None:
        self._llm = llm_client
        self._mm = memory_manager

    # ------------------------------------------------------------------
    # 抽取（不写入）
    # ------------------------------------------------------------------

    async def extract(self, dialog_text: str) -> list[dict]:
        """从对话文本中抽取结构化事实（异步，不写入记忆）。

        Returns:
            [{key, value, type, importance}, ...]，抽取失败返回 []
        """
        try:
            prompt = _EXTRACT_PROMPT.format(dialog=dialog_text[-4000:])
            response, _ = await self._llm.chat_async(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
            )
            facts = self._parse_response(response)
            if facts:
                logger.info(
                    "extractor done",
                    extra={"count": len(facts), "sample": facts[:3]},
                )
            return facts
        except Exception:
            logger.warning("extractor failed", exc_info=True)
            return []

    def extract_sync(self, dialog_text: str) -> list[dict]:
        """同步抽取（阻塞，不写入记忆）。"""
        try:
            prompt = _EXTRACT_PROMPT.format(dialog=dialog_text[-4000:])
            response, _ = self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
            )
            facts = self._parse_response(response)
            if facts:
                logger.info(
                    "extractor (sync) done",
                    extra={"count": len(facts)},
                )
            return facts
        except Exception:
            logger.warning("extractor (sync) failed", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # 抽取 + 写入
    # ------------------------------------------------------------------

    def extract_and_save(self, dialog_text: str) -> int:
        """抽取并自动写入 MemoryManager 的长期记忆。

        Returns:
            成功写入的事实数量
        """
        if not dialog_text.strip():
            return 0

        facts = self.extract_sync(dialog_text)
        saved = 0
        for fact in facts:
            try:
                self._mm.save_fact(
                    key=str(fact["key"]),
                    value=str(fact["value"]),
                    memory_type=str(fact.get("type", "fact")),
                    importance=float(fact.get("importance", 0.5)),
                )
                saved += 1
            except Exception:
                logger.warning(
                    "extractor save_fact failed",
                    extra={"fact": fact},
                    exc_info=True,
                )
        return saved

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(text: str) -> list[dict]:
        """从 LLM 回复中安全解析 JSON 数组，容忍 markdown 代码块包裹。"""
        text = text.strip()

        # 去除 markdown 代码块
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

        # 直接解析
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return [f for f in result if isinstance(f, dict)]
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: 提取 [...] 片段
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, list):
                    return [f for f in result if isinstance(f, dict)]
            except (json.JSONDecodeError, ValueError):
                pass

        logger.warning("extractor parse failed", extra={"raw": text[:200]})
        return []
