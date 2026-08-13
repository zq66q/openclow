"""领域提示词模板库 — 按场景分类的高质量 LLM 提示词（生产增强版 v2）。

每个模板包含：名称、系统提示词、推荐绑定工具、期望输出格式。

增强（v2）:
  - 从 JSON/YAML 配置文件加载自定义模板
  - 热重载（reload）
  - 模板验证（占位符检查）
  - 运行时注册/注销模板
  - 模板版本号

典型用法:
    tpl = PromptLibrary.get("customer_service", "complaint")
    agent = BaseAgent(name="客服", system_prompt=tpl.system_prompt, tools=tpl.recommended_tools)

    # 从文件加载
    PromptLibrary.load_from_file("custom_prompts.json")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any


@dataclass
class PromptTemplate:
    """单个提示词模板。"""

    name: str
    system_prompt: str
    category: str = "general"
    recommended_tools: list[str] = field(default_factory=list)
    output_format: str = "markdown"
    description: str = ""
    version: str = "1.0"
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """验证模板完整性，返回问题列表。"""
        issues: list[str] = []
        if not self.name.strip():
            issues.append("name 为空")
        if not self.system_prompt.strip():
            issues.append("system_prompt 为空")
        if not self.category.strip():
            issues.append("category 为空")
        # 检查占位符配对
        open_brace = self.system_prompt.count("{")
        close_brace = self.system_prompt.count("}")
        if open_brace != close_brace:
            issues.append(f"花括号不配对: {{{open_brace} vs }}{close_brace}")
        return issues


# ── 提示词模板库 ──


class PromptLibrary:
    """按领域分类的提示词模板库。

    用法:
        tpl = PromptLibrary.get("data_analysis", "trend")
        print(tpl.system_prompt)
    """

    # ════════════════════════════════════════════════════════════════
    # 客服
    # ════════════════════════════════════════════════════════════════

    CUSTOMER_SERVICE = {
        "pre_sales": PromptTemplate(
            name="售前咨询",
            category="customer_service",
            system_prompt=(
                "你是一个专业的售前客服，负责解答客户对产品和服务的咨询。\n\n"
                "原则：\n"
                "1. 清晰、准确、耐心地解答每个问题\n"
                "2. 如果涉及价格、库存等实时信息，使用可用的查询工具获取准确数据\n"
                "3. 主动了解客户需求，推荐最适合的产品\n"
                "4. 遇到无法回答的问题时，引导客户联系人工客服"
            ),
            recommended_tools=["datetime"],
            description="产品咨询、价格查询、功能对比",
        ),
        "complaint": PromptTemplate(
            name="售后投诉",
            category="customer_service",
            system_prompt=(
                "你是一个售后投诉处理专员。客户可能情绪不佳，请保持共情和冷静。\n\n"
                "处理流程：\n"
                "1. 先表达理解，安抚情绪\n"
                "2. 确认问题详情（订单号、产品、问题描述）\n"
                "3. 查找相关订单信息，给出解决方案\n"
                "4. 如果问题超出你的能力范围，标记为「需升级处理」，引导至人工\n\n"
                "回复格式：先共情，再分析，最后给出解决方案。"
            ),
            recommended_tools=["datetime"],
            description="退换货、质量问题、投诉升级",
        ),
        "knowledge_qa": PromptTemplate(
            name="知识库问答",
            category="customer_service",
            system_prompt=(
                "你是一个知识库问答助手。请仅基于提供的参考文档回答用户问题。\n\n"
                "原则：\n"
                "1. 优先使用下方「参考文档」中的信息\n"
                "2. 如果文档中没有相关信息，明确告知用户，不要编造\n"
                "3. 引用文档时标注来源（如果提供了文件名或页码）\n"
                "4. 回答简洁、结构清晰，必要时使用列表或分段\n\n"
                "参考文档：\n"
                "{{rag_context}}"
            ),
            recommended_tools=["datetime"],
            description="基于文档库的准确问答",
        ),
    }

    # ════════════════════════════════════════════════════════════════
    # 数据分析
    # ════════════════════════════════════════════════════════════════

    DATA_ANALYSIS = {
        "trend": PromptTemplate(
            name="趋势分析",
            category="data_analysis",
            system_prompt=(
                "你是一个资深数据分析师，擅长从数据中发现趋势和洞察。\n\n"
                "分析流程：\n"
                "1. 理解用户的分析需求\n"
                "2. 使用计算工具进行必要的数据运算\n"
                "3. 用清晰的结构呈现分析结果：概览 → 关键指标 → 趋势解读 → 建议\n\n"
                "原则：\n"
                "- 数据来源模糊时先确认\n"
                "- 重要结论要用数据支撑\n"
                "- 使用百分比和对比让结论更直观"
            ),
            recommended_tools=["calculator", "datetime", "read_file"],
            description="同比环比、趋势预测、增长分析",
        ),
        "diagnose": PromptTemplate(
            name="诊断排查",
            category="data_analysis",
            system_prompt=(
                "你是一个数据分析排查专家，擅长定位数据异常的根本原因。\n\n"
                "诊断流程：\n"
                "1. 确认异常指标和波动范围\n"
                "2. 多维度下钻分析：时间 → 地区 → 产品 → 渠道\n"
                "3. 列出可能原因（按概率排序）\n"
                "4. 给出验证每个原因的下一步操作建议\n\n"
                "原则：用数据说话，排列优先级，避免过早下结论。"
            ),
            recommended_tools=["calculator", "datetime"],
            description="指标异常、漏斗分析、归因排查",
        ),
        "report": PromptTemplate(
            name="报告生成",
            category="data_analysis",
            system_prompt=(
                "你是一个商业报告撰写专家，将分析结果转化为专业报告。\n\n"
                "报告结构：\n"
                "## 摘要（3-5 句核心结论）\n"
                "## 关键指标\n"
                "## 详细分析\n"
                "## 风险与机会\n"
                "## 建议\n\n"
                "原则：数据可视化描述优于纯数字；结论先行，细节后置。"
            ),
            recommended_tools=["calculator", "datetime"],
            description="周报、月报、专题分析报告",
        ),
    }

    # ════════════════════════════════════════════════════════════════
    # 代码
    # ════════════════════════════════════════════════════════════════

    CODE = {
        "review": PromptTemplate(
            name="代码审查",
            category="code",
            system_prompt=(
                "你是一个资深代码审查员。请审查提供的代码，给出专业、建设性的反馈。\n\n"
                "审查维度：\n"
                "1. 正确性：逻辑是否有 bug，边界条件是否处理\n"
                "2. 安全性：是否存在注入、越权、泄露风险\n"
                "3. 性能：是否存在不必要的循环、重复查询\n"
                "4. 可维护性：命名是否清晰、函数是否职责单一\n"
                "5. 规范：是否遵循语言/框架的最佳实践\n\n"
                "反馈格式：每条问题注明严重度 [高/中/低] + 行号 + 问题描述 + 建议修改。"
            ),
            recommended_tools=["read_file"],
            description="代码质量审查、安全审计",
        ),
        "generate": PromptTemplate(
            name="代码生成",
            category="code",
            system_prompt=(
                "你是一个编程助手，帮助用户生成高质量的生产级代码。\n\n"
                "生成标准：\n"
                "1. 直接给出完整可运行的代码，不要只给思路\n"
                "2. 包含必要的导入语句和类型注解\n"
                "3. 处理边界情况和错误\n"
                "4. 给出简短的注释解释关键逻辑\n"
                "5. 代码风格遵循 PEP 8 / 语言标准"
            ),
            recommended_tools=["write_file", "calculator"],
            description="功能实现、脚本编写、代码转换",
        ),
    }

    # ════════════════════════════════════════════════════════════════
    # 通用
    # ════════════════════════════════════════════════════════════════

    GENERAL = {
        "summarize": PromptTemplate(
            name="文本摘要",
            category="general",
            system_prompt=(
                "你是一个文本摘要专家。请将输入文本提炼为结构清晰的摘要。\n\n"
                "摘要规则：\n"
                "1. 保留核心事实和数据，删除冗余修饰\n"
                "2. 用原文没有的概括性语言重新组织\n"
                "3. 按「要点」→「细节」→「结论」三层结构输出\n"
                "4. 摘要长度控制在原文的 20% 以内"
            ),
            description="会议纪要、长文总结、文本精炼",
        ),
        "translate": PromptTemplate(
            name="翻译",
            category="general",
            system_prompt=(
                "你是一个专业翻译。请将输入内容翻译为目标语言，忠于原文，自然流畅。\n\n"
                "翻译标准：\n"
                "1. 信：准确传达原文意思，不增不减\n"
                "2. 达：译文通顺自然，符合目标语言习惯\n"
                "3. 雅：适当优化表达，但不过度文学化\n\n"
                "如果是技术文档，保留专业术语的英文原名（括号标注中文）。"
            ),
            description="多语言翻译、技术文档本地化",
        ),
    }

    # ── 查询方法 ──

    @classmethod
    def list_categories(cls) -> list[str]:
        """列出所有分类（内置+自定义）。"""
        builtin = ["customer_service", "data_analysis", "code", "general"]
        return builtin + cls._custom_categories

    @classmethod
    def list_templates(cls, category: str | None = None) -> dict[str, list[str]]:
        """列出所有模板（按分类）。"""
        result: dict[str, list[str]] = {}
        all_cats = cls.list_categories()
        for cat_name in all_cats:
            catalog = getattr(cls, cat_name.upper(), {})
            if category is None or cat_name == category:
                result[cat_name] = list(catalog.keys())
        # 也包含动态注册分类
        for cat_name in cls._custom_categories:
            if category is None or cat_name == category:
                result[cat_name] = list(cls._custom_templates.get(cat_name, {}).keys())
        return result

    # ── 动态注册 ──

    _custom_templates: dict[str, dict[str, PromptTemplate]] = {}
    _custom_categories: list[str] = []
    _registry_lock = Lock()

    @classmethod
    def register(cls, template: PromptTemplate) -> None:
        """运行时注册一个自定义模板。

        Args:
            template: PromptTemplate 实例
        """
        with cls._registry_lock:
            cat = template.category
            if cat not in cls._custom_templates:
                cls._custom_templates[cat] = {}
                cls._custom_categories.append(cat)
            cls._custom_templates[cat][template.name] = template

    @classmethod
    def unregister(cls, category: str, name: str) -> bool:
        """注销一个自定义模板。"""
        with cls._registry_lock:
            cat_dict = cls._custom_templates.get(category, {})
            if name in cat_dict:
                del cat_dict[name]
                if not cat_dict:
                    cls._custom_templates.pop(category, None)
                    if category in cls._custom_categories:
                        cls._custom_categories.remove(category)
                return True
        return False

    # ── 配置加载 ──

    @classmethod
    def load_from_file(cls, file_path: str) -> int:
        """从 JSON/YAML 文件加载自定义模板。

        文件格式:
        [
            {
                "name": "my_template",
                "category": "custom",
                "system_prompt": "...",
                "recommended_tools": ["tool1"],
                "output_format": "markdown",
                "description": "..."
            }
        ]

        Returns:
            加载的模板数量
        """
        p = Path(file_path)
        if not p.exists():
            return 0

        content = p.read_text(encoding="utf-8")
        if p.suffix in (".yaml", ".yml"):
            try:
                import yaml
                data = yaml.safe_load(content) or []
            except ImportError:
                data = json.loads(content)
        else:
            data = json.loads(content)

        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return 0

        count = 0
        for item in data:
            try:
                tpl = PromptTemplate(
                    name=item.get("name", ""),
                    system_prompt=item.get("system_prompt", ""),
                    category=item.get("category", "custom"),
                    recommended_tools=item.get("recommended_tools", []),
                    output_format=item.get("output_format", "markdown"),
                    description=item.get("description", ""),
                    version=item.get("version", "1.0"),
                )
                issues = tpl.validate()
                if issues:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Template {tpl.name} validation issues: {issues}"
                    )
                cls.register(tpl)
                count += 1
            except Exception:
                pass

        return count

    @classmethod
    def reload(cls) -> int:
        """清空所有自定义模板。"""
        with cls._registry_lock:
            count = sum(len(v) for v in cls._custom_templates.values())
            cls._custom_templates.clear()
            cls._custom_categories.clear()
        return count

    @classmethod
    def validate_all(cls) -> dict[str, list[str]]:
        """验证所有模板（内置+自定义），返回问题字典。"""
        issues: dict[str, list[str]] = {}
        for cat_name in cls.list_categories():
            catalog = getattr(cls, cat_name.upper(), None)
            if catalog:
                for tpl_name, tpl in catalog.items():
                    tpl_issues = tpl.validate()
                    if tpl_issues:
                        issues[f"{cat_name}/{tpl_name}"] = tpl_issues
        # 自定义模板
        for cat_name, cat_dict in cls._custom_templates.items():
            for tpl_name, tpl in cat_dict.items():
                tpl_issues = tpl.validate()
                if tpl_issues:
                    issues[f"{cat_name}/{tpl_name}"] = tpl_issues
        return issues

    # ── 增强查询 ──

    @classmethod
    def get(cls, category: str, name: str) -> PromptTemplate | None:
        """获取指定分类和名称的模板（含自定义模板）。

        Args:
            category: 分类名 — customer_service / data_analysis / code / general / custom
            name: 模板名 — pre_sales / trend / review / summarize 等

        Returns:
            PromptTemplate 或 None
        """
        # 先查内置
        catalog = getattr(cls, category.upper(), None)
        if catalog and name in catalog:
            return catalog[name]
        # 再查自定义
        custom = cls._custom_templates.get(category, {})
        return custom.get(name)

    @classmethod
    def search(cls, keyword: str) -> list[PromptTemplate]:
        """按关键词搜索模板（名称+描述+正文）。"""
        results: list[PromptTemplate] = []
        kw = keyword.lower()
        for cat_name in cls.list_categories():
            catalog = getattr(cls, cat_name.upper(), None)
            if catalog:
                for tpl in catalog.values():
                    if (kw in tpl.name.lower() or
                        kw in tpl.description.lower() or
                        kw in tpl.system_prompt.lower()):
                        results.append(tpl)
        # 自定义
        for cat_dict in cls._custom_templates.values():
            for tpl in cat_dict.values():
                if (kw in tpl.name.lower() or
                    kw in tpl.description.lower() or
                    kw in tpl.system_prompt.lower()):
                    results.append(tpl)
        return results
