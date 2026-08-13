"""文档切片器 — 多模式分块，保持语义完整性。

分块器类型：
  TextChunker      — 通用文本（段落 → 句子 → 滑动窗口）
  MarkdownChunker  — Markdown 文档（按标题层级分块，带 heading path）
  CodeChunker      — 代码文件（按函数/类边界分块，AST 提取）

所有分块器返回统一的 Chunk 列表，可混用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.settings import settings


# 句子结束标点（中英文）
_SENTENCE_END = re.compile(r"(?<=[。！？.!?\n])\s*")

# Markdown 标题匹配
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# 代码块边界（```...```）
_MD_CODE_BLOCK = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)


@dataclass
class Chunk:
    """文档片段。"""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    start_pos: int = 0
    end_pos: int = 0


# ====================================================================
# TextChunker — 通用文本分块（优化版，修复 O(n²)）
# ====================================================================

class TextChunker:
    """通用文本切片器。

    用法：
        chunker = TextChunker(chunk_size=512, chunk_overlap=128)
        chunks = chunker.split(text, metadata={"source": "report.pdf"})
    """

    def __init__(
        self,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> None:
        self.chunk_size = chunk_size or settings.rag.chunk_size
        self.chunk_overlap = chunk_overlap or settings.rag.chunk_overlap

        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"chunk_overlap ({self.chunk_overlap}) 必须小于 chunk_size ({self.chunk_size})"
            )

    def split(self, text: str, metadata: dict[str, Any] | None = None) -> list[Chunk]:
        """切分文本为语义片段。"""
        if not text.strip():
            return []

        base_meta = dict(metadata or {})

        # Step 1: 按段落切分
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

        # Step 2: 段落内按句子切分
        sentences: list[str] = []
        for para in paragraphs:
            parts = _SENTENCE_END.split(para)
            parts = [s.strip() for s in parts if s.strip()]
            sentences.extend(parts)

        if not sentences:
            return []

        # Step 3: 滑动窗口合并句子（使用字符位置追踪，避免 O(n²) text.index()）
        chunks: list[Chunk] = []
        current: list[str] = []
        current_len = 0
        current_start = 0
        pos = 0  # 当前累积字符位置

        for sent in sentences:
            sent_len = len(sent)
            sent_start = pos
            sent_end = pos + sent_len

            if current_len + sent_len > self.chunk_size and current:
                content = "".join(current)
                chunks.append(Chunk(
                    content=content,
                    metadata=dict(base_meta, chunk_index=len(chunks)),
                    start_pos=current_start,
                    end_pos=sent_start,
                ))

                # 重叠：保留最后 overlap 长度的字符
                overlap_chars = 0
                overlap_sents: list[str] = []
                for s in reversed(current):
                    if overlap_chars + len(s) <= self.chunk_overlap:
                        overlap_sents.insert(0, s)
                        overlap_chars += len(s)
                    else:
                        break

                current = overlap_sents
                current_len = overlap_chars
                current_start = sent_start - current_len if current else sent_start

            current.append(sent)
            current_len += sent_len
            if current_len == sent_len:
                current_start = sent_start

            pos = sent_end

        # 最后一段
        if current:
            content = "".join(current)
            chunks.append(Chunk(
                content=content,
                metadata=dict(base_meta, chunk_index=len(chunks)),
                start_pos=current_start,
                end_pos=len(text),
            ))

        return chunks


# ====================================================================
# MarkdownChunker — 按标题层级分块
# ====================================================================

class MarkdownChunker:
    """Markdown 文档分块器。

    策略：
    1. 按 # 标题分割为 section
    2. 每个 section 作为独立 chunk（除非超过 chunk_size）
    3. 超过 chunk_size 的 section 回退到 TextChunker
    4. metadata 包含 heading_path（如 "## 使用方法 > ### 安装"）

    用法：
        chunker = MarkdownChunker(chunk_size=512)
        chunks = chunker.split(markdown_text, metadata={"source": "README.md"})
    """

    def __init__(self, chunk_size: int | None = None) -> None:
        self.chunk_size = chunk_size or settings.rag.chunk_size
        self._text_chunker = TextChunker(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_size // 4,
        )

    def split(self, text: str, metadata: dict[str, Any] | None = None) -> list[Chunk]:
        """按 Markdown 标题层级分块。"""
        if not text.strip():
            return []

        base_meta = dict(metadata or {})

        # 按标题分割
        sections = self._split_by_headings(text)
        if len(sections) <= 1:
            # 无标题结构，回退到文本分块
            return self._text_chunker.split(text, metadata=base_meta)

        chunks: list[Chunk] = []
        pos = 0

        for heading_path, section_text in sections:
            section_len = len(section_text)
            section_start = pos
            section_end = pos + section_len
            pos = section_end

            section_meta = dict(base_meta, heading_path=heading_path)

            if section_len <= self.chunk_size:
                # 直接作为一个 chunk
                chunks.append(Chunk(
                    content=section_text,
                    metadata=dict(section_meta, chunk_index=len(chunks)),
                    start_pos=section_start,
                    end_pos=section_end,
                ))
            else:
                # 超长 section，回退到文本分块（保留 heading path）
                sub_chunks = self._text_chunker.split(section_text, metadata=section_meta)
                for sc in sub_chunks:
                    sc.metadata["chunk_index"] = len(chunks)
                    sc.start_pos += section_start
                    sc.end_pos += section_start
                    chunks.append(sc)

        return chunks

    @staticmethod
    def _split_by_headings(text: str) -> list[tuple[str, str]]:
        """按标题分割 Markdown 文本，返回 [(heading_path, section_text), ...]"""
        # 提取所有标题位置
        headings: list[tuple[int, int, str]] = []  # [(start, level, text), ...]
        for m in _MD_HEADING.finditer(text):
            level = len(m.group(1))
            heading_text = m.group(2).strip()
            headings.append((m.start(), level, heading_text))

        if not headings:
            return [("", text)]

        sections: list[tuple[str, str]] = []

        # 第一个 section：标题之前的内容
        if headings[0][0] > 0:
            sections.append(("", text[:headings[0][0]].strip()))

        # 构建 heading path
        path_stack: list[tuple[int, str]] = []  # [(level, heading_text), ...]

        for idx, (start, level, heading_text) in enumerate(headings):
            # 更新 path_stack
            while path_stack and path_stack[-1][0] >= level:
                path_stack.pop()
            path_stack.append((level, heading_text))
            heading_path = " > ".join(h[1] for h in path_stack)

            # section 结束位置
            if idx + 1 < len(headings):
                end = headings[idx + 1][0]
            else:
                end = len(text)

            # 提取 section 全文（包含标题行）
            section_text = text[start:end].strip()
            if section_text:
                sections.append((heading_path, section_text))

        return sections


# ====================================================================
# CodeChunker — 代码文件分块
# ====================================================================

class CodeChunker:
    """代码文件分块器。

    策略：
    1. 尝试 AST 分块（按函数/类边界），Python 优先
    2. 回退到行级滑动窗口（通用语言）
    3. 每块包含前后上下文行（overlap_lines）

    用法：
        chunker = CodeChunker(chunk_size=512)
        chunks = chunker.split(code_text, language="python", metadata={"source": "main.py"})
    """

    # 常见语言的函数定义行正则
    _FUNC_PATTERNS: dict[str, re.Pattern] = {
        "python": re.compile(
            r"^(def\s+\w+|class\s+\w+|async\s+def\s+\w+)", re.MULTILINE
        ),
        "javascript": re.compile(
            r"^(function\s+\w+|class\s+\w+|const\s+\w+\s*=\s*(?:async\s*)?\(|let\s+\w+\s*=\s*(?:async\s*)?\()", re.MULTILINE
        ),
        "typescript": re.compile(
            r"^(function\s+\w+|class\s+\w+|const\s+\w+\s*=\s*(?:async\s*)?\(|let\s+\w+\s*=\s*(?:async\s*)?\(|interface\s+\w+|type\s+\w+)", re.MULTILINE
        ),
        "go": re.compile(r"^(func\s+|type\s+\w+\s+struct)", re.MULTILINE),
        "java": re.compile(
            r"^\s*(public|private|protected|static|\s)*\s*(class|interface|enum)\s+\w+|^\s*(public|private|protected|static|\s)*\s*\w+\s+\w+\s*\(", re.MULTILINE
        ),
        "rust": re.compile(r"^(fn\s+\w+|struct\s+\w+|impl\s+|trait\s+\w+)", re.MULTILINE),
        "cpp": re.compile(
            r"^\s*\w+\s+\w+\s*\(|^\s*class\s+\w+|^\s*struct\s+\w+", re.MULTILINE
        ),
    }

    def __init__(
        self,
        chunk_size: int | None = None,
        overlap_lines: int = 3,
    ) -> None:
        self.chunk_size = chunk_size or settings.rag.chunk_size
        self.overlap_lines = overlap_lines

    def split(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        language: str = "",
    ) -> list[Chunk]:
        """分块代码文件。"""
        if not text.strip():
            return []

        base_meta = dict(metadata or {}, language=language)
        lines = text.split("\n")

        # AST 分块（Python 优先）— 即使文件很小也尝试按函数拆分
        if language in ("python", "py") or (not language and metadata and metadata.get("source", "").endswith(".py")):
            ast_chunks = self._ast_split_python(text, base_meta)
            if ast_chunks:
                return ast_chunks

        # 正则按函数边界分块
        if language in self._FUNC_PATTERNS:
            func_chunks = self._func_split(text, lines, language, base_meta)
            if func_chunks and len(func_chunks) > 1:
                return func_chunks

        # 如果文件太小且无结构边界，直接单块
        if len(text) <= self.chunk_size:
            return [Chunk(
                content=text,
                metadata=dict(base_meta, chunk_index=0),
                start_pos=0,
                end_pos=len(text),
            )]

        # 回退：行级滑动窗口
        return self._line_window_split(lines, base_meta)

    # ------------------------------------------------------------------
    # AST 分块（Python）
    # ------------------------------------------------------------------

    @staticmethod
    def _ast_split_python(text: str, base_meta: dict) -> list[Chunk]:
        """使用 Python AST 按函数/类边界分块。"""
        try:
            import ast
            tree = ast.parse(text)
        except (SyntaxError, ImportError):
            return []

        nodes: list[tuple[int, int, str]] = []  # (lineno, end_lineno, name)

        # 只遍历顶层节点，避免 Module 节点导致整个文件变成一个 chunk
        for node in ast.iter_child_nodes(tree):
            _collect_nodes(node, nodes)

        if not nodes:
            return []

        # 按行号排序
        nodes.sort(key=lambda x: x[0])
        lines = text.split("\n")
        chunks: list[Chunk] = []

        for lineno, end_lineno, name in nodes:
            start_idx = max(0, lineno - 1)
            end_idx = min(len(lines), end_lineno)
            chunk_lines = lines[start_idx:end_idx]
            chunk_text = "\n".join(chunk_lines)
            if chunk_text.strip():
                chunks.append(Chunk(
                    content=chunk_text,
                    metadata=dict(base_meta, chunk_index=len(chunks), symbol=name),
                    start_pos=sum(len(l) + 1 for l in lines[:start_idx]),
                    end_pos=sum(len(l) + 1 for l in lines[:end_idx]),
                ))

        return chunks

    # ------------------------------------------------------------------
    # 正则分块（通用��言）
    # ------------------------------------------------------------------

    @staticmethod
    def _func_split(
        text: str, lines: list[str], language: str, base_meta: dict
    ) -> list[Chunk]:
        """按函数/类定义行分块。"""
        pattern = CodeChunker._FUNC_PATTERNS.get(language)
        if not pattern:
            return []

        # 找到所有函数/类开始行
        boundaries: list[int] = [0]
        for m in pattern.finditer(text):
            line_idx = text[:m.start()].count("\n")
            boundaries.append(line_idx)

        if len(boundaries) <= 1:
            return []

        boundaries.append(len(lines))
        chunks: list[Chunk] = []

        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1]
            chunk_lines = lines[start:end]
            chunk_text = "\n".join(chunk_lines)
            if chunk_text.strip():
                # 提取函数名
                first_line = chunk_lines[0].strip() if chunk_lines else ""
                chunks.append(Chunk(
                    content=chunk_text,
                    metadata=dict(base_meta, chunk_index=len(chunks), symbol=first_line[:60]),
                    start_pos=sum(len(l) + 1 for l in lines[:start]),
                    end_pos=sum(len(l) + 1 for l in lines[:end]),
                ))

        return chunks

    # ------------------------------------------------------------------
    # 行级滑动窗口（回退）
    # ------------------------------------------------------------------

    def _line_window_split(
        self, lines: list[str], base_meta: dict
    ) -> list[Chunk]:
        """行级滑动窗口分块。"""
        chunks: list[Chunk] = []
        current_lines: list[str] = []
        current_len = 0
        current_start = 0

        for line in lines:
            line_len = len(line) + 1  # +1 for newline
            if current_len + line_len > self.chunk_size and current_lines:
                content = "\n".join(current_lines)
                chunks.append(Chunk(
                    content=content,
                    metadata=dict(base_meta, chunk_index=len(chunks)),
                    start_pos=current_start,
                    end_pos=current_start + len(content),
                ))
                # overlap
                overlap = current_lines[-self.overlap_lines:] if self.overlap_lines > 0 else []
                current_lines = overlap
                current_start += len("\n".join(current_lines[:-len(overlap)])) + (1 if len(current_lines) > len(overlap) else 0)
                current_len = sum(len(l) + 1 for l in overlap)

            current_lines.append(line)
            current_len += line_len
            if len(current_lines) == 1:
                current_start = sum(len(l) + 1 for l in lines[:lines.index(line)])

        if current_lines:
            content = "\n".join(current_lines)
            chunks.append(Chunk(
                content=content,
                metadata=dict(base_meta, chunk_index=len(chunks)),
                start_pos=            current_start,
                end_pos=current_start + len(content),
            ))

        return chunks


# ------------------------------------------------------------------
# AST 辅助 —— 递归收集函数/类节点
# ------------------------------------------------------------------

def _collect_nodes(node: Any, out: list[tuple[int, int, str]]) -> None:
    """递归收集 AST 中的函数定义和类定义节点。"""
    import ast as _ast

    if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
        out.append((
            node.lineno,
            getattr(node, "end_lineno", node.lineno) or node.lineno,
            f"def {node.name}()",
        ))
    elif isinstance(node, _ast.ClassDef):
        out.append((
            node.lineno,
            getattr(node, "end_lineno", node.lineno) or node.lineno,
            f"class {node.name}",
        ))
        for child in _ast.iter_child_nodes(node):
            _collect_nodes(child, out)
