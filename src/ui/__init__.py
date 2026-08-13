"""UI 层 — Streamlit Web 控制台。

提供:
    - AppState: 会话状态管理
    - main(): Web UI 入口
    - run_headless(): 无 Streamlit 环境的后备测试入口
"""

from __future__ import annotations

from ui.app import AppState, main, run_headless

__all__ = ["AppState", "main", "run_headless"]
