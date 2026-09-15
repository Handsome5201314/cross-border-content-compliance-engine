# -*- coding: utf-8 -*-
"""DAO 层包：用户 / 积分流水 / 任务 / 审计 的持久化抽象（SQLite 单文件）。

设计要点（见 ARCHITECTURE_V2.md §3、§10）：
- 所有 DAO 经由 `dao.db.Database` 单例取连接，业务代码不直接 `sqlite3.connect`。
- 连接懒创建（首次 `Database.get()` 触发 `init_schema`），import 阶段不连库，
  保证 `app.py` 在 AppTest / 演示模式下不会触碰数据库。
- 写操作统一走 `Database.transaction()` 上下文管理器 + 全局写锁串行化，杜绝并发双花。
"""
