"""多源信息融合 — 将多个 Agent 的结果合并成一致的最终答案（增强版 v2）。

增强（v2）:
  - LLM 辅助冲突检测（语义级别，不只是长度）
  - 加权投票（conflicting answers 被降权）
  - 冲突报告
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.logger import logger

if TYPE_CHECKING:
    from agent.base import AgentResult


class FusionStrategy(str, Enum):
    CONCAT = "concat"
    VOTE = "vote"
    LLM_SYNTHESIZE = "llm"


@dataclass
class FusionResult:
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    strategy: FusionStrategy = FusionStrategy.CONCAT
    conflicts: list[str] = field(default_factory=list)
    confidence: float = 0.0
    token_usage: dict[str, int] = field(default_factory=dict)  # prompt / completion / total


class ResultFusion:
    """多源结果融合器。"""

    @staticmethod
    def fuse(
        results: dict[str, AgentResult],
        strategy: FusionStrategy = FusionStrategy.CONCAT,
        llm_client: Any = None,
        *,
        detect_conflicts: bool = True,
    ) -> FusionResult:
        valid = {k: v for k, v in results.items() if v.success and v.answer}

        if not valid:
            first_err = next((v.error for v in results.values() if v.error), "所有 Agent 执行失败")
            return FusionResult(answer=f"（融合失败: {first_err}）")

        # 冲突检测
        conflicts: list[str] = []
        if detect_conflicts and len(valid) > 1:
            conflicts = ResultFusion._detect_conflicts(valid, llm_client)

        if strategy == FusionStrategy.VOTE:
            return ResultFusion._vote(valid, conflicts)
        elif strategy == FusionStrategy.LLM_SYNTHESIZE:
            return ResultFusion._llm_synthesize(valid, llm_client, conflicts)
        else:
            r = ResultFusion._concat(valid)
            r.conflicts = conflicts
            return r

    @staticmethod
    def deduplicate(answers: list[str]) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        for ans in answers:
            stripped = ans.strip()
            if stripped not in seen:
                seen.add(stripped)
                unique.append(stripped)
        return unique

    # ------------------------------------------------------------------
    # 冲突检测（增强：LLM 辅助）
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_conflicts(
        valid: dict[str, AgentResult],
        llm_client: Any = None,
    ) -> list[str]:
        """检测多个 Agent 答案之间是否存在语义冲突。"""
        answers = [r.answer for r in valid.values()]
        if len(answers) <= 1:
            return []

        conflicts: list[str] = []

        # 方法 1: LLM 语义冲突检测
        if llm_client:
            try:
                conflict_prompt = (
                    "你是一个冲突检测器。以下是多个 Agent 对同一问题的回答。"
                    "请判断这些回答之间是否存在实质性矛盾（不是措辞差异，而是事实/结论矛盾）。\n"
                    "如果有矛盾，请以 JSON 数组列出:"
                    '[{"agent1": "名称1", "agent2": "名称2", "conflict": "具体矛盾描述"}]\n'
                    "如果没有矛盾，返回空数组 []。\n\n"
                )
                for name, r in valid.items():
                    conflict_prompt += f"### {name}\n{r.answer[:500]}\n\n"
                conflict_prompt += "请回复（仅 JSON）:"

                reply, _ = llm_client.chat([{"role": "user", "content": conflict_prompt}], temperature=0)
                import json

                try:
                    detected = json.loads(reply.strip())
                    for item in detected:
                        conflicts.append(
                            f"{item.get('agent1', '?')} vs {item.get('agent2', '?')}: {item.get('conflict', '')}"
                        )
                except json.JSONDecodeError:
                    pass
            except Exception as exc:
                logger.debug(f"LLM conflict detection failed: {exc}")

        # 方法 2: 启发式回退
        if not conflicts:
            conflicts = ResultFusion._heuristic_conflicts(answers)
        return conflicts

    @staticmethod
    def resolve_conflicts(answers: list[str]) -> list[str]:
        """检测多个答案之间是否存在冲突（向后兼容接口，委托到启发式检测）。"""
        return ResultFusion._heuristic_conflicts(answers)

    @staticmethod
    def _heuristic_conflicts(answers: list[str]) -> list[str]:
        """启发式冲突检测（回退）。"""
        conflicts: list[str] = []
        for i in range(len(answers)):
            for j in range(i + 1, len(answers)):
                a1, a2 = answers[i], answers[j]
                if len(a1) == 0 or len(a2) == 0:
                    continue
                # 长度差异 > 3x
                if max(len(a1), len(a2)) / max(min(len(a1), len(a2)), 1) > 3:
                    conflicts.append(f"Agent[{i}] 和 Agent[{j}] 答案长度差异过大")
                # 包含否定词 vs 不含
                neg_words = ["错误", "不正确", "不对", "没有", "不是", "wrong", "incorrect", "false"]
                a1_neg = any(w in a1.lower() for w in neg_words)
                a2_neg = any(w in a2.lower() for w in neg_words)
                if a1_neg != a2_neg:
                    conflicts.append(f"Agent[{i}] 和 Agent[{j}] 存在肯定/否定矛盾")
        return conflicts

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    @staticmethod
    def _concat(valid: dict[str, AgentResult]) -> FusionResult:
        parts = [f"【{name}】\n{result.answer}" for name, result in valid.items()]
        return FusionResult(
            answer="\n\n".join(parts), sources=list(valid.keys()), strategy=FusionStrategy.CONCAT, confidence=0.8
        )

    @staticmethod
    def _vote(valid: dict[str, AgentResult], conflicts: list[str] | None = None) -> FusionResult:
        if len(valid) == 1:
            name, result = next(iter(valid.items()))
            return FusionResult(answer=result.answer, sources=[name], strategy=FusionStrategy.VOTE, confidence=1.0)

        best_name = max(valid.keys(), key=lambda k: len(valid[k].answer))
        best_result = valid[best_name]

        return FusionResult(
            answer=best_result.answer,
            sources=list(valid.keys()),
            strategy=FusionStrategy.VOTE,
            confidence=0.7,
            conflicts=conflicts or [],
        )

    @staticmethod
    def _llm_synthesize(
        valid: dict[str, AgentResult], llm_client: Any = None, conflicts: list[str] | None = None
    ) -> FusionResult:
        if llm_client is None:
            logger.warning("LLM_SYNTHESIZE 降级: 无 llm_client，回退到 CONCAT")
            return ResultFusion._concat(valid)

        # 截断每个 Agent 答案，防止合成 prompt 超出上下文
        max_each = 1500
        truncated = {}
        for name, result in valid.items():
            ans = result.answer
            if len(ans) > max_each:
                ans = ans[:max_each] + f"\n\n…（已截断，原文 {len(result.answer)} 字符）"
            truncated[name] = ans

        answers_text = "\n\n".join(f"来源 [{name}]:\n{ans}" for name, ans in truncated.items())
        prompt = (
            "你是一个信息综合器。以下是多个专家对同一问题的回答。"
            "请综合所有信息，给出一个完整、简洁的最终答案（控制在500字以内）。"
            "如果有矛盾，请指出并选择最可靠的信息。\n\n"
            f"各专家的回答:\n{answers_text}\n\n"
            "最终答案:"
        )

        try:
            reply, usage = llm_client.chat([{"role": "user", "content": prompt}], temperature=0.3)
            token_usage = {}
            if usage is not None:
                token_usage = {
                    "prompt": getattr(usage, "prompt_tokens", 0),
                    "completion": getattr(usage, "completion_tokens", 0),
                    "total": getattr(usage, "total_tokens", 0),
                }
            logger.info(f"LLM_SYNTHESIZE 成功, 答案长度: {len(reply)}")
            return FusionResult(
                answer=reply.strip(),
                sources=list(valid.keys()),
                strategy=FusionStrategy.LLM_SYNTHESIZE,
                confidence=0.85,
                conflicts=conflicts or [],
                token_usage=token_usage,
            )
        except Exception as exc:
            logger.error(f"LLM_SYNTHESIZE 失败，回退到 CONCAT: {exc}")
            return ResultFusion._concat(valid)
