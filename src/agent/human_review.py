"""人在回路（Human-in-the-Loop）— 暂停 Agent 执行，等待人类审批（增强版 v2）。

增强（v2）:
  - 审批超时保护: 超过指定时间自动拒绝/通过
  - 异步回调支持
  - 审批历史持久化（可选）
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from core.logger import logger


class ReviewAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"
    TIMEOUT = "timeout"


@dataclass
class ReviewRequest:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    content: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    source_agent: str = ""
    timeout_seconds: float = 300  # 默认 5 分钟超时

    def summary(self) -> str:
        risk_tag = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(self.risk_level, "⚪")
        return (
            f"{risk_tag} [{self.risk_level.upper()}] {self.title}\n"
            f"来源: {self.source_agent}\n"
            f"内容:\n{self.content[:500]}"
        )


@dataclass
class ReviewResult:
    request_id: str = ""
    action: ReviewAction = ReviewAction.APPROVE
    modified_content: str = ""
    comment: str = ""


ReviewCallback = Callable[[ReviewRequest], ReviewResult]


class HumanReview:
    """人在回路管理器（增强版）。

    用法:
        hr = HumanReview(timeout_seconds=300, default_action="reject")
        hr.on_review = lambda req: show_ui_dialog(req)
        result = hr.request_review(req)
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 300,
        default_action: str = "reject",
        history_path: str | None = None,
    ) -> None:
        self._pending: dict[str, ReviewRequest] = {}
        self._callbacks: list[ReviewCallback] = []
        self._history: list[ReviewResult] = []
        self._timeout_seconds = timeout_seconds
        self._default_action = ReviewAction.REJECT if default_action == "reject" else ReviewAction.APPROVE

        # 持久化历史
        self._history_path = history_path
        if history_path:
            self._init_history_db()

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def on_review(self, callback: ReviewCallback) -> None:
        self._callbacks = [callback]

    def add_callback(self, callback: ReviewCallback) -> None:
        self._callbacks.append(callback)

    # ------------------------------------------------------------------
    # 审批流程
    # ------------------------------------------------------------------

    def request_review(self, request: ReviewRequest) -> ReviewResult:
        """同步审批请求（带超时保护）。"""
        self._pending[request.request_id] = request
        effective_timeout = request.timeout_seconds or self._timeout_seconds

        logger.info(
            f"HumanReview [{request.request_id}] risk={request.risk_level} "
            f"title='{request.title}' from [{request.source_agent}] "
            f"timeout={effective_timeout}s"
        )

        if not self._callbacks:
            logger.warning(f"HumanReview: no callback, auto-approving [{request.request_id}]")
            result = ReviewResult(
                request_id=request.request_id, action=ReviewAction.APPROVE, comment="（自动通过：未注册审批回调）"
            )
        else:
            start = time.time()
            try:
                result = self._callbacks[-1](request)

                # 检查超时
                elapsed = time.time() - start
                if elapsed > effective_timeout:
                    logger.warning(
                        f"HumanReview [{request.request_id}] timeout ({elapsed:.1f}s > {effective_timeout}s)"
                    )
                    result = ReviewResult(
                        request_id=request.request_id,
                        action=ReviewAction.TIMEOUT,
                        comment=f"审批超时 ({elapsed:.1f}s)，自动{'拒绝' if self._default_action == ReviewAction.REJECT else '通过'}",
                    )
            except Exception as exc:
                logger.error(f"HumanReview [{request.request_id}] callback error: {exc}")
                result = ReviewResult(
                    request_id=request.request_id, action=self._default_action, comment=f"回调异常: {exc}"
                )

        self._pending.pop(request.request_id, None)
        self._history.append(result)
        self._save_to_history(request, result)
        return result

    async def arequest_review(self, request: ReviewRequest) -> ReviewResult:
        """异步审批请求。"""
        loop = asyncio.get_running_loop()

        def _sync() -> ReviewResult:
            return self.request_review(request)

        return await loop.run_in_executor(None, _sync)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        return len(self._pending)

    def get_pending(self) -> list[ReviewRequest]:
        return list(self._pending.values())

    def history(self) -> list[ReviewResult]:
        return list(self._history)

    # ------------------------------------------------------------------
    # 便捷工厂
    # ------------------------------------------------------------------

    @staticmethod
    def create_request(
        title: str,
        content: str,
        source_agent: str = "",
        risk_level: str = "low",
        context: dict[str, Any] | None = None,
        timeout_seconds: float = 300,
    ) -> ReviewRequest:
        return ReviewRequest(
            title=title,
            content=content,
            source_agent=source_agent,
            risk_level=risk_level,
            context=context or {},
            timeout_seconds=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _init_history_db(self) -> None:
        if not self._history_path:
            return
        Path(self._history_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._history_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    source_agent TEXT DEFAULT '',
                    risk_level TEXT DEFAULT 'low',
                    action TEXT NOT NULL,
                    comment TEXT DEFAULT '',
                    reviewed_at REAL NOT NULL
                )
            """)
            conn.commit()

    def _save_to_history(self, request: ReviewRequest, result: ReviewResult) -> None:
        if not self._history_path:
            return
        try:
            with sqlite3.connect(self._history_path) as conn:
                conn.execute(
                    "INSERT INTO review_history (request_id, title, source_agent, risk_level, action, comment, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        request.request_id,
                        request.title,
                        request.source_agent,
                        request.risk_level,
                        result.action.value,
                        result.comment,
                        time.time(),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"HumanReview history persist failed: {exc}")
