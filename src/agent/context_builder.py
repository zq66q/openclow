"""上下文构建器 — 将记忆、RAG、工具 Schema 组装成 LLM 可读的 messages 列表（增强版 v2）。

增强（v2）:
  - Token 预算控制: 防止超过模型上下文窗口
  - 对话历史管理: 自动压缩旧消息
  - 多轮对话支持
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent


class ContextBuilder:
    """从零散组件组装 LLM 消息上下文。

    配置项（通过 agent 属性）:
        max_context_tokens: 上下文 token 上限（默认 4096）
        history_rounds:     保留的最近对话轮数（默认 5）
    """

    DEFAULT_MAX_CONTEXT_TOKENS = 4096
    DEFAULT_HISTORY_ROUNDS = 5
    DEFAULT_MAX_IMAGE_HISTORY = 1  # 历史消息中最多保留 N 张图片，防止多轮带图对话 token 爆炸

    @staticmethod
    def build_system_prompt(agent: BaseAgent, _query: str = "") -> str:
        parts: list[str] = [agent.system_prompt]

        memory_text = ContextBuilder.inject_memory(agent)
        if memory_text:
            parts.append(f"\n## 用户信息\n{memory_text}")

        tools_text = ContextBuilder.build_tools_prompt(agent)
        if tools_text:
            parts.append(f"\n## 可用工具\n{tools_text}")

        # 只在 agent 没有 time 相关工具时才注入当前时间，鼓励工具调用
        if not any(t in (agent.tools or []) for t in ("current_time", "datetime", "date_diff")):
            parts.append(f"\n当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(parts)

    @staticmethod
    def inject_memory(agent: BaseAgent) -> str:
        if not agent.memory:
            return ""
        try:
            return agent.memory.get_context()
        except Exception:
            return ""

    @staticmethod
    def inject_rag_context(agent: BaseAgent, query: str) -> str:
        if not agent.rag:
            return ""
        try:
            return agent.rag.query_with_context(query, top_k=3)
        except Exception as exc:
            logger.warning(f"RAG 检索失败: {exc}", extra={"query": query[:80]})
            return ""

    @staticmethod
    def build_tools_prompt(agent: BaseAgent) -> str:
        if not agent.tools:
            return ""
        tool_desc = agent.get_tool_description()
        loop_type = getattr(agent, "loop_type", "react")
        if loop_type == "function":
            # Function Calling 模式：tools schema 已通过 tools 参数传给 LLM，
            # 不需要在 system prompt 里写 ReAct 格式说明，只给简要工具列表
            return f"可用工具列表:\n{tool_desc}"
        # ReAct 模式：需要文本格式引导 LLM 调用工具
        return (
            "你可以使用以下工具来完成用户的任务。\n"
            "当需要获取实时信息（如时间、计算结果）时，必须调用对应工具，不要直接猜测。\n\n"
            "回复时请使用以下格式：\n\n"
            "Thought: 你的思考过程\n"
            "Action: 工具名\n"
            'Action Input: {"参数名": "参数值"}\n\n'
            "调用工具后你会收到 Observation，然后继续思考下一步……\n"
            "当你准备好回答用户时，使用：\n"
            "Final Answer: 你的最终回答\n\n"
            f"工具列表:\n{tool_desc}"
        )

    @staticmethod
    def build_messages(
        agent: BaseAgent,
        query: str,
        *,
        history: list[dict[str, str]] | None = None,
        image_data: str | None = None,
    ) -> list[dict[str, Any]]:
        """构建完整的 messages 列表，含对话历史和 token 预算控制。

        Args:
            agent: Agent 实例
            query: 当前用户问题
            history: 历史对话消息（可选）
            image_data: base64 编码的图片数据（data:image/xxx;base64,...），多模态时传入

        Returns:
            OpenAI 格式的消息列表（content 可以是 str 或 list[dict]）
        """
        system_prompt = ContextBuilder.build_system_prompt(agent, query)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # 注入对话历史（压缩后）
        if history:
            max_rounds = getattr(agent, "history_rounds", ContextBuilder.DEFAULT_HISTORY_ROUNDS)
            max_image_history = getattr(agent, "max_image_history", ContextBuilder.DEFAULT_MAX_IMAGE_HISTORY)
            compressed = ContextBuilder._compress_history(history, max_rounds, max_image_history)
            messages.extend(compressed)

        # 注入 RAG 上下文
        rag_context = ContextBuilder.inject_rag_context(agent, query)
        user_text = query
        if rag_context:
            user_text = f"{query}\n\n参考信息:\n{rag_context}"

        # 构建 user 消息（多模态支持）
        if image_data:
            user_message: dict[str, Any] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_data}},
                ],
            }
        else:
            user_message = {"role": "user", "content": user_text}

        messages.append(user_message)

        # Token 预算检查
        max_tokens = getattr(agent, "max_context_tokens", ContextBuilder.DEFAULT_MAX_CONTEXT_TOKENS)
        estimated_tokens = ContextBuilder._estimate_tokens(messages)

        if estimated_tokens > max_tokens:
            logger.warning(f"ContextBuilder: 预估 token ({estimated_tokens}) 超过预算 ({max_tokens}), 将截断历史消息")
            messages = ContextBuilder._truncate_to_budget(messages, max_tokens)

        return messages

    # ------------------------------------------------------------------
    # Token 预算管理
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
        """估算消息列表的 token 数（粗略：1 token ≈ 0.75 中文字符 ≈ 4 英文字符）。"""
        total_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # 多模态消息：文本部分正常算，图片部分固定算 1000 tokens
                for item in content:
                    if item.get("type") == "text":
                        total_chars += len(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        total_chars += 1000 * 4  # 约 1000 tokens
        # 中文字符占比高则 token 数接近字符数
        chinese_chars = sum(1 for c in str(messages) if "\u4e00" <= c <= "\u9fff")
        if chinese_chars > 0:
            return total_chars + chinese_chars  # 中文约 1.5-2 token/字
        return total_chars // 3  # 英文 ~4 chars/token

    @staticmethod
    def _truncate_to_budget(messages: list[dict[str, Any]], max_tokens: int) -> list[dict[str, Any]]:
        """截断消息列表到 token 预算内。优先保留 system + 最近的消息。

        多模态消息不再强制保留；当预算不足时，会尝试将其降级为纯文本（只保留文字部分）
        后再判断是否能放入预算，避免图片无限累积撑爆上下文。

        ⚠️ 当前用户的查询消息（含图片的多模态消息）绝不能降级——降级会导致用户上传的图片丢失。
        """
        result: list[dict[str, Any]] = []
        result.append(messages[0])  # system prompt

        # 从后往前保留
        remaining_budget = max_tokens - ContextBuilder._estimate_tokens([messages[0]])
        # 当前用户查询消息（messages 列表最后一个）必须保留原样，不能降级
        # 区分"最后一条 user 消息"用 role 判断；多模态 content 都是 list
        current_query_msg = messages[-1] if len(messages) > 1 else None
        for msg in reversed(messages[1:]):
            if remaining_budget <= 0:
                break
            msg_tokens = ContextBuilder._estimate_tokens([msg])
            if msg_tokens <= remaining_budget:
                result.insert(1, msg)
                remaining_budget -= msg_tokens
                continue

            # 预算不足时，如果是当前用户查询 → 强制保留（哪怕超出预算）
            # 当前查询含图片被丢弃会导致 AI "看不到" 图片
            if msg is current_query_msg:
                # 当前用户消息：永远保留完整多模态内容，不做任何降级
                result.insert(1, msg)
                # 不扣 budget（已经超出），但保留这条
                continue

            # 预算不足时，如果是历史多模态消息，尝试降级为纯文本
            is_multimodal = isinstance(msg.get("content"), list) and any(
                item.get("type") == "image_url" for item in msg.get("content", [])
            )
            if is_multimodal:
                text_msg = ContextBuilder._downgrade_multimodal_to_text(msg)
                text_tokens = ContextBuilder._estimate_tokens([text_msg])
                if text_tokens <= remaining_budget:
                    result.insert(1, text_msg)
                    remaining_budget -= text_tokens

        return result

    @staticmethod
    def _downgrade_multimodal_to_text(msg: dict[str, Any]) -> dict[str, Any]:
        """将多模态消息降级为纯文本消息，仅保留文本部分。

        用于历史图片超出限制或 token 预算不足时，避免图片 token 无限累积。
        """
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [
                item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
            ]
            text = "\n".join(text_parts).strip()
            return {**msg, "content": text or "[图片消息]"}
        return msg

    # ------------------------------------------------------------------
    # 对话历史压缩
    # ------------------------------------------------------------------

    @staticmethod
    def _compress_history(
        history: list[dict[str, str]], max_rounds: int, max_image_history: int = 1
    ) -> list[dict[str, Any]]:
        """压缩对话历史：只保留最近 N 轮，并限制历史图片数量。

        超过 max_image_history 的历史图片消息会被降级为纯文本（仅保留文字部分），
        避免多轮带图对话时图片 token 累积导致上下文爆炸。
        """
        # 计算轮数：每轮 = user + assistant
        rounds: list[list[dict[str, str]]] = []
        current_round: list[dict[str, str]] = []

        for msg in history:
            role = msg.get("role", "")
            if role == "user" and current_round:
                rounds.append(current_round)
                current_round = [msg]
            else:
                current_round.append(msg)

        if current_round:
            rounds.append(current_round)

        # 只保留最近 max_rounds 轮
        if len(rounds) > max_rounds:
            logger.debug(f"ContextBuilder: 压缩 {len(rounds)} → {max_rounds} 轮对话")
            rounds = rounds[-max_rounds:]

        # 展平，并限制历史图片数量
        flattened: list[dict[str, Any]] = []
        for r in rounds:
            flattened.extend(r)

        compressed: list[dict[str, Any]] = []
        image_count = 0
        for msg in reversed(flattened):
            is_multimodal = isinstance(msg.get("content"), list) and any(
                isinstance(item, dict) and item.get("type") == "image_url" for item in msg.get("content", [])
            )
            if is_multimodal:
                image_count += 1
                if image_count > max_image_history:
                    msg = ContextBuilder._downgrade_multimodal_to_text(msg)
                    logger.debug(f"ContextBuilder: 历史图片超过 {max_image_history} 张，降级为文本")
            compressed.insert(0, msg)

        return compressed
