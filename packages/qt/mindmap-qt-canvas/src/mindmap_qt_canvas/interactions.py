"""画布交互的来源作用域，供共享引擎的多个独立视图区分本地提交。"""

from functools import wraps


def document_interaction(method):
    """为画布方法发出的同步引擎事件添加所属视图标识，保留原签名与中文文档。

    嵌套调用恢复外层来源；异步回调必须在实际提交方法上使用本装饰器，不能
    依赖发起请求时的作用域。装饰器不捕获异常、不创建事务、不改变撤销粒度。
    """
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self.engine.event_source(self.interaction_id):
            return method(self, *args, **kwargs)

    return invoke
