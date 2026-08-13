"""Streamlit 前端界面 — OpenClaw 可视化交互控制台。

功能:
    - 侧边栏: 模型配置、场景选择、会话管理
    - 主区域: 对话界面、历史消息
    - RAG 文档上传与知识库检索
    - 成本/用量面板
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from core.logger import logger
from rag.document_parser import parse_file

# 触发 mcp_tools 下所有 @register_tool 装饰器执行，完成工具注册
import mcp_tools.tools  # noqa: F401

# ── 加载 .env ──
try:
    from dotenv import load_dotenv

    _ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
    if _ENV_FILE.exists():
        load_dotenv(str(_ENV_FILE), override=True)
except Exception:
    pass  # dotenv 可选

# ── 可选依赖检测 ──

_HAS_STREAMLIT = False
try:
    import streamlit as st
    _HAS_STREAMLIT = True
except ImportError:
    st = None  # type: ignore[assignment]


# ── 页面配置 ──

if _HAS_STREAMLIT:
    st.set_page_config(
        page_title="OpenClaw 控制台",
        page_icon="🦞",
        layout="wide",
        initial_sidebar_state="expanded",
    )


# ── 应用状态 ──

class AppState:
    """Streamlit 会话状态管理。"""

    KEY_FACADE = "_openclaw_facade"
    KEY_MESSAGES = "_openclaw_messages"
    KEY_SESSION_ID = "_openclaw_session_id"
    KEY_COST = "_openclaw_cost"

    @staticmethod
    def init() -> None:
        if not _HAS_STREAMLIT:
            return
        if AppState.KEY_MESSAGES not in st.session_state:
            st.session_state[AppState.KEY_MESSAGES] = []
        if AppState.KEY_COST not in st.session_state:
            st.session_state[AppState.KEY_COST] = {"tokens": 0, "cost": 0.0}

    @staticmethod
    def get_facade(**kwargs: Any) -> Any:
        if not _HAS_STREAMLIT:
            return None
        key = AppState.KEY_FACADE
        # 只基于影响 facade 基础设施的配置做 hash（scenario 等上层配置不应导致重建）
        _facade_keys = {"api_key", "base_url", "model"}
        _facade_cfg = {k: v for k, v in kwargs.items() if k in _facade_keys}
        cfg_hash = hash(json.dumps(_facade_cfg, sort_keys=True, default=str))
        prev_hash = st.session_state.get("_facade_cfg_hash")
        if prev_hash != cfg_hash or key not in st.session_state:
            from business.service_facade import ServiceFacade, ServiceConfig

            config = ServiceConfig.from_env()
            if kwargs.get("api_key"):
                config.llm_api_key = kwargs["api_key"]
            if kwargs.get("base_url"):
                config.llm_base_url = kwargs["base_url"]
            if kwargs.get("model"):
                config.llm_model = kwargs["model"]
            if not config.llm_api_key:
                config.llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
            facade = ServiceFacade(config)
            facade.start()
            st.session_state[key] = facade
            st.session_state["_facade_cfg_hash"] = cfg_hash
        return st.session_state[key]

    @staticmethod
    def add_message(role: str, content: str) -> None:
        if not _HAS_STREAMLIT:
            return
        st.session_state[AppState.KEY_MESSAGES].append(
            {"role": role, "content": content, "time": time.strftime("%H:%M:%S")}
        )


# ── 页面组件 ──


def render_sidebar() -> dict[str, Any]:
    """渲染侧边栏配置。

    Returns:
        用户配置字典
    """
    if not _HAS_STREAMLIT:
        return {}

    with st.sidebar:
        st.title("🦞 OpenClaw")
        st.markdown("---")

        # LLM 配置（自动读取 .env）
        st.subheader("模型配置")
        api_key = st.text_input(
            "API Key",
            value=os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            type="password",
            help="OpenAI 兼容 API Key",
        )
        base_url = st.text_input(
            "Base URL",
            value=os.getenv("LLM_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")),
            help="API 端点地址",
        )
        # 模型列表：优先 .env 中的值
        _env_model = os.getenv("LLM_MODEL", "")
        _models = ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo", "deepseek-chat", "qwen-plus"]
        if _env_model and _env_model not in _models:
            _models.insert(0, _env_model)
        model = st.selectbox("模型", _models, index=_models.index(_env_model) if _env_model in _models else 0)

        st.markdown("---")

        # 场景选择
        st.subheader("场景")
        scenario = st.selectbox(
            "选择场景",
            [
                "general_assistant",
                "rag_customer_service",
                "data_analyst",
                "code_reviewer",
                "multi_agent_collaboration",
                "master_orchestrator",
                "plan_executor",
            ],
            format_func=lambda x: {
                "general_assistant": "通用助手",
                "rag_customer_service": "知识库客服",
                "data_analyst": "数据分析师",
                "code_reviewer": "代码审查",
                "multi_agent_collaboration": "多 Agent 协作",
                "master_orchestrator": "主 Agent 编排",
                "plan_executor": "Plan-and-Execute 规划执行",
            }.get(x, x),
        )

        # 多 Agent 协作 — 选择参与专家
        selected_agents = []
        if scenario == "multi_agent_collaboration":
            st.markdown("---")
            st.subheader("🤝 选择专家")
            _agent_options = {
                "general": "通用助手",
                "data_analysis": "数据分析师",
                "code_review": "代码审查",
                "rag_qa": "知识库专家",
            }
            selected_agents = st.multiselect(
                "勾选要并行调用的 Agent",
                list(_agent_options.keys()),
                default=["general"],
                format_func=lambda x: _agent_options.get(x, x),
                help="多个 Agent 将同时处理你的问题，最终由通用助手综合汇总",
            )
            if not selected_agents:
                st.warning("请至少选择一个 Agent")
            st.caption(f"已选 {len(selected_agents)} 个专家，将并行执行后汇总")

        st.markdown("---")

        # 护栏状态
        from security.guardrails import get_guardrails
        guardrails = get_guardrails()
        stats = guardrails.get_stats()
        if stats["total_checked"] > 0:
            with st.expander("🛡️ 内容安全", expanded=False):
                col1, col2, col3 = st.columns(3)
                col1.metric("已检查", stats["total_checked"])
                col2.metric("已拦截", stats["input_blocked"], delta=None, delta_color="off")
                col3.metric("已脱敏", stats["output_masked"], delta=None, delta_color="off")
        else:
            st.caption("🛡️ 内容安全护栏已启用")

        # 会话管理
        st.subheader("会话")
        if st.button("新建会话", use_container_width=True):
            st.session_state[AppState.KEY_MESSAGES] = []
            st.session_state[AppState.KEY_SESSION_ID] = None
            st.rerun()

        st.markdown("---")

        # 成本面板
        cost_data = st.session_state.get(AppState.KEY_COST, {})
        st.subheader("用量")
        cols = st.columns(2)
        with cols[0]:
            st.metric("Token", f"{cost_data.get('tokens', 0):,}")
        with cols[1]:
            st.metric("费用", f"${cost_data.get('cost', 0):.4f}")

        # 页脚
        st.markdown("---")
        st.caption("OpenClaw v0.1.0")

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "scenario": scenario,
        "selected_agents": selected_agents,
    }


def render_chat(config: dict[str, Any]) -> None:
    """渲染对话主区域。"""
    if not _HAS_STREAMLIT:
        return

    st.title("对话")
    st.markdown("---")

    # 历史消息
    messages = st.session_state.get(AppState.KEY_MESSAGES, [])
    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            st.caption(msg.get("time", ""))

    # 展示主 Agent 编排步骤（仅主 Agent 编排模式）
    master_steps = st.session_state.get("_last_master_steps", [])
    master_tc = st.session_state.get("_last_master_tool_calls", 0)
    master_elapsed = st.session_state.get("_last_master_elapsed", 0)
    if master_steps and messages and messages[-1]["role"] == "assistant":
        with st.expander(
            f"🎯 主 Agent 编排过程 ({master_tc} 次委派, {master_elapsed:.0f}ms)", expanded=False
        ):
            for step in master_steps:
                if step.get("thought"):
                    st.markdown(f"**💭 思考**")
                    st.info(step["thought"][:300])
                if step.get("action"):
                    agent_name = step.get("action_input", {}).get("agent_name", "?")
                    task_preview = str(step.get("action_input", {}).get("task", ""))[:100]
                    st.markdown(f"**📤 委派给 `{agent_name}`**")
                    st.caption(f"任务: {task_preview}...")
                if step.get("observation"):
                    st.markdown(f"**📥 专家回复**")
                    st.success(step["observation"][:500])
                st.markdown("---")

    # 展示多 Agent 协作结果（仅多 Agent 场景）
    ma_results = st.session_state.get("_last_multi_agent_results", {})
    ma_elapsed = st.session_state.get("_last_multi_agent_elapsed", 0)
    if ma_results and messages and messages[-1]["role"] == "assistant":
        with st.expander(
            f"🔍 各专家详细分析 ({len(ma_results)} 个专家 | 并行耗时 {ma_elapsed:.0f}ms)", expanded=False
        ):
            for name, detail in ma_results.items():
                status = "✅" if detail.get("success") else "❌"
                with st.container():
                    st.markdown(
                        f"**{status} {name}** ({detail.get('elapsed_ms', 0):.0f}ms)"
                    )
                    if detail.get("success"):
                        st.markdown(detail.get("answer", ""))
                    else:
                        st.error(detail.get("error", "未知错误"))
                    st.markdown("---")

    # 输入区
    # ── 图片上传 ──
    image_data = None
    uploaded_image = st.file_uploader(
        "上传图片",
        type=["png", "jpg", "jpeg"],
        key="chat_image_uploader",
        label_visibility="collapsed",
        help="支持 PNG/JPG 格式，最大 5MB",
    )
    if uploaded_image is not None:
        import base64
        img_bytes = uploaded_image.getvalue()
        if len(img_bytes) > 5 * 1024 * 1024:
            st.warning("图片超过 5MB，已忽略")
        else:
            # 推断 MIME 类型
            mime = "image/png" if uploaded_image.name.lower().endswith(".png") else "image/jpeg"
            image_data = f"data:{mime};base64,{base64.b64encode(img_bytes).decode()}"
            st.caption(f"📎 已选择图片: {uploaded_image.name} ({len(img_bytes)//1024}KB)")

    if prompt := st.chat_input("输入消息..."):
        AppState.add_message("user", prompt)

        # 清除上次的工具调用详情（新对话开始）
        st.session_state.pop("_last_tool_steps", None)
        st.session_state.pop("_last_tool_calls_count", None)
        st.session_state.pop("_last_elapsed_ms", None)
        st.session_state.pop("_last_multi_agent_results", None)
        st.session_state.pop("_last_multi_agent_elapsed", None)

        answer = ""
        is_master = config.get("scenario") == "master_orchestrator"

        # ─�� 检索诊断（仅知识库场景，在 spinner 外展示）──
        if config.get("scenario") == "rag_customer_service":
            try:
                facade = AppState.get_facade(**config)
                pipeline = facade.rag_pipeline
                if pipeline:
                    with st.expander("🔍 检索诊断", expanded=False):
                        try:
                            results = pipeline.query(prompt, top_k=5)
                            if results:
                                st.markdown(f"**检索到 {len(results)} 个片段：**")
                                for r in results:
                                    source = r.get("metadata", {}).get("source", "unknown")
                                    score = r.get("score", 0)
                                    st.markdown(f"- **来源**: `{source}` | **相似度**: `{score:.4f}`")
                                    st.text(r.get("text", "")[:300])
                            else:
                                st.warning("未检索到任何片段")
                        except Exception as e:
                            st.error(f"检索诊断失败: {e}")
            except Exception:
                pass

        # ── 主 Agent 编排模式：流式输出，不用 spinner ──
        if is_master:
            facade = AppState.get_facade(**config)
            stream_placeholder = st.empty()
            steps_text: list[str] = []
            collected_steps: list[dict] = []
            tool_calls_count = 0
            final_answer = ""
            final_elapsed = 0
            final_token_usage: dict = {}

            for event in facade.master_agent_chat_stream(query=prompt):
                if event["type"] == "think":
                    thought = event.get("thought", "")
                    steps_text.append(
                        "💭 **思考**\n\n" + thought
                    )
                    collected_steps.append({"thought": thought})
                elif event["type"] == "delegate":
                    tool_name = event.get("tool", "")
                    task = event.get("task", "")
                    t = task[:200] + "..." if len(task) > 200 else task
                    steps_text.append(
                        f"📤 **委派给 {tool_name}**\n\n{t}"
                    )
                    if collected_steps:
                        collected_steps[-1]["action"] = tool_name
                        collected_steps[-1]["action_input"] = {
                            "agent_name": tool_name,
                            "task": task,
                        }
                elif event["type"] == "tool_result":
                    tool_name = event.get("tool", "")
                    result_text = str(event.get("result", ""))
                    ms = event.get("elapsed_ms", 0)
                    r = result_text[:300] + "..." if len(result_text) > 300 else result_text
                    steps_text.append(
                        f"📥 **{tool_name} 回复** (耗时 {ms:.0f}ms)\n\n{r}"
                    )
                    tool_calls_count += 1
                    if collected_steps:
                        collected_steps[-1]["observation"] = r
                elif event["type"] == "waiting":
                    pass  # 心跳，保持 UI 响应
                elif event["type"] == "_done":
                    final_answer = event.get("answer", "")
                    final_elapsed = event.get("elapsed_ms", 0)
                    final_token_usage = event.get("token_usage", {})
                elif event["type"] == "_error":
                    final_answer = "错误: " + event.get("error", "未知错误")

                # 实时刷新流式输出
                if steps_text:
                    stream_placeholder.markdown("\n\n---\n\n".join(steps_text))

            # 保存步骤详情供 expander 展示
            st.session_state["_last_master_steps"] = collected_steps
            st.session_state["_last_master_tool_calls"] = tool_calls_count
            st.session_state["_last_master_elapsed"] = final_elapsed

            answer = final_answer
            AppState.add_message("assistant", answer)

            # 更新用量
            cost_data = st.session_state.get(AppState.KEY_COST, {})
            cost_data["tokens"] = cost_data.get("tokens", 0) + final_token_usage.get(
                "total_tokens", 0
            )
            cost_data["cost"] = cost_data.get("cost", 0) + final_token_usage.get(
                "total_cost", 0
            )
            st.session_state[AppState.KEY_COST] = cost_data
            st.rerun()
        else:
            # 非主 Agent 模式：使用 spinner
            with st.spinner("思考中..."):
                try:
                    facade = AppState.get_facade(**config)
                    answer = facade.chat(query=prompt, image_data=image_data)
                except Exception as exc:
                    answer = f"出错了: {exc}"
                    logger.error(f"chat error: {exc}")

            AppState.add_message("assistant", answer)
            st.rerun()


# ── RAG 知识库管理面板 ──


def render_rag_panel(config: dict[str, Any]) -> None:
    """渲染 RAG 知识库管理面板（侧边栏底部）。"""
    if not _HAS_STREAMLIT:
        return

    with st.sidebar:
        st.markdown("---")
        st.subheader("📚 知识库管理")

        # 文件上传（使用 counter key 实现可靠清除）
        upload_counter = st.session_state.get("_rag_upload_counter", 0)
        upload_key = f"rag_file_uploader_{upload_counter}"
        uploaded_file = st.file_uploader(
            "上传文档",
            type=["txt", "md", "pdf", "csv", "json", "docx", "doc", "pptx", "xlsx", "html"],
            help="支持 TXT/MD/PDF/CSV/JSON/DOCX/PPTX/XLSX/HTML",
            key=upload_key,
        )
        if uploaded_file is not None:
            try:
                facade = AppState.get_facade(**config)
                pipeline = facade.rag_pipeline
                if pipeline:
                    # 写入临时文件
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=Path(uploaded_file.name).suffix  # type: ignore[arg-type]
                    ) as tmp:
                        tmp.write(uploaded_file.getvalue())  # type: ignore[union-attr]
                        tmp_path = tmp.name
                    try:
                        result = pipeline.ingest_file(tmp_path)
                        if result.get("chunks", 0) == 0:
                            st.warning(f"`{uploaded_file.name}` 已处理但未生成片段")
                        else:
                            st.success(
                                f"`{uploaded_file.name}` 已入库 (共 {result['chunks']} 个片段)"
                            )
                            # 上传成功后改变 counter，强制创建新的 file_uploader
                            st.session_state["_rag_upload_counter"] = upload_counter + 1
                            st.rerun()
                    finally:
                        Path(tmp_path).unlink(missing_ok=True)
                else:
                    st.warning("RAG 管道未初始化")
            except Exception as exc:
                st.error(f"上传失败: {exc}")

        # 文本直接入库
        st.markdown("---")
        st.markdown("**直接输入文本**")
        manual_text = st.text_area("文本内容", height=100, key="rag_manual_text")
        if manual_text and st.button("添加入库", key="rag_add_text_btn"):
            try:
                facade = AppState.get_facade(**config)
                pipeline = facade.rag_pipeline
                if pipeline:
                    result = pipeline.ingest_text(
                        manual_text, metadata={"source": "manual"}, source="manual"
                    )
                    if result.get("chunks", 0) == 0:
                        st.warning("文本已处理但未生成片段")
                    else:
                        st.success(f"已入库 (共 {result['chunks']} 个片段)")
                else:
                    st.warning("RAG 管道未初始化")
            except Exception as exc:
                st.error(f"入库失败: {exc}")

        # 显示知识库状态
        st.markdown("---")
        st.markdown("**知识库状态**")
        try:
            facade = AppState.get_facade(**config)
            pipeline = facade.rag_pipeline
            if pipeline:
                doc_count = pipeline.document_count
                st.info(f"当前知识库共有 {doc_count} 个文档片段")
                if doc_count > 0:
                    sources = pipeline.list_sources()
                    if sources:
                        st.markdown("已入库文件:")
                        for src in sources[:10]:
                            col1, col2 = st.columns([4, 1])
                            with col1:
                                st.markdown(f"- {src['source']} ({src['chunks']} 个片段)")
                            with col2:
                                btn_key = f"del_{src['source']}"
                                if st.button("删除", key=btn_key, type="secondary"):
                                    try:
                                        deleted = pipeline.remove_source(src["source"])
                                        st.success(
                                            f"已删除 {src['source']} ({deleted} 个片段)"
                                        )
                                        # 改变 counter 强制创建新的 file_uploader，防止重复上传
                                        st.session_state["_rag_upload_counter"] = st.session_state.get("_rag_upload_counter", 0) + 1
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"删除失败: {exc}")

                        # 清空全部按钮
                        st.markdown("---")
                        if st.button(
                            "清空知识库",
                            type="primary",
                            help="删除所有已入库文档",
                            key="rag_clear_all_btn",
                        ):
                            try:
                                pipeline.clear_all()
                                st.success("知识库已清空")
                                # 改变 counter 强制创建新的 file_uploader，防止重复上传
                                st.session_state["_rag_upload_counter"] = st.session_state.get("_rag_upload_counter", 0) + 1
                                st.rerun()
                            except Exception as exc:
                                st.error(f"清空失败: {exc}")
            else:
                st.warning("RAG 管道未初始化")
        except Exception as exc:
            st.error(f"无法读取知识库状态: {exc}")


# ── 主入口 ──


def main() -> None:
    """Streamlit 主入口。"""
    if not _HAS_STREAMLIT:
        print("Streamlit not installed. Install with: pip install streamlit")
        return

    AppState.init()
    config = render_sidebar()
    render_chat(config)
    render_rag_panel(config)


# ── 无 Streamlit 时的独立测试 ──


def run_headless(query: str = "你好") -> str:
    """无 UI 环境的测试入口。"""
    AppState.init()
    facade = AppState.get_facade()
    return facade.chat(query=query)


if __name__ == "__main__":
    main()
