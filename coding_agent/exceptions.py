"""
exceptions.py —— Coding Agent 的异常体系
==========================================
把所有工具层的错误从「字符串返回」升级为结构化异常，
让调用方能用 try/except 处理，而非靠 startswith("错误")。
"""


class ToolError(Exception):
    """所有工具异常的基类。"""

    def __init__(self, message: str, *, tool_name: str | None = None):
        super().__init__(message)
        self.tool_name = tool_name


class ToolInputError(ToolError):
    """参数不合法（缺必填参数、类型错误、路径为空等）。"""
    pass


class ToolExecutionError(ToolError):
    """工具执行时发生运行时错误（文件不存在、权限不足、编码错误等）。"""
    pass


class ToolSecurityError(ToolError):
    """安全限制触发（路径逃逸、命令被阻止等）。"""
    pass


class ToolTimeoutError(ToolError):
    """命令或操作超时。"""
    pass


class AgentAPIError(Exception):
    """API 调用层异常（网络错误、重试耗尽等）。"""
    pass


class ModelResponseError(AgentAPIError):
    """The provider replied, but its response cannot complete a model round."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


class TurnPersistenceError(Exception):
    """An execution checkpoint could not be saved or recovered."""
