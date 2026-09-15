"""执行层：每个 harness 一个薄适配器与执行器资源池（DESIGN §3.4、§8、§12）。

适配器只做三件事：入场（打包合同+交接）、翻译（约束→强制面，翻不了拒接）、
回报（写回交接、释放租约）。不复制合同状态，只做接线。
"""

from longtask.adapters.base import (
    AttemptInput,
    ExecutorAdapter,
    PreparedLaunch,
    PrepareRefusedError,
)
from longtask.adapters.manifest import (
    Capabilities,
    ExecutorManifest,
    SandboxCapability,
)
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)

__all__ = [
    "AttemptInput",
    "Capabilities",
    "CostHint",
    "ExecutorAdapter",
    "ExecutorManifest",
    "ExecutorRegistry",
    "LaunchSpec",
    "PrepareRefusedError",
    "PreparedLaunch",
    "RegistryEntry",
    "SandboxCapability",
]
