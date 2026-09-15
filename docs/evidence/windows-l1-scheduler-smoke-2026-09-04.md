# Windows L1 计划任务真实 smoke（2026-09-04）

## 范围

使用临时合同目录和一次性任务 ID 验证 `WindowsTaskSchedulerPort` 的真实
`schtasks.exe` 生命周期。任务动作只指向本机 `daemon/wake` RPC；本次不触发
任务本身，避免对运行中的 daemon 产生副作用。

## 结果

- `schtasks.exe` 创建返回码：`0`
- `/Query` 返回：`Status: Ready`
- 任务名：`\LHGP\longtask-wakeup-smoke-20260904c`
- Windows 接受的日期格式：`yyyy/mm/dd`（此前的 `mm/dd/yyyy` 会被拒绝）
- `schtasks.exe /Delete` 后再次查询返回码：`1`
- 结论：`task_present=True`，删除后 `task_absent=True`

## 清理

任务已在 smoke 结束前删除；临时目录仅用于生成动作命令，未写入项目数据目录。

## 限定

该证据证明 L1 平台任务的创建、查询和删除链路；不证明未来任务到点时 daemon
进程、RPC socket 或外部 CLI 一定在线。到点后的 `wakeup/rtc-fired` 消费仍由
daemon 运行时集成测试覆盖。
