"""记忆压缩器 — 后台异步摘要压缩 + 自动信息抽取。

监控短期记忆的 token 使用量，超阈值（70%）时自动：
1. 触发 LLM 摘要压缩 → 释放短期记忆 token 空间
2. 从被压缩内容中抽取结构化事实 → 写入长期记忆
3. 摘要本身也存入长期记忆（episode 类型）

支持 asyncio 后台循环 + 手动触发双模式。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from core.logger import logger
from core.settings import settings

if TYPE_CHECKING:
    from core.llm_client import BaseLLMClient
    from memory.extractor import MemoryExtractor
    from memory.memory_manager import MemoryManager


class MemoryCompressor:
    """后台压缩器 — 自动管理短期记忆生命周期。

    用法:
        compressor = MemoryCompressor(mm, llm, extractor)

        # 手动触发一次
        summary = compressor.compress_once()

        # 后台循环（永不返回，直到 stop）
        await compressor.run(interval=15.0)
    """

    # 触发阈值 — 短期记忆 token 使用率达到此值则触发压缩
    THRESHOLD_RATIO = 0.7
    # 最少消息数 — 低于此值不触发
    MIN_MESSAGES = 8
    # 最小压缩间隔（秒）
    MIN_INTERVAL = 30.0
    # 压缩后最少保留的消息数
    MAX_KEEP = 6

    def __init__(
        self,
        memory_manager: MemoryManager,
        llm_client: BaseLLMClient,
        extractor: MemoryExtractor,
    ) -> None:
        self._mm = memory_manager
        self._llm = llm_client
        self._extractor = extractor
        self._running = False
        self._last_compress: float = 0.0

    # ------------------------------------------------------------------
    # 判断
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def should_compress(self) -> bool:
        """检查是否应该触发一次压缩。"""
        stm = self._mm.short_term
        token_count = stm.token_count()
        max_tokens = settings.memory.short_term_max_tokens
        threshold = int(max_tokens * self.THRESHOLD_RATIO)

        if token_count < threshold:
            return False

        if stm.message_count() < self.MIN_MESSAGES:
            return False

        return time.time() - self._last_compress >= self.MIN_INTERVAL

    # ------------------------------------------------------------------
    # 压缩
    # ------------------------------------------------------------------

    def compress_once(self) -> str | None:
        """执行一次完整压缩：摘要 + 事实抽取 + 长期存储。

        Returns:
            生成的摘要文本，无需压缩则返回 None
        """
        if not self.should_compress():
            return None

        stm = self._mm.short_term

        # 1. 保存压缩前的消息（用于后续事实抽取）
        messages_before = stm.get_history()

        # 2. 摘要压缩 — 内部会替换 self._messages
        summary = stm.summarize(llm_client=self._llm, max_keep=self.MAX_KEEP)

        if not summary:
            logger.debug("compressor: summarize returned empty")
            return None

        self._last_compress = time.time()

        # 3. 从被压缩的消息中抽取结构化事实 → 长期记忆
        to_extract = messages_before[: -self.MAX_KEEP] if len(messages_before) > self.MAX_KEEP else messages_before

        dialog = "\n".join(f"[{m.role}]: {m.content}" for m in to_extract)
        extracted = self._extractor.extract_and_save(dialog)

        # 4. 摘要本身也存入长期记忆（episode 类型）
        self._mm.save_fact(
            key=f"summary_{int(time.time())}",
            value=summary,
            memory_type="episode",
            importance=0.5,
        )

        logger.info(
            "compressor cycle complete",
            extra={
                "compressed_from": len(to_extract),
                "remaining": stm.message_count(),
                "summary_len": len(summary),
                "extracted_facts": extracted,
            },
        )
        return summary

    # ------------------------------------------------------------------
    # 后台循环
    # ------------------------------------------------------------------

    async def run(self, check_interval: float = 10.0) -> None:
        """启动后台循环，按固定间隔检查并压缩。

        永不返回，直到 stop() 被调用。
        异常仅记日志不中断循环。

        Args:
            check_interval: 检查间隔（秒），默认 10s
        """
        self._running = True
        logger.info(
            "compressor background loop started",
            extra={"interval": check_interval},
        )

        while self._running:
            try:
                self.compress_once()
            except Exception:
                logger.warning("compressor tick failed", exc_info=True)
            await asyncio.sleep(check_interval)

        logger.info("compressor background loop stopped")

    def stop(self) -> None:
        """停止后台循环（异步安全，设置标志位后下一轮退出）。"""
        self._running = False
        logger.debug("compressor stop flag set")
