# 自定义异常体系

"""自定义异常体系.

recoverable: 是否可恢复（可恢复异常自动重试，不可恢复异常直接上报）
"""


class OpenClawBaseError(Exception):
    """根异常."""

    recoverable: bool = False

    def __init__(self, message: str, *, recoverable: bool | None = None):
        super().__init__(message)
        if recoverable is not None:
            self.recoverable = recoverable


# 配置类异常
class ConfigError(OpenClawBaseError):
    """配置错误 - 启动时检查，不可恢复."""

    recoverable = False


# LLM 类异常
class LLMError(OpenClawBaseError):
    """LLM 调用异常基类."""

    pass


class LLMTimeoutError(LLMError):
    """LLM 请求超时 - 可恢复，退避重试."""

    recoverable = True


class LLMRateLimitError(LLMError):
    """LLM 限流 - 可恢复，指数退避."""

    recoverable = True


class LLMContextWindowError(LLMError):
    """上下文窗口超限 - 需压缩."""

    recoverable = True


# 工具类异常
class ToolError(OpenClawBaseError):
    """工具调用异常基类."""

    pass


class ToolTimeoutError(ToolError):
    """工具调用超时 - 可恢复，触发熔断."""

    recoverable = True


class ToolAuthError(ToolError):
    """工具鉴权失败 - 不可恢复."""

    recoverable = False


class ToolParamError(ToolError):
    """工具参数错误 - 不可恢复，返回给用户."""

    recoverable = False


# 熔断类异常
class CircuitBreakerOpen(OpenClawBaseError):
    """熔断器打开 - 不可恢复，自动降级."""

    recoverable = False


# RAG 类异常
class RAGError(OpenClawBaseError):
    """RAG 检索异常基类."""

    pass


class RAGNotFoundError(RAGError):
    """知识库未找到匹配 - 不可恢复，返回拒答."""

    recoverable = False


class RAGCorruptError(RAGError):
    """知识库数据损坏 - 可恢复，尝试重建."""

    recoverable = True


# 记忆类异常
class MemoryError(OpenClawBaseError):
    """记忆系统异常基类."""

    pass
