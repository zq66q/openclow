"""联网搜索工具 — 基于 Tavily AI 搜索引擎 API。

提供 web_search 工具，支持自然语言搜索互联网，返回摘要 + 结构化结果。
使用 http_tool.http_request 发送请求，无需安装额外依赖。
"""

from __future__ import annotations

import json
from typing import Any

from mcp_tools.base import Tool, ToolDangerLevel, ToolMeta
from mcp_tools.registry import register_tool
from mcp_tools.tools.http_tool import http_request

_TAVILY_URL = "https://api.tavily.com/search"


@register_tool
class WebSearchTool(Tool):
    """联网搜索工具 — 调用 Tavily API 搜索互联网信息。"""

    name = "web_search"
    description = (
        "搜索互联网获取实时信息。适用于查询最新新闻、数据、事件、产品信息等。\n"
        "返回搜索结果的摘要和多个网页的标题、链接、内容片段。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，用自然语言描述要查找的信息。例如 '2026年中国新能源汽车销量排名'。",
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认 5，最大 10。",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
            },
            "search_depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "description": "搜索深度。basic 快速搜索，advanced 深度搜索（内容更全但更慢）。默认 basic。",
                "default": "basic",
            },
        },
        "required": ["query"],
    }
    meta = ToolMeta(
        timeout=30,
        danger_level=ToolDangerLevel.SAFE,
        max_retries=2,
        tags=["search", "web", "internet"],
    )

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        query: str = kwargs["query"]
        max_results: int = kwargs.get("max_results", 5)
        search_depth: str = kwargs.get("search_depth", "basic")

        # 从 settings 获取 API Key
        from core.settings import settings

        api_key = settings.tavily_api_key
        if not api_key:
            raise RuntimeError("Tavily API Key 未配置，请在 .env 中设置 TAVILY_API_KEY")

        # 构建请求体
        payload = json.dumps(
            {
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": search_depth,
                "include_answer": True,
                "include_raw_content": False,
            }
        )

        # 发送请求
        resp = http_request(
            url=_TAVILY_URL,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=payload,
            timeout=self.meta.timeout,
        )

        if resp.get("status_code") != 200:
            error_msg = resp.get("error", "未知错误")
            body = resp.get("body", "")
            if isinstance(body, dict) and body.get("detail"):
                error_msg = body["detail"]
            raise RuntimeError(f"Tavily 搜索失败 ({resp['status_code']}): {error_msg}")

        data = resp.get("body", {})
        if not isinstance(data, dict):
            raise RuntimeError("Tavily 返回格式异常")

        # 提取并格式化结果
        answer = data.get("answer", "")
        results_raw = data.get("results", [])

        results = []
        for item in results_raw[:max_results]:
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", "")[:500],  # 截断过长的内容
                }
            )

        return {
            "query": query,
            "answer": answer,
            "results": results,
            "total_results": len(results),
        }
