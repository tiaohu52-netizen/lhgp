"""持久层：唯一碰 state.db 的层（DESIGN §3.1、§13.3）。

保持为空入口以避免 ``longtask.persistence.store`` 初始化时与 canonical
包形成循环依赖；具体 API 仍从对应模块导入。
"""
