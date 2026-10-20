"""所有层共享的中文业务异常。"""


class MindMapError(ValueError):
    """面向调用方的输入、文档或操作错误，错误消息使用中文。"""


class OperationRejected(MindMapError):
    """前置钩子明确拒绝了操作；引擎不会留下部分修改或历史记录。"""
