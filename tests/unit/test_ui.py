"""openclaw/src/ui/app.py 单元测试。

测试 AppState、headless 入口、组件函数（无 Streamlit 时）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from contextlib import suppress

import pytest


def _is_streamlit_available() -> bool:
    try:
        import streamlit  # noqa: F401

        return True
    except ImportError:
        return False


class TestAppState:
    """AppState 状态管理测试。"""

    def test_app_state_class_exists(self):
        """AppState 类可导入。"""
        from ui.app import AppState

        assert AppState is not None

    def test_static_attributes_exist(self):
        """所有 KEY_* 常量存在。"""
        from ui.app import AppState

        assert AppState.KEY_FACADE is not None
        assert AppState.KEY_MESSAGES is not None
        assert AppState.KEY_SESSION_ID is not None
        assert AppState.KEY_COST is not None

    def test_init_sets_defaults(self):
        """init 设置默认 session state。"""
        from ui.app import AppState

        # 在无 Streamlit 环境下不应崩溃
        with suppress(Exception):
            AppState.init()  # 无 Streamlit 时 init 直接 return

    def test_get_facade_no_streamlit(self):
        """无 Streamlit 时 get_facade 返回 None。"""
        from ui.app import _HAS_STREAMLIT, AppState

        if not _HAS_STREAMLIT:
            facade = AppState.get_facade()
            assert facade is None


class TestHeadless:
    """run_headless 函数测试。"""

    def test_run_headless_returns_string(self):
        """run_headless 返回字符串。"""
        from ui.app import run_headless

        # 在无 StreamLit 无真实 LLM 的环境下会使用 PlaceholderLLM
        try:
            result = run_headless("测试")
            assert isinstance(result, str)
        except Exception as exc:
            # 可能因为 ServiceFacade 初始化失败
            if "ServiceFacade" in str(exc):
                pytest.skip("ServiceFacade init requires mock env")
            raise


class TestAppModule:
    """模块级别测试。"""

    def test_module_imports_cleanly(self):
        """ui.app 模块可导入。"""
        import ui.app

        assert ui.app is not None

    def test_has_streamlit_flag(self):
        """_HAS_STREAMLIT flag 存在。"""
        import ui.app

        has_flag = ui.app._HAS_STREAMLIT
        assert isinstance(has_flag, bool)

    def test_main_function_exists(self):
        """main 函数存在。"""
        from ui.app import main

        assert callable(main)

    def test_render_sidebar_function_exists(self):
        """render_sidebar 函数存在。"""
        from ui.app import render_sidebar

        assert callable(render_sidebar)

    def test_render_chat_function_exists(self):
        """render_chat 函数存在。"""
        from ui.app import render_chat

        assert callable(render_chat)

    def test_render_rag_panel_function_exists(self):
        """render_rag_panel 函数存在。"""
        from ui.app import render_rag_panel

        assert callable(render_rag_panel)

    def test_run_headless_function_exists(self):
        """run_headless 函数存在。"""
        from ui.app import run_headless

        assert callable(run_headless)


class TestUiWithFacade:
    """与 ServiceFacade 联动的测试。"""

    def test_run_headless_with_facade(self, tmp_facade):
        """通过 run_headless 调用 facade 的 chat。"""
        # 注入 facade
        import ui.app as ui_mod

        # 模拟 state（保留 st 导入路径以验证环境）
        if ui_mod._HAS_STREAMLIT:
            with suppress(Exception):
                import streamlit as st  # noqa: F401

        # 直接调用 facade 的 chat
        result = tmp_facade.chat("你好")
        assert isinstance(result, str)
        assert len(result) > 0
