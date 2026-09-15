# -*- coding: utf-8 -*-
"""FastAPI 后端包（ARCHITECTURE_V3.md §2）。

只做「换壳」：路由 / 会话 / SSE / 静态服务。业务逻辑全部 import 复用 V2 层
（dao/ auth/ credits/ tasks/ storage/ admin/ core/），本包不复制任何业务规则。
"""
