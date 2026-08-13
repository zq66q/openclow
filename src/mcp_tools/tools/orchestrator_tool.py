"""主 Agent 编排工具 — 允许主 Agent 将子任务委派给专家 Agent。

提供 delegate_task 工具，主 Agent 在 ReAct 循环中调用它将子任务分派给
指定的子 Agent（如 data_analysis、code_review、researcher 等），获取结果后
自行综合出最终答案。

全局状态 _sub_agents 由 PresetAgents.master() 工厂方法注入。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from mcp_tools.base import Tool, ToolMeta, ToolDangerLevel
from mcp_tools.registry import register_tool
from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent


# ── 全局上下文（由 PresetAgents.master() 注入）──
_sub_agents: dict[str, BaseAgent] = {}
_sub_agent_descriptions: dict[str, str] = {}


def set_orchestrator_context(
    agents: dict[str, BaseAgent],
    descriptions: dict[str, str] | None = None,
) -> None:
    """由 MasterAgent 工厂注入子 Agent 列表及其描述。

    Args:
        agents: {agent_name: agent_instance} — 主 Agent 可委派的子 Agent
        descriptions: {agent_name: description} — 子 Agent 的能力描述
    """
    global _sub_agents, _sub_agent_descriptions
    _sub_agents.clear()
    _sub_agents.update(agents)
    _sub_agent_descriptions.clear()
    if descriptions:
        _sub_agent_descriptions.update(descriptions)
    logger.info(
        f"Orchestrator context set: {list(agents.keys())}",
        extra={"agent_count": len(agents)},
    )


def get_orchestrator_agent_list() -> str:
    """生成子 Agent 列表的描述文本（供 system prompt 使用）。"""
    if not _sub_agents:
        return "（无可用子 Agent）"

    # 预定义的子 Agent 能力描述
    default_descriptions = {
        "general": "通用助手 — 擅长常识问答、概念解释、文本分析、综合判断",
        "data_analysis": "数据分析师 — 擅长数值计算、趋势分析、数据报告生成",
        "code_review": "代码审查 — 擅长代码质量审查、安全分析、最佳实践建议",
        "researcher": "深度研究者 — 擅长信息搜集、多角度分析、综合研究",
        "rag_qa": "知识库专家 — 基于内部知识库的精准检索问答",
    }

    lines = []
    for name in _sub_agents:
        desc = _sub_agent_descriptions.get(name) or default_descriptions.get(name, f"{name} 专家")
        lines.append(f"- **{name}**: {desc}")
    return "\n".join(lines)


@register_tool
class DelegateTaskTool(Tool):
    """委派任务工具 — 将子任务分派给指定的子 Agent 执行。"""

    name = "delegate_task"
    description = (
        "将子任务委派给指定的专家 Agent 执行，获取该专家的分析结果。\n"
        "适用于：需要特定领域专家（如数据分析、代码审查、深度研究）参与的任务。\n"
        "每次调用只能委派给一个专家，如果任务需要多个专家，请逐一委派。\n"
        "委派后你会收到该专家的完整回答，请综合所有专家的意见给出最终结论。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": (
                    "要委派的专家 Agent 名称。可用选项: "
                    "general（通用助手）, data_analysis（数据分析师）, "
                    "code_review（代码审查）, researcher（深度研究者）"
                ),
                "enum": ["general", "data_analysis", "code_review", "researcher"],
            },
            "task": {
                "type": "string",
                "description": (
                    "要委派给该专家的具体任务描述。请用自然语言详细描述，"
                    "包括所有必要的上下文、数据和约束条件。"
                ),
            },
        },
        "required": ["agent_name", "task"],
    }
    meta = ToolMeta(
        timeout=120,  # 子 Agent 可能执行较长时间（含搜索、计算等）
        danger_level=ToolDangerLevel.SAFE,
        max_retries=1,
        tags=["orchestrator", "agent", "delegate"],
    )

    def execute(self, agent_name: str = "", task: str = "", **kwargs: Any) -> str:
        agent = _sub_agents.get(agent_name)
        if agent is None:
            available = list(_sub_agents.keys())
            return self._format_result({
                "agent_name": agent_name,
                "success": False,
                "error": (
                    f"未找到 Agent '{agent_name}'。"
                    f"当前可用的专家: {', '.join(available) if available else '无'}"
                ),
            })

        logger.info(
            f"DelegateTask: {agent_name} ← {task[:100]}",
            extra={"agent_name": agent_name},
        )

        from agent.executor import AgentExecutor

        result = AgentExecutor.run_safe(agent, task)

        if result.success:
            logger.info(
                f"DelegateTask [{agent_name}] success",
                extra={"elapsed_ms": result.total_elapsed_ms, "answer_len": len(result.answer)},
            )
        else:
            logger.warning(
                f"DelegateTask [{agent_name}] failed",
                extra={"error": result.error},
            )

        result_dict = {
            "agent_name": agent_name,
            "answer": result.answer,
            "success": result.success,
            "elapsed_ms": result.total_elapsed_ms,
            "error": result.error,
        }
        return self._format_result(result_dict)

    def _format_result(self, result_dict: dict[str, Any]) -> str:
        """将执行结果格式化为 LLM 可读的自然语言文本。"""
        if not result_dict.get("success", False):
            return f"【{result_dict.get('agent_name', 'unknown')}】委派失败: {result_dict.get('error', '未知错误')}"
        return (
            f"【{result_dict.get('agent_name', 'unknown')} 专家的回答】\n\n"
            f"{result_dict.get('answer', '（无回答）')}\n\n"
            f"（耗时 {result_dict.get('elapsed_ms', 0):.0f}ms）"
        )
