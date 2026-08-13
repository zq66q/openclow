"""内容安全护栏 — 输入/输出过滤引擎。

功能:
    - 输入过滤: 关键词黑名单、正则敏感模式、Prompt 注入检测
    - 输出过滤: PII 脱敏、涉政/涉黄/暴力关键词过滤
    - 统一拦截点: ServiceFacade.chat() 前后自动触发

配置方式:
    环境变量 / .env 配置 GUARDRAILS_ENABLED=true 开启
    默认启用基础规则，无需额外配置。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from core.logger import logger


# ── 默认规则库（中文 + 英文） ──

_DEFAULT_BLACKLIST = [
    # 暴力 / 恐怖主义
    "杀", "砍", "炸弹", "爆炸", "terrorist", "kill", "bomb",
    # 色情
    "色情", "porn", "裸体", "nude", "sex", "性交",
    # 政治敏感（简化示例，生产环境应接入专业 API）
    "翻墙", "vpn", "法轮功", "falun",
]

_DEFAULT_PII_PATTERNS = {
    "phone": re.compile(r"1[3-9]\d{9}|\+?\d{1,3}-?\d{7,11}"),
    "email": re.compile(r"[\w.-]+@[\w.-]+\.\w+"),
    "id_card": re.compile(r"\d{17}[\dXx]|\d{15}"),
    "bank_card": re.compile(r"\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}"),
}

_DEFAULT_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+previous\s+instructions", re.IGNORECASE),
    re.compile(r"忘记\s*.*?指令"),
    re.compile(r"you\s+are\s+now\s+.*assistant", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"role\s*:\s*", re.IGNORECASE),
    re.compile(r"忽略\s*.*?设定"),
]


# ── 数据模型 ──

@dataclass
class FilterResult:
    """单次过滤结果。"""

    allowed: bool = True
    """是否允许通过（False = 被拦截）"""

    cleaned_text: str = ""
    """清洗后的文本（脱敏/替换后）"""

    reason: str | None = None
    """拦截原因（allowed=False 时必填）"""

    matched_rules: list[str] = field(default_factory=list)
    """命中了哪些规则名"""


@dataclass
class GuardrailRule:
    """单条规则定义。"""

    name: str
    rule_type: str  # "keyword" | "regex" | "pii" | "injection"
    pattern: str | re.Pattern | list[str]
    action: str = "block"  # "block" | "mask" | "warn"
    description: str = ""
    enabled: bool = True


@dataclass
class GuardrailConfig:
    """护栏配置。"""

    enabled: bool = True
    input_enabled: bool = True
    output_enabled: bool = True

    # 输入过滤
    blacklist_keywords: list[str] = field(default_factory=list)
    injection_patterns: list[re.Pattern] = field(default_factory=list)
    input_regex_rules: list[tuple[str, re.Pattern]] = field(default_factory=list)

    # 输出过滤
    pii_patterns: dict[str, re.Pattern] = field(default_factory=dict)
    output_regex_rules: list[tuple[str, re.Pattern]] = field(default_factory=list)
    output_mask_char: str = "***"

    # 白名单（命中则跳过过滤）
    whitelist_patterns: list[re.Pattern] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> GuardrailConfig:
        """从环境变量 / .env 加载配置。"""
        enabled = os.getenv("GUARDRAILS_ENABLED", "true").lower() in ("true", "1", "yes", "on")
        input_enabled = os.getenv("GUARDRAILS_INPUT_ENABLED", "true").lower() in ("true", "1", "yes", "on")
        output_enabled = os.getenv("GUARDRAILS_OUTPUT_ENABLED", "true").lower() in ("true", "1", "yes", "on")

        cfg = cls(
            enabled=enabled,
            input_enabled=input_enabled,
            output_enabled=output_enabled,
            blacklist_keywords=_DEFAULT_BLACKLIST.copy(),
            injection_patterns=_DEFAULT_PROMPT_INJECTION_PATTERNS.copy(),
            pii_patterns=_DEFAULT_PII_PATTERNS.copy(),
            output_mask_char=os.getenv("GUARDRAILS_MASK_CHAR", "***"),
        )

        # 加载自定义黑名单（逗号分隔）
        custom_blacklist = os.getenv("GUARDRAILS_BLACKLIST", "")
        if custom_blacklist:
            cfg.blacklist_keywords.extend([k.strip() for k in custom_blacklist.split(",") if k.strip()])

        # 加载自定义正则（JSON 格式: {"rule_name": "pattern"}）
        custom_regex = os.getenv("GUARDRAILS_CUSTOM_REGEX", "")
        if custom_regex:
            try:
                rules = json.loads(custom_regex)
                for name, pattern_str in rules.items():
                    cfg.input_regex_rules.append((name, re.compile(pattern_str, re.IGNORECASE)))
                    cfg.output_regex_rules.append((name, re.compile(pattern_str, re.IGNORECASE)))
            except (json.JSONDecodeError, re.error) as exc:
                logger.warning(f"Guardrail custom regex parse failed: {exc}")

        # 加载白名单
        whitelist = os.getenv("GUARDRAILS_WHITELIST", "")
        if whitelist:
            cfg.whitelist_patterns = [re.compile(p, re.IGNORECASE) for p in whitelist.split(",") if p.strip()]

        return cfg


# ── 核心引擎 ──

class ContentGuardrails:
    """内容安全护栏引擎。

    使用方式:
        guardrails = ContentGuardrails()
        result = guardrails.filter_input(user_input)   # 输入过滤
        result = guardrails.filter_output(llm_output)  # 输出过滤
    """

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        self.config = config or GuardrailConfig.from_env()
        self._stats: dict[str, int] = {"input_blocked": 0, "output_masked": 0, "total_checked": 0}

    # ── 输入过滤 ──

    def filter_input(self, text: str) -> FilterResult:
        """输入过滤 — 在用户输入发送到 LLM 之前执行。

        检测内容:
            1. 关键词黑名单（涉黄/暴力/政治敏感）
            2. Prompt 注入攻击模式
            3. 自定义正则规则
        """
        if not self.config.enabled or not self.config.input_enabled:
            return FilterResult(allowed=True, cleaned_text=text)

        self._stats["total_checked"] += 1

        # 白名单检查（命中白名单则跳过）
        if self._is_whitelisted(text):
            return FilterResult(allowed=True, cleaned_text=text)

        matched: list[str] = []

        # 1. 关键词黑名单
        blocked_word = self._check_blacklist(text)
        if blocked_word:
            matched.append(f"blacklist:{blocked_word}")
            self._stats["input_blocked"] += 1
            return FilterResult(
                allowed=False,
                cleaned_text="",
                reason=f"输入包含敏感关键词: '{blocked_word}'",
                matched_rules=matched,
            )

        # 2. Prompt 注入检测
        injection = self._check_injection(text)
        if injection:
            matched.append(f"injection:{injection}")
            self._stats["input_blocked"] += 1
            return FilterResult(
                allowed=False,
                cleaned_text="",
                reason=f"检测到 Prompt 注入攻击: '{injection}'",
                matched_rules=matched,
            )

        # 3. 自定义正则规则
        for name, pattern in self.config.input_regex_rules:
            if pattern.search(text):
                matched.append(f"regex:{name}")
                self._stats["input_blocked"] += 1
                return FilterResult(
                    allowed=False,
                    cleaned_text="",
                    reason=f"命中自定义过滤规则: '{name}'",
                    matched_rules=matched,
                )

        return FilterResult(allowed=True, cleaned_text=text, matched_rules=matched)

    # ── 输出过滤 ──

    def filter_output(self, text: str) -> FilterResult:
        """输出过滤 — 在 LLM 返回到用户之前执行。

        处理内容:
            1. PII 脱敏（手机号、邮箱、身份证、银行卡）
            2. 自定义正则规则（如内部 IP、域名等）
        """
        if not self.config.enabled or not self.config.output_enabled:
            return FilterResult(allowed=True, cleaned_text=text)

        self._stats["total_checked"] += 1

        # 白名单检查
        if self._is_whitelisted(text):
            return FilterResult(allowed=True, cleaned_text=text)

        matched: list[str] = []
        cleaned = text
        masked = False

        # 1. PII 脱敏
        for name, pattern in self.config.pii_patterns.items():
            if pattern.search(cleaned):
                matched.append(f"pii:{name}")
                cleaned = pattern.sub(self.config.output_mask_char, cleaned)
                masked = True

        # 2. 自定义输出正则规则
        for name, pattern in self.config.output_regex_rules:
            if pattern.search(cleaned):
                matched.append(f"regex:{name}")
                cleaned = pattern.sub(self.config.output_mask_char, cleaned)
                masked = True

        if masked:
            self._stats["output_masked"] += 1
            return FilterResult(
                allowed=True,
                cleaned_text=cleaned,
                reason="部分内容已脱敏",
                matched_rules=matched,
            )

        return FilterResult(allowed=True, cleaned_text=cleaned, matched_rules=matched)

    # ── 内部工具 ──

    def _is_whitelisted(self, text: str) -> bool:
        """检查是否命中白名单（命中则跳过过滤）。"""
        for pattern in self.config.whitelist_patterns:
            if pattern.search(text):
                return True
        return False

    def _check_blacklist(self, text: str) -> str | None:
        """检查关键词黑名单，返回第一个命中的词。"""
        lower = text.lower()
        for keyword in self.config.blacklist_keywords:
            if keyword.lower() in lower:
                return keyword
        return None

    def _check_injection(self, text: str) -> str | None:
        """检查 Prompt 注入攻击模式，返回第一个命中的模式名。"""
        for i, pattern in enumerate(self.config.injection_patterns, 1):
            if pattern.search(text):
                return f"pattern_{i}"
        return None

    def get_stats(self) -> dict[str, int]:
        """获取统计信息。"""
        return dict(self._stats)

    def reset_stats(self) -> None:
        """重置统计。"""
        self._stats = {"input_blocked": 0, "output_masked": 0, "total_checked": 0}


# ── 便捷工厂 ──

_guardrails_instance: ContentGuardrails | None = None


def get_guardrails() -> ContentGuardrails:
    """获取全局单例护栏实例（懒加载）。"""
    global _guardrails_instance
    if _guardrails_instance is None:
        _guardrails_instance = ContentGuardrails()
    return _guardrails_instance
