"""记忆系统 — 三层记忆（短期 / 长期 / 语义）+ 自动学习（抽取 + 压缩）。

快速上手:
    mm = MemoryManager(user_id="user_001")

    # 基础用法
    mm.add_message("user", "我叫张三，做后端开发")
    mm.save_fact("role", "backend_dev")

    # 开启自动学习（从对话中自动抽事实 + 压缩）
    mm.enable_auto_learning(llm_client)
    # 后台压缩循环（需在 async 上下文中）
    # await mm.compressor.run()
"""

from memory.compressor import MemoryCompressor
from memory.db import MemoryDB
from memory.extractor import MemoryExtractor
from memory.long_term import MemoryEntry, SemanticMemoryStore, SQLiteMemoryStore
from memory.memory_manager import MemoryManager
from memory.short_term import Message, ShortTermMemory

__all__ = [
    # 入口
    "MemoryManager",
    # 数据库
    "MemoryDB",
    # 短期
    "ShortTermMemory",
    "Message",
    # 长期
    "MemoryEntry",
    "SQLiteMemoryStore",
    "SemanticMemoryStore",
    # 自动学习
    "MemoryExtractor",
    "MemoryCompressor",
]
