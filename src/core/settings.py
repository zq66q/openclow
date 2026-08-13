#Pydantic 配置中心，管理所有环境配置

"""全局配置中心 - Pydantic Settings 强类型管理.

三层优先级：环境变量 > .env 文件 > 代码默认值.
"""

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Literal

# 预加载 .env 文件，确保 Pydantic BaseSettings 能读取到环境变量
load_dotenv(".env", override=True)


class LLMConfig(BaseSettings):
    """LLM 大模型配置组."""
    model_config = SettingsConfigDict(env_prefix="LLM_")

    provider: str = Field(default="openai")
    api_key: str = Field(default="")
    base_url: str = Field(default="https://api.openai.com/v1")
    model: str = Field(default="gpt-4o")
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=4096, gt=0)
    timeout: int = Field(default=60, gt=0)

    # 多模态配置（用于图片/语音输入）
    vision_model: str = Field(default="", description="视觉模型，空则用 model")
    vision_base_url: str = Field(default="", description="视觉模型 base_url，空则用 base_url")
    vision_api_key: str = Field(default="", description="视觉模型 api_key，空则用 api_key")


class LLMMiniConfig(BaseSettings):
    """小模型配置（摘要、路由等低成本场景）."""
    model_config = SettingsConfigDict(env_prefix="LLM_MINI_")

    provider: str = Field(default="openai")
    api_key: str = Field(default="")
    base_url: str = Field(default="https://api.openai.com/v1")
    model: str = Field(default="gpt-4o-mini")


class EmbeddingConfig(BaseSettings):
    """Embedding 嵌入模型配置."""
    model_config = SettingsConfigDict(env_prefix="EMBEDDING_")

    provider: str = Field(default="openai")
    api_key: str = Field(default="")
    base_url: str = Field(default="https://api.openai.com/v1")
    model: str = Field(default="text-embedding-3-large")


class RAGConfig(BaseSettings):
    """RAG 知识库配置."""
    model_config = SettingsConfigDict(env_prefix="RAG_")

    vector_store_type: Literal["chroma", "qdrant", "milvus"] = Field(default="chroma")
    vector_store_path: str = Field(default="./data/vector_db")
    chunk_size: int = Field(default=512)
    chunk_overlap: int = Field(default=128)
    top_k_retrieve: int = Field(default=10)
    top_k_rerank: int = Field(default=5)
    similarity_threshold: float = Field(default=0.5, ge=0, le=1)


class MCPConfig(BaseSettings):
    """MCP 工具层配置."""
    model_config = SettingsConfigDict(env_prefix="MCP_")

    tool_timeout: int = Field(default=30)
    tool_max_retries: int = Field(default=3)
    circuit_breaker_threshold: int = Field(default=5)
    circuit_breaker_recovery: int = Field(default=30)


class MemoryConfig(BaseSettings):
    """分层记忆系统配置."""
    model_config = SettingsConfigDict(env_prefix="MEMORY_")

    db_path: str = Field(default="./data/memory.db")
    short_term_max_tokens: int = Field(default=4000)
    long_term_top_k: int = Field(default=5)
    summary_interval: int = Field(default=10)


class APIConfig(BaseSettings):
    """API 服务配置."""
    model_config = SettingsConfigDict(env_prefix="API_")

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    workers: int = Field(default=1)


class UIConfig(BaseSettings):
    """前端 UI 配置."""
    model_config = SettingsConfigDict(env_prefix="STREAMLIT_")

    port: int = Field(default=8501)
    title: str = Field(default="OpenClaw 企业智能助手")


class AuditConfig(BaseSettings):
    """审计日志配置."""
    model_config = SettingsConfigDict(env_prefix="AUDIT_")

    log_path: str = Field(default="./data/audit_logs")
    log_level: str = Field(default="INFO")


class SecurityConfig(BaseSettings):
    """安全配置."""
    model_config = SettingsConfigDict(env_prefix="")

    file_sandbox_root: str = Field(default="./data/uploads", alias="FILE_SANDBOX_ROOT")
    allowed_file_extensions: str = Field(default="pdf,docx,xlsx,png,jpg,jpeg", alias="ALLOWED_FILE_EXTENSIONS")
    max_upload_size_mb: int = Field(default=10, alias="MAX_UPLOAD_SIZE_MB")


class Settings(BaseSettings):
    """全局配置聚合."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm: LLMConfig = Field(default_factory=LLMConfig)
    llm_mini: LLMMiniConfig = Field(default_factory=LLMMiniConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    # LangSmith
    langchain_tracing_v2: bool = Field(default=True, alias="LANGCHAIN_TRACING_V2")
    langchain_api_key: str = Field(default="", alias="LANGCHAIN_API_KEY")
    langchain_project: str = Field(default="openclaw", alias="LANGCHAIN_PROJECT")

    # Tavily
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")


# 全局单例
settings = Settings()
