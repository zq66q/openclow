"""工具注册中心 + @register_tool 装饰器。

自动发现、注册、查询 Tool 子类。
对外暴露 get_tools_schema() 生成 OpenAI Function Calling 格式列表。
"""

from __future__ import annotations

from typing import Any

from core.logger import logger
from mcp_tools.base import Tool


class ToolRegistry:
    """全局工具注册中心（单例模式）。"""

    def __init__(self) -> None:
        self._tools: dict[str, type[Tool]] = {}

    # ---- 注册 ----

    def register(self, tool_cls: type[Tool]) -> type[Tool]:
        """注册一个 Tool 子类。"""
        if not tool_cls.name:
            raise ValueError(f"Tool 子类 {tool_cls.__name__} 必须设置 name 属性")
        if tool_cls.name in self._tools:
            logger.warning(f"Tool [{tool_cls.name}] 重复注册，覆盖旧定义")
        self._tools[tool_cls.name] = tool_cls
        logger.info(f"Tool registered: {tool_cls.name}")
        return tool_cls

    # ---- 查询 ----

    def get(self, name: str) -> Tool | None:
        """按名称获取工具实例（新建实例，非缓存）。"""
        cls = self._tools.get(name)
        if cls is None:
            return None
        return cls()

    def get_cls(self, name: str) -> type[Tool] | None:
        """按名称获取工具类（不实例化）。"""
        return self._tools.get(name)

    def list_tools(self) -> list[type[Tool]]:
        """返回所有已注册的 Tool 类列表。"""
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        """返回所有已注册的工具名列表。"""
        return list(self._tools.keys())

    # ---- OpenAI Function Calling Schema ----

    def get_tools_schema(self) -> list[dict[str, Any]]:
        """生成标准 OpenAI Function Calling 格式的工具定义列表。

        可直接传给 LLM 的 tools 参数：
            client.chat.completions.create(..., tools=registry.get_tools_schema())
        """
        schemas: list[dict[str, Any]] = []
        for tool_cls in self._tools.values():
            # 实例化一次以获取完整的 schema（parameters 可能在 __init__ 后动态生成）
            instance = tool_cls()
            schemas.append(instance.to_openai_schema())
        return schemas

    def get_tool_meta(self, name: str) -> dict[str, Any] | None:
        """获取指定工具的元数据摘要。"""
        cls = self._tools.get(name)
        if cls is None:
            return None
        return {
            "name": cls.name,
            "description": cls.description,
            "danger_level": cls.meta.danger_level.value,
            "timeout": cls.meta.timeout,
            "need_approval": cls.meta.need_approval,
            "tags": cls.meta.tags,
        }

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ------------------------------------------------------------------------------
# 全局单例 + 便捷函数
# ------------------------------------------------------------------------------

_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    """获取全局工具注册中心。"""
    return _registry


def register_tool(cls: type[Tool]) -> type[Tool]:
    """装饰器：注册 Tool 子类到全局注册中心。

    用法:
        @register_tool
        class DateTimeTool(Tool):
            name = "datetime"
            ...
    """
    _registry.register(cls)
    return cls


def get_tool(name: str) -> Tool | None:
    """便捷函数：按名称获取工具实例。"""
    return _registry.get(name)


def get_tools_schema() -> list[dict[str, Any]]:
    """便捷函数：获取所有工具的 OpenAI Schema。"""
    return _registry.get_tools_schema()
