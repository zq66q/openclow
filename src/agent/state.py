"""全局 Agent 状态管理 — 跨 Agent 会话、流水线与共享上下文。

与 base.py 的 AgentState（单 Agent 运行时状态）不同，本模块管理：
  - 多 Agent 协作的完整会话生命周期
  - 流水线中的步骤状态与依赖
  - 跨 Agent 共享的上下文传递
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    """流水线步骤状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class WorkflowStep:
    """多 Agent 流水线中的单个步骤。

    描述一个 Agent 执行任务的定义、状态与结果。
    """

    step_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    agent_name: str = ""
    description: str = ""
    status: StepStatus = StepStatus.PENDING
    result: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    started_at: float = 0
    finished_at: float = 0

    @property
    def can_start(self) -> bool:
        """依赖步骤是否全部完成。"""
        return True  # 由 WorkflowContext 统一检查


@dataclass
class AgentContext:
    """跨 Agent 共享的上下文容器。

    在流水线执行期间，各 Agent 通过此对象传递中间结果。
    类似一个 key-value 黑板，读写的 key 通过 agreed 协议约定。
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    data: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)

    def put(self, key: str, value: Any) -> None:
        """写入共享上下文。"""
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """读取共享上下文。"""
        return self.data.get(key, default)

    def note(self, source: str, event: str, detail: Any = None) -> None:
        """记录流水线事件。"""
        self.history.append({"source": source, "event": event, "detail": detail, "ts": time.time()})

    def snapshot(self) -> dict[str, Any]:
        """返回当前上下文快照。"""
        return {
            "session_id": self.session_id,
            "data": dict(self.data),
            "history": list(self.history),
            "created_at": self.created_at,
        }
