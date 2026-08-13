"""OpenClaw — 企业级多 Agent 业务自动化平台。

Layer 1-2: 基础设施层
"""

from __future__ import annotations

from core.exceptions import OpenClawBaseError
from core.logger import logger
from core.settings import Settings, settings

__version__ = "0.1.0"
__all__ = ["OpenClawBaseError", "Settings", "settings", "logger"]
