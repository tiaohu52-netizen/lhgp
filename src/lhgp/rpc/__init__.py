"""LHGP RPC public API.

该入口只导出无副作用的协议类型和客户端调用函数；服务端路由与
handlers 保持惰性导入，避免包初始化阶段形成循环依赖。
"""

from lhgp.rpc.client import call_unix_socket
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.methods import Method

__all__ = ["ErrorCode", "Method", "RpcError", "call_unix_socket"]
