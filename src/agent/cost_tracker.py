"""成本追踪 — 按 Agent、会话、工具维度统计 token 和费用（增强版 v2）。

增强（v2）:
  - SQLite 持久化: 重启不丢失
  - 跨会话聚合查询
  - 自动计算费用
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from core.logger import logger


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class CostReport:
    session_id: str = ""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_tool_calls: int = 0
    estimated_cost_usd: float = 0.0
    per_agent: dict[str, dict[str, int]] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0


_MODEL_PRICE: dict[str, dict[str, float]] = {
    "gpt-4": {"prompt": 30.0, "completion": 60.0},
    "gpt-4o": {"prompt": 2.5, "completion": 10.0},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.6},
    "gpt-3.5-turbo": {"prompt": 0.5, "completion": 1.5},
    "deepseek-chat": {"prompt": 0.14, "completion": 0.28},
    "deepseek-reasoner": {"prompt": 0.55, "completion": 2.19},
    "claude-3": {"prompt": 3.0, "completion": 15.0},
}


class CostTracker:
    """Token 和工具调用成本追踪器（增强版）。"""

    def __init__(
        self,
        session_id: str = "",
        budget_usd: float = 0.0,
        model: str = "",
        *,
        persist_path: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.budget_usd = budget_usd
        self.model = model

        self._agent_tokens: dict[str, dict[str, int]] = {}
        self._agent_tool_calls: dict[str, int] = {}
        self._alerts: list[str] = []
        self._started_at = time.time()

        # SQLite 持久化
        self._db_path: str | None = None
        if persist_path:
            self._init_db(persist_path)

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------

    def record_tokens(self, agent_name: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        if agent_name not in self._agent_tokens:
            self._agent_tokens[agent_name] = {"prompt": 0, "completion": 0, "total": 0}
        stats = self._agent_tokens[agent_name]
        stats["prompt"] += prompt_tokens
        stats["completion"] += completion_tokens
        stats["total"] += prompt_tokens + completion_tokens

        if self._db_path:
            self._persist_record(agent_name, prompt_tokens, completion_tokens)

    def record_tool_call(self, agent_name: str) -> None:
        self._agent_tool_calls[agent_name] = self._agent_tool_calls.get(agent_name, 0) + 1

    def record_result(self, agent_name: str, result: Any) -> None:
        token_usage = getattr(result, "token_usage", {})
        if token_usage:
            self.record_tokens(agent_name,
                prompt_tokens=token_usage.get("prompt", 0),
                completion_tokens=token_usage.get("completion", 0))
        tool_calls = getattr(result, "tool_calls_count", 0)
        if tool_calls:
            self._agent_tool_calls[agent_name] = self._agent_tool_calls.get(agent_name, 0) + tool_calls

    # ------------------------------------------------------------------
    # 预算
    # ------------------------------------------------------------------

    def check_budget(self) -> bool:
        if self.budget_usd <= 0:
            return True
        current_cost = self.total_cost_usd()
        if current_cost >= self.budget_usd:
            alert = f"⚠️ 预算用尽: ${current_cost:.4f} / ${self.budget_usd:.2f}"
            self._alerts.append(alert)
            logger.warning(alert)
            return False
        if current_cost >= self.budget_usd * 0.8:
            alert = f"⚠️ 预算已达 {current_cost / self.budget_usd * 100:.0f}%: ${current_cost:.4f} / ${self.budget_usd:.2f}"
            if alert not in self._alerts:
                self._alerts.append(alert)
            logger.warning(alert)
        return True

    # ------------------------------------------------------------------
    # 计算
    # ------------------------------------------------------------------

    def total_tokens(self) -> int:
        return sum(s.get("total", 0) for s in self._agent_tokens.values())

    def total_tool_calls(self) -> int:
        return sum(self._agent_tool_calls.values())

    def total_cost_usd(self) -> float:
        if not self.model:
            return 0.0
        pricing = self._resolve_pricing(self.model)
        if not pricing:
            return 0.0
        total_prompt = sum(s.get("prompt", 0) for s in self._agent_tokens.values())
        total_completion = sum(s.get("completion", 0) for s in self._agent_tokens.values())
        return round((total_prompt / 1_000_000) * pricing["prompt"] +
                     (total_completion / 1_000_000) * pricing["completion"], 6)

    # ------------------------------------------------------------------
    # 报告
    # ------------------------------------------------------------------

    def report(self) -> CostReport:
        total_prompt = sum(s.get("prompt", 0) for s in self._agent_tokens.values())
        total_completion = sum(s.get("completion", 0) for s in self._agent_tokens.values())
        return CostReport(
            session_id=self.session_id,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_tokens=total_prompt + total_completion,
            total_tool_calls=self.total_tool_calls(),
            estimated_cost_usd=self.total_cost_usd(),
            per_agent=dict(self._agent_tokens),
            alerts=list(self._alerts),
            started_at=self._started_at,
            ended_at=time.time(),
        )

    # ------------------------------------------------------------------
    # SQLite 持久化
    # ------------------------------------------------------------------

    def _init_db(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = path
        with sqlite3.connect(path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cost_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    model TEXT DEFAULT '',
                    recorded_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_session ON cost_records(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_agent ON cost_records(agent_name)")
            conn.commit()

    def _persist_record(self, agent_name: str, prompt: int, completion: int) -> None:
        if not self._db_path:
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO cost_records (session_id, agent_name, prompt_tokens, completion_tokens, model, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.session_id, agent_name, prompt, completion, self.model, time.time()),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"CostTracker persist failed: {exc}")

    @staticmethod
    def query_history(db_path: str, *, session_id: str = "", agent_name: str = "",
                      days: int = 7) -> list[dict[str, Any]]:
        """查询历史成本记录。"""
        if not Path(db_path).exists():
            return []
        since = time.time() - days * 86400
        query = "SELECT session_id, agent_name, prompt_tokens, completion_tokens, model, recorded_at FROM cost_records WHERE recorded_at >= ?"
        params: list = [since]
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        query += " ORDER BY recorded_at DESC LIMIT 500"

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {"session_id": r[0], "agent_name": r[1], "prompt_tokens": r[2],
             "completion_tokens": r[3], "model": r[4], "recorded_at": r[5]}
            for r in rows
        ]

    @staticmethod
    def _resolve_pricing(model: str) -> dict[str, float] | None:
        model_lower = model.lower()
        for prefix, price in _MODEL_PRICE.items():
            if model_lower.startswith(prefix):
                return price
        return None
