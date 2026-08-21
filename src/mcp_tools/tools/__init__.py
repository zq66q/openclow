"""MCP 工具实现包。

显式 import 所有 Tool 子模块，触发 @register_tool 装饰器完成注册。
（注册中心没有自动扫描机制，必须手动 import。）

新加工具时在此 import 即可被全局注册中心发现。
"""

from . import basic_tools, file_tools, orchestrator_tool, search_tool  # noqa: F401
# http_tool 是内部共用模块，不做 Tool 注册
