# Security

> 仓库已按 **Apache-2.0** 发布（见 [LICENSE](LICENSE)）。本节描述协议
> 与参考实现的安全姿态与报告通道。

## Reference implementation security posture

- **零运行时第三方依赖**（`pyproject.toml` 的 `dependencies` 为空）。
  依赖白名单由 `scripts/deps_check.py` 强制，新增依赖必须先登记理由。
- **代码不监听网络端口**。控制面在 DEVELOPER PREVIEW 走进程内 RPC
  （`route()`）；§11.1 设计的命名管道 / UDS 传输在 v1 部署。
- **任何「模型输出直接变命令 / 拼 shell」的代码是设计缺陷**——按 bug
  处理。详见 §14.1 威胁模型。
- **fencing 是写回路径的硬边界**（§7、§14.1）：旧代次写回事件不落库。
- **§4.1 临时上下文容量合同 fail-closed**：超限拒绝 attempt 启动。

## Threat model

详见 [DESIGN.md §14.1](DESIGN.md#141-威胁模型)。本实现明确**不防**：
恶意 root、其他用户账户、网络远程攻击者、被攻陷的 harness 宿主。
v0.1 假设本机单用户受信任环境。

## Reporting

- **公开前**（当前）：直接提给维护者。
- **公开后**（v0.1.0 之后）：通过 GitHub Security Advisory 报告，不要
  开公开 issue 描述可利用细节。

