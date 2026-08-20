"""预置 Agent 工厂 — 一行代码创建常用 Agent（生产增强版 v2）。

每种预置 Agent 自动绑定合适的 tools、system_prompt、loop_type，
可选的 memory_manager 和 rag_pipeline 注入后即可直接调用 run()。

增强（v2）:
  - 修复 tool_calling() 参数 bug
  - 新增 multi_agent 和 researcher 预置
  - 支持从 dict/JSON 配置创建
  - create_all() 支持选择性创建

用法:
    agent = PresetAgents.rag_qa(rag_pipeline=pipeline)
    result = agent.run("Python 是什么？")

    # 从配置创建
    agent = PresetAgents.from_config({"type": "code_review", "llm_client": client})
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent.base import BaseAgent

if TYPE_CHECKING:
    from memory.memory_manager import MemoryManager
    from rag.pipeline import RAGPipeline


class PresetAgents:
    """预置 Agent 工厂（所有方法静态，无内部状态）。"""

    # ── 通用 ──

    @staticmethod
    def general(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
    ) -> BaseAgent:
        """通用助手 Agent — 对话聊天，无特殊能力。"""

        class GeneralAssistant(BaseAgent):
            name = "general_assistant"
            system_prompt = (
                "你是一个智能助手，可以回答各类问题、提供建议、进行对话。\n"
                "回答要准确、简洁、友好。不确定时坦诚告知。\n"
                "当需要获取实时时间或进行计算时，请主动调用对应工具。"
            )
            tools: list[str] = ["current_time", "safe_eval", "unit_convert", "web_search"]
            loop_type = "react"
            max_steps = 8

        return GeneralAssistant(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

    # ── 知识库问答 ──

    @staticmethod
    def rag_qa(
        rag_pipeline: RAGPipeline | None = None,
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
    ) -> BaseAgent:
        """知识库问答 Agent — 基于 RAG 的准确检索式回答。

        Args:
            rag_pipeline: Layer 3 RAG 检索流水线，用于文档检索
            llm_client: LLM 客户端（可选，懒加载）
            memory_manager: 记忆管理器（可选，跨会话记忆）
        """

        class RAGQAAgent(BaseAgent):
            name = "rag_qa_agent"
            system_prompt = (
                "你是一个知识库问答助手。请基于用户消息中提供的参考信息回答用户问题。\n\n"
                "规则:\n"
                "1. 优先使用参考信息中的内容\n"
                "2. 参考信息无答案时明确告知用户，不要编造\n"
                "3. 引用时标注来源\n"
                "4. 回答简洁、结构化"
            )
            tools: list[str] = ["current_time"]
            loop_type = "react"
            max_steps = 6

        return RAGQAAgent(
            llm_client=llm_client,
            memory_manager=memory_manager,
            rag_pipeline=rag_pipeline,
        )

    # ── 工具调用 ──

    @staticmethod
    def tool_calling(
        tools: list[str] | None = None,
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
    ) -> BaseAgent:
        """工具调用 Agent — 绑定全部或指定工具，Function Calling 模式。

        Args:
            tools: 工具名列表。如果为 None/空，注册全部已注册工具；
                   如果指定了具体工具列表，直接使用（不再从 MCP registry 获取）。
        """

        class ToolCallingAgent(BaseAgent):
            name = "tool_calling_agent"
            system_prompt = (
                "你是一个工具调用助手。你可以使用多种工具来帮助用户完成任务。\n"
                "当需要计算、读写文件、查询时间时，主动调用对应工具。\n"
                "调用工具后根据结果给出清晰易懂的回答。"
            )
            tools: list[str] = []
            loop_type = "react"
            max_steps = 10

        agent = ToolCallingAgent(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

        # 工具选择逻辑（修复：传入 tools 时直接使用，不覆盖）
        if tools:
            # 用户明确指定了工具列表，直接使用
            agent.tools = list(tools)
        else:
            # 未指定时，尝试获取所有已注册工具
            try:
                from mcp_tools.registry import get_registry

                agent.tools = get_registry().list_names()
            except Exception:
                agent.tools = ["datetime", "calculator"]
        return agent

    # ── 数据分析 ──

    @staticmethod
    def data_analysis(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
    ) -> BaseAgent:
        """数据分析 Agent — 适合计算、趋势分析、报告生成。"""

        class DataAnalysisAgent(BaseAgent):
            name = "data_analyst"
            system_prompt = (
                "你是一个资深数据分析师。\n\n"
                "能力:\n"
                "- 使用联网搜索工具获取实时数据和行业信息\n"
                "- 使用计算工具进行数值分析\n"
                "- 解读数据趋势和模式\n"
                "- 生成结构化的分析报告\n\n"
                "工作流程:\n"
                "1. 需要实时数据时，先用 web_search 搜索 1 次（最多 2 次）\n"
                "2. 获取数据后，使用 safe_eval 或 unit_convert 进行计算和换算\n"
                "3. 分析完成后，直接输出最终答案，不要再调用任何工具\n\n"
                "原则:\n"
                "- 不要反复搜索同一个问题\n"
                "- 搜索到结果后立即分析并给出最终回答\n"
                "- 每一步运算都用工具验证\n"
                "- 结论必须有数据支撑\n"
                "- 用清晰的结构呈现(概览→指标→解读→建议)"
            )
            tools = ["current_time", "safe_eval", "unit_convert", "web_search", "read_file"]
            loop_type = "react"
            max_steps = 8

        return DataAnalysisAgent(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

    # ── 代码审查 ──

    @staticmethod
    def code_review(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
    ) -> BaseAgent:
        """代码审查 Agent — 代码质量审查、安全分析、最佳实践建议。"""

        class CodeReviewAgent(BaseAgent):
            name = "code_reviewer"
            system_prompt = (
                "你是一个资深代码审查员。\n\n"
                "审查维度:\n"
                "1. 正确性 — 逻辑 bug、边界条件\n"
                "2. 安全性 — 注入风险、权限问题\n"
                "3. 性能 — 不必要的计算或查询\n"
                "4. 可维护性 — 命名、结构、注释\n"
                "5. 规范 — 语言/框架最佳实践\n\n"
                "工作规则（必须遵守）:\n"
                "- 当用户消息中出现文件路径时，必须先调用 read_file 工具读取该文件，"
                "再基于读到的真实内容进行审查\n"
                "- 严禁凭对话历史或记忆中的代码片段代替读取文件\n"
                "- 调用工具时必须使用规定的文本格式（Thought/Action/Action Input），"
                "不要使用 XML 标签或 function-call 语法\n"
                "- read_file 的 path 参数建议使用相对路径（如 src/ui/app.py），"
                "offset/limit 控制读取范围\n\n"
                "反馈格式: [高/中/低] 行号 — 问题描述 — 建议修改。"
            )
            tools = ["read_file"]
            loop_type = "react"
            max_steps = 8

        return CodeReviewAgent(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

    # ── 多 Agent 协调 ──

    @staticmethod
    def multi_agent(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
    ) -> BaseAgent:
        """多 Agent 协调器 — 适合复杂任务分解和结果融合。

        使用场景: 竞品分析、多维度评估、大规模项目审查。
        """

        class MultiAgentCoordinator(BaseAgent):
            name = "multi_agent_coordinator"
            system_prompt = (
                "你是一个多 Agent 协调器（Orchestrator），负责将复杂任务分解为子任务。\n\n"
                "工作流程:\n"
                "1. 分析任务复杂度，判断是否需要分解\n"
                "2. 如需分解，列出子任务列表（JSON 格式）\n"
                "3. 综合各子任务结果，给出最终答案\n\n"
                "输出格式:\n"
                "- 简单任务直接回答\n"
                "- 复杂任务输出 JSON:"
                ' {"need_decompose": true, "subtasks": [{"id": "1", "query": "...", "type": "..."}]}'
            )
            tools = ["current_time", "safe_eval", "unit_convert"]
            loop_type = "react"
            max_steps = 8

        return MultiAgentCoordinator(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

    # ── 主 Agent 编排 ──

    @staticmethod
    def master(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        rag_pipeline: RAGPipeline | None = None,
    ) -> BaseAgent:
        """主 Agent 编排器 — 接收任务后自行分解并委派给子 Agent。

        主 Agent 通过 delegate_task 工具将子任务分派给各专家，
        收集结果后综合出最终答案。本质上是"工具调用"模式的高级形态。

        使用场景: 复杂跨领域问题、需要多轮分派的复合任务。
        """
        # 1. 创建子 Agent（注入 llm_client，确保它们能正常推理）
        sub_agents = {
            "general": PresetAgents.general(llm_client, memory_manager),
            "data_analysis": PresetAgents.data_analysis(llm_client, memory_manager),
            "code_review": PresetAgents.code_review(llm_client, memory_manager),
            "researcher": PresetAgents.researcher(llm_client, memory_manager, rag_pipeline),
        }

        # 2. 注入到 delegate_task 工具的全局上下文
        from mcp_tools.tools.orchestrator_tool import (
            get_orchestrator_agent_list,
            set_orchestrator_context,
        )

        set_orchestrator_context(sub_agents)

        # 3. 动态构建 system_prompt（包含子 Agent 列表）
        agent_list_text = get_orchestrator_agent_list()

        class MasterOrchestrator(BaseAgent):
            name = "master_orchestrator"
            system_prompt = (
                "你是一个主控 Agent（Master Orchestrator），负责接收用户问题，"
                "分析后决定是否需要委派给专家 Agent，并综合各专家意见给出最终答案。\n\n"
                f"## 你可以委派的专家\n{agent_list_text}\n\n"
                "## 工作流程\n"
                "1. **分析用户问题**：判断问题类型和需要的专家领域\n"
                "2. **委派专家**：对复合问题，使用 delegate_task 工具逐一委派给合适的专家\n"
                "   - 每个专家擅长不同领域，将相关子任务委派给对应专家\n"
                "   - 可以串行委派（先问 A，基于 A 的回答再问 B）\n"
                "   - 也可以并行委派（同时问多个专家，再综合）\n"
                "3. **综合答案**：收集所有专家的回答后，综合给出完整的最终答案\n\n"
                "## 委派格式\n"
                "Action: delegate_task\n"
                'Action Input: {"agent_name": "data_analysis", "task": "计算2025年销量同比增长率"}\n\n'
                "## 重要原则\n"
                "- **必须委派的情况**：用户问题涉及多个领域（如数据计算+架构设计、代码+业务分析），"
                "或需要具体数值/实时数据支撑时，**必须先委派给对应专家，不能直接回答**\n"
                "- **不要自己猜测数据**：任何需要计算、统计、量化分析的内容，必须委派给 data_analysis\n"
                "- **不要自己审查代码**：任何涉及代码、架构、技术方案的内容，必须委派给 code_review\n"
                "- 委派时给出清晰、完整的任务描述（包括上下文和数据）\n"
                "- 每个专家最多委派 1 次，不要重复问同一个专家\n"
                "- 综合答案时要标注信息来源（由哪个专家提供的分析）\n"
                "- 总委派次数不超过 4 次，超出后必须给出最终答案\n"
                "- 只有在问题非常单一且明显属于常识（如'今天星期几'）时，才可以直接回答"
            )
            tools: list[str] = ["delegate_task", "current_time", "web_search"]
            loop_type = "react"
            max_steps = 15  # 需要更多步骤：分析→委派→观察→再委派→综合

        return MasterOrchestrator(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

    # ── 研究者 ──

    @staticmethod
    def researcher(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        rag_pipeline: RAGPipeline | None = None,
    ) -> BaseAgent:
        """深度研究者 Agent — 适合需要搜索/检索/分析的复杂研究任务。

        自动接入 RAG pipeline 做知识检索。
        """

        class ResearchAgent(BaseAgent):
            name = "researcher"
            system_prompt = (
                "你是一个深度研究者，擅长信息搜集、分析和综合。\n\n"
                "研究方法:\n"
                "1. 明确研究问题和范围\n"
                "2. 多角度搜集信息\n"
                "3. 验证信息来源的可靠性\n"
                "4. 综合不同观点形成结论\n"
                "5. 标注不确定性和局限\n\n"
                "有参考文档时优先基于文档内容回答，无文档时基于自身知识回答。"
            )
            tools = ["current_time", "safe_eval", "unit_convert", "web_search", "read_file"]
            loop_type = "react"
            max_steps = 10

        return ResearchAgent(
            llm_client=llm_client,
            memory_manager=memory_manager,
            rag_pipeline=rag_pipeline,
        )

    # ── Plan-and-Execute ──

    @staticmethod
    def plan_execute(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        rag_pipeline: RAGPipeline | None = None,
    ) -> BaseAgent:
        """Plan-and-Execute Agent — 先规划、再执行、后自检修正。

        适合需要显式多步骤规划和自我检查的复杂任务。
        工作流: Plan → Execute(逐步骤) → Review → 必要时 RePlan
        """

        class PlanExecuteAgent(BaseAgent):
            name = "plan_execute_agent"
            system_prompt = (
                "你是一个 Plan-and-Execute 型智能体，擅长处理复杂的多步骤任务。\n\n"
                "## 工作模式\n"
                "1. **规划阶段**: 将用户问题分解为清晰的步骤列表\n"
                "2. **执行阶段**: 逐一使用工具完成每个步骤\n"
                "3. **检查阶段**: 验证所有步骤是否完成，回答是否准确\n"
                "4. **修正阶段**: 如果不够完善，自动补充新步骤\n\n"
                "## 适用场景\n"
                "- 需要多步推理的复杂分析\n"
                "- 需要收集多源信息再综合的任务\n"
                "- 需要精确计算和验证的工作\n"
                "- 跨领域的问题解决\n\n"
                "## 行为准则\n"
                "- 每个步骤独立、可验证、有明确输出\n"
                "- 步骤之间可以依赖前者结果\n"
                "- 不确定时主动搜索获取信息\n"
                "- 完成所有步骤后再给出最终答案"
            )
            tools: list[str] = ["web_search", "read_file", "current_time", "safe_eval", "unit_convert"]
            loop_type = "plan_execute"
            max_steps = 20

        return PlanExecuteAgent(
            llm_client=llm_client,
            memory_manager=memory_manager,
            rag_pipeline=rag_pipeline,
        )

    # ── 批量创建 ──

    @staticmethod
    def create_all(
        rag_pipeline: RAGPipeline | None = None,
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        include: list[str] | None = None,
    ) -> dict[str, BaseAgent]:
        """创建全部预置 Agent，返回 {name: agent} 字典。

        Args:
            include: 可选，只创建指定的 Agent（名称列表），None=全部
        """
        all_creators = {
            "general": lambda: PresetAgents.general(llm_client, memory_manager),
            "rag_qa": lambda: PresetAgents.rag_qa(rag_pipeline, llm_client, memory_manager),
            "tool_calling": lambda: PresetAgents.tool_calling(None, llm_client, memory_manager),
            "data_analysis": lambda: PresetAgents.data_analysis(llm_client, memory_manager),
            "code_review": lambda: PresetAgents.code_review(llm_client, memory_manager),
            "multi_agent": lambda: PresetAgents.multi_agent(llm_client, memory_manager),
            "researcher": lambda: PresetAgents.researcher(llm_client, memory_manager, rag_pipeline),
            "master": lambda: PresetAgents.master(llm_client, memory_manager, rag_pipeline),
            "plan_execute": lambda: PresetAgents.plan_execute(llm_client, memory_manager, rag_pipeline),
        }

        if include:
            all_creators = {k: v for k, v in all_creators.items() if k in include}

        return {name: creator() for name, creator in all_creators.items()}

    # ── 配置驱动创建 ──

    @staticmethod
    def from_config(
        config: dict[str, Any],
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        rag_pipeline: RAGPipeline | None = None,
    ) -> BaseAgent:
        """从配置字典创建 Agent。

        config 字段:
            type: Agent 类型（required）— general/rag_qa/tool_calling/data_analysis/code_review/multi_agent/researcher
            tools: 工具列表（tool_calling 专用）
            system_prompt: 自定义提示词（覆盖默认）

        示例:
            PresetAgents.from_config({"type": "code_review"}, llm_client=client)
            PresetAgents.from_config({"type": "tool_calling", "tools": ["calculator", "datetime"]})
        """
        agent_type = config.get("type", "general")
        custom_tools = config.get("tools")
        custom_prompt = config.get("system_prompt")

        method = getattr(PresetAgents, agent_type, None)
        if method is None:
            raise ValueError(
                f"未知 Agent 类型: {agent_type}，支持: general, rag_qa, tool_calling, data_analysis, code_review, multi_agent, researcher, master, plan_execute"
            )

        kwargs: dict[str, Any] = {
            "llm_client": config.get("llm_client", llm_client),
            "memory_manager": config.get("memory_manager", memory_manager),
        }

        if agent_type == "rag_qa" or agent_type == "researcher" or agent_type == "plan_execute":
            kwargs["rag_pipeline"] = config.get("rag_pipeline", rag_pipeline)
        if agent_type == "tool_calling" and custom_tools is not None:
            kwargs["tools"] = custom_tools

        agent: BaseAgent = method(**kwargs)

        if custom_prompt:
            agent.system_prompt = custom_prompt

        return agent
